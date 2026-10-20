"""Real lifespan and durable HTTP receipt behavior, with fake send side effects."""
import asyncio
import time
from unittest.mock import AsyncMock

from starlette.websockets import WebSocketDisconnect

from app.models.models import EdgeAlertReceipt, User
from app.services import device_auth
from app.routers import ws
from test_device_auth import identity, device_key  # noqa: F401
from test_edge_idempotency import payload, post


def wait_state(owner, alert_id, expected, timeout=3):
    """Poll detached snapshots; never hold SQL open while waiting."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with owner.session() as session:
            receipt = session.query(EdgeAlertReceipt).filter_by(alert_id=alert_id).one()
            state = receipt.notification_state
        if state == expected:
            return
        time.sleep(0.01)
    raise AssertionError(f"notification state did not reach {expected}; last={state}")


def test_http_admission_and_repeat_expose_persisted_notification(client, identity, test_database):
    worker = client.app.state.delivery_worker
    sender = AsyncMock(return_value=2)
    worker.service.sender = sender
    worker.service.online_owners = lambda: None
    body = payload(identity[2].id)
    first = post(client, identity, body)
    assert first.status_code == 200
    data = first.json()["data"]
    assert data["notification"] == "pending"
    assert data["receipt_status"] == "received"
    assert data["confirmation"] == "unconfirmed"
    wait_state(test_database, data["alert_id"], "attempt_returned")
    repeat = post(client, identity, body).json()["data"]
    assert repeat["alert_id"] == data["alert_id"]
    assert repeat["notification"] == "attempt_returned"
    assert repeat["notification_send_count"] == 2
    assert repeat["confirmation"] == "unconfirmed"
    sender.assert_awaited_once()


def test_unknown_is_not_reset_by_duplicate_http(client, identity, test_database):
    worker = client.app.state.delivery_worker
    worker.service.online_owners = lambda: None
    sender = AsyncMock(side_effect=OSError("controlled ambiguous send"))
    worker.service.sender = sender
    body = payload(identity[2].id)
    response = post(client, identity, body)
    alert_id = response.json()["data"]["alert_id"]
    wait_state(test_database, alert_id, "unknown")
    assert post(client, identity, body).json()["data"]["notification"] == "unknown"
    sender.assert_awaited_once()


async def test_ws_reconnect_does_not_bypass_unknown_delivery_state(identity, test_database, monkeypatch):
    from app.routers.edge import EdgeAlertIn, require_bound_elder
    from app.services import edge_alerts
    db, device, elder = identity
    token = device_auth.issue_token(db, device.device_id)
    with test_database.session() as session:
        authenticated = device_auth.authenticate_device(session, token)
        bound = require_bound_elder(session, authenticated, elder.id)
        alert, receipt, _ = edge_alerts.accept_alert(session, authenticated, bound, EdgeAlertIn(**payload(elder.id)))
        receipt.notification_state = "unknown"
        session.commit()
    from unittest.mock import Mock
    from starlette.datastructures import Headers
    socket = Mock(headers=Headers(), query_params={"token": "synthetic"}, accept=AsyncMock(), send_json=AsyncMock(),
                  receive_text=AsyncMock(side_effect=WebSocketDisconnect()))
    monkeypatch.setattr(ws, "_resolve_user", lambda value: db.get(User, elder.owner_id))
    await ws.ws_alerts(socket)
    socket.send_json.assert_not_awaited()


def test_restart_worker_recovers_pending_with_original_alert(client, identity, test_database):
    from app.services.alert_delivery import DeliveryService, DeliveryWorker
    original = client.app.state.delivery_worker
    original.service.online_owners = lambda: []
    response = post(client, identity, payload(identity[2].id)).json()["data"]
    assert response["notification"] == "pending"
    client.portal.call(original.stop)
    sender = AsyncMock(return_value=1)
    restarted = DeliveryWorker(DeliveryService(session_scope=test_database.session, sender=sender,
                                               online_owners=lambda: None))
    try:
        client.portal.call(restarted.start)
        wait_state(test_database, response["alert_id"], "attempt_returned")
        assert sender.call_args.args[1]["data"]["id"] == response["alert_id"]
        with test_database.session() as session:
            row = session.query(EdgeAlertReceipt).one()
            assert row.event_id == response["event_id"]
    finally:
        client.portal.call(restarted.stop)


async def test_ws_database_error_still_releases_connection_registration(identity, monkeypatch):
    from unittest.mock import Mock
    from sqlalchemy.exc import SQLAlchemyError
    import pytest
    from app.services.ws_manager import manager
    owner_id = identity[2].owner_id
    from starlette.datastructures import Headers
    socket = Mock(headers=Headers(), query_params={}, accept=AsyncMock())
    monkeypatch.setattr(ws, "_resolve_user", lambda value: Mock(id=owner_id))
    monkeypatch.setattr(ws, "SessionLocal", Mock(side_effect=SQLAlchemyError("controlled storage failure")))
    try:
        with pytest.raises(RuntimeError, match="websocket_storage_unavailable"):
            await ws.ws_alerts(socket)
        assert owner_id not in manager.online_users()
    finally:
        await manager.disconnect(owner_id, socket)
