"""Default Agent assembly through durable local sending into the real cloud app."""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx

from app.main import app
from app.models.models import AlertLog, Device, EdgeAlertReceipt


async def test_default_agent_recovers_lost_response_without_duplicate_admission(
    auth, device_credentials, test_database, monkeypatch,
):
    """No direct test call to report_alert; all Sessions retain P0-12 checks."""
    from src import main

    auth["register"]("reporter-family", "13800007777")
    elder_id = auth["create_elder"]("reporter-family", "synthetic")
    credential = test_database.root / "reporter-device.credential"
    device_credentials(elder_id, token_file=credential)
    with test_database.session() as session:
        device_id = session.query(Device).filter_by(elder_id=elder_id).one().device_id
    directory = test_database.root / "reporter-data"
    directory.mkdir()
    for key, value in {"ELDER_DATA_DIR": str(directory), "EDGE_DEVICE_ID": device_id,
                       "EDGE_ELDER_ID": str(elder_id), "EDGE_BASE_URL": "http://127.0.0.1:8000",
                       "EDGE_DEVICE_TOKEN_FILE": str(credential)}.items():
        monkeypatch.setenv(key, value)
    calls = []
    inner = httpx.ASGITransport(app=app)

    class LoseFirstResponse(httpx.AsyncBaseTransport):
        """Commit via actual REST, then simulate the response disappearing."""

        async def handle_async_request(self, request):
            response = await inner.handle_async_request(request)
            if request.method == "POST":
                calls.append(json.loads(request.content))
                if len(calls) == 1:
                    assert response.status_code == 200
                    with test_database.session() as session:
                        session.query(EdgeAlertReceipt).one().notification_state = "unknown"
                        session.commit()
                    await response.aclose()
                    raise httpx.ReadError("synthetic lost response")
            return response

    async with httpx.AsyncClient(transport=LoseFirstResponse()) as http:
        real = main.EdgeClient
        monkeypatch.setattr(main, "EdgeClient", lambda **kwargs: real(client=http, **kwargs))
        agent = main.create_agent()
        agent.tts.speak = AsyncMock()
        # Inject a queue clock only; no backoff waits or new database engine.
        from datetime import datetime, timedelta, timezone
        from itertools import count
        ticks = count()
        agent.alert_outbox._now = lambda: datetime.now(timezone.utc) + timedelta(minutes=5 * next(ticks))
        try:
            await agent.start()
            result = await agent.handle_voice_text("救命")
            event_id = result["alert_result"]["event_id"]
            for _ in range(200):
                if agent.alert_outbox.get(event_id)["state"] == "received":
                    break
                await asyncio.sleep(0.01)
            stored = agent.alert_outbox.get(event_id)
            assert stored["state"] == "received"
            assert stored["cloud_notification"] == "unknown"
            assert stored["cloud_confirmation"] == "unconfirmed"
            assert [body["event_id"] for body in calls] == [event_id, event_id]
            with test_database.session() as session:
                assert session.query(AlertLog).count() == 1
                receipt = session.query(EdgeAlertReceipt).one()
                assert receipt.alert_id == stored["cloud_alert_id"]
                assert receipt.event_id == event_id and receipt.device_id == device_id
        finally:
            await agent.stop()
        assert agent.reporter.task is None
