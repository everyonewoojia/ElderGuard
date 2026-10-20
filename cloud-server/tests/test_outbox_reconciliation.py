"""Real device GET resolves exhausted local delivery without a fifth POST."""
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import httpx

from app.main import app
from app.models.models import AlertLog, Device, EdgeAlertReceipt


async def test_exhausted_lost_responses_are_reconciled_via_authenticated_get(auth, device_credentials, test_database):
    from src.core.alert_outbox import AlertOutbox
    from src.integration.edge import EdgeClient, EdgeReporter
    from src.integration.outbox_admin import OutboxAdmin

    auth["register"]("reconcile-family", "13800008888")
    elder_id = auth["create_elder"]("reconcile-family", "synthetic")
    token = device_credentials(elder_id)
    with test_database.session() as session:
        device_id = session.query(Device).filter_by(elder_id=elder_id).one().device_id
    directory = test_database.root / "outbox-reconcile"
    directory.mkdir()
    inner = httpx.ASGITransport(app=app)
    posts, gets = [], []

    class LostResponses(httpx.AsyncBaseTransport):
        """Only the response disappears; database transactions really commit."""

        async def handle_async_request(self, request):
            response = await inner.handle_async_request(request)
            if request.method == "POST":
                assert response.status_code == 200
                posts.append(json.loads(request.content))
                await response.aclose()
                raise httpx.ReadError("synthetic response loss")
            gets.append(str(request.url.path))
            return response

    now = [datetime.now(timezone.utc)]
    event_id = str(uuid4())
    with AlertOutbox(directory, device_id=device_id, elder_id=elder_id, cloud_url="http://127.0.0.1:8000",
                     now=lambda: now[0]) as outbox:
        outbox.enqueue({"eventId": event_id, "type": "help", "priority": "P0", "timestamp": now[0].isoformat(),
                        "payload": {"content": "synthetic help"}})
        async with httpx.AsyncClient(transport=LostResponses()) as http:
            client = EdgeClient(base_url="http://127.0.0.1:8000", client=http, device_token=token)
            reporter = EdgeReporter(client, outbox=outbox)
            for _ in range(4):
                assert await reporter.run_once() is False
                now[0] += timedelta(minutes=5)
            row = outbox.get(event_id)
            assert row["state"] == "exhausted" and row["attempts"] == 4
            admin = OutboxAdmin(outbox, client)
            args = dict(event_id=event_id, action="reconcile", expected_revision=row["revision"], confirm_event_id=event_id,
                        operation_id=str(uuid4()), operator="local-admin", reason="reviewed")
            await admin.apply(**args)
            await admin.apply(**args)
            assert await reporter.run_once() is False
        row = outbox.get(event_id)
        assert row["state"] == "received" and row["attempts"] == 4
        assert row["cloud_confirmation"] == "unconfirmed"
        assert [body["event_id"] for body in posts] == [event_id] * 4
        assert len(gets) == 1 and len(outbox.audit()) == 1
        with test_database.session() as session:
            assert session.query(AlertLog).count() == session.query(EdgeAlertReceipt).count() == 1
            receipt = session.query(EdgeAlertReceipt).one()
            assert receipt.alert_id == row["cloud_alert_id"] and receipt.event_id == event_id
