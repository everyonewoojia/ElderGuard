"""Authenticated idempotency on the P0-12 registered SQLite/Session factory."""
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.models import AlertLog, EdgeAlertReceipt
from app.routers import edge
from app.services import device_auth, edge_alerts
from test_device_auth import identity, device_key  # noqa: F401


def payload(elder_id, **changes):
    """Fresh stable client identity; no server-generated fallback."""
    return {"elder_id": elder_id, "event_id": str(uuid4()), "alert_type": "help",
            "priority": "P0", "content": "synthetic help", **changes}


def post(client, identity, body):
    """Use a real device JWT without echoing it in failure output."""
    db, device, _ = identity
    token = device_auth.issue_token(db, device.device_id)
    return client.post("/api/v1/edge/alerts", json=body,
                       headers={"Authorization": f"Bearer {token}"})


@pytest.mark.parametrize("value", [None, "not-a-uuid", "", 12])
def test_event_id_is_required_and_validated(client, identity, value):
    body = payload(identity[2].id)
    if value is None:
        body.pop("event_id")
    else:
        body["event_id"] = value
    assert post(client, identity, body).status_code == 422
    assert identity[0].query(AlertLog).count() == 0
    assert identity[0].query(EdgeAlertReceipt).count() == 0


def test_first_receipt_and_lost_response_retry_have_one_alert_and_notification(client, identity, test_database):
    from test_delivery_api import wait_state
    notify = AsyncMock(return_value=1)
    worker = client.app.state.delivery_worker
    worker.service.sender = notify
    worker.service.online_owners = lambda: None
    body = payload(identity[2].id)
    first = post(client, identity, body)
    repeat = post(client, identity, body)  # Same retry after ignoring the first response.
    assert first.status_code == repeat.status_code == 200
    data = first.json()["data"]
    assert repeat.json()["data"]["alert_id"] == data["alert_id"]
    assert data["event_id"] == body["event_id"]
    assert data["device_id"] == identity[1].device_id
    assert data["receipt_status"] == "received"
    assert data["notification"] == "pending"
    assert data["confirmation"] == "unconfirmed"
    assert data["status"] == "pending"
    wait_state(test_database, data["alert_id"], "attempt_returned")
    assert post(client, identity, body).json()["data"]["notification"] == "attempt_returned"
    notify.assert_awaited_once()
    db = identity[0]
    assert db.query(AlertLog).count() == db.query(EdgeAlertReceipt).count() == 1
    receipt = db.query(EdgeAlertReceipt).one()
    assert receipt.alert_id == data["alert_id"]
    assert len(receipt.payload_hash) == 64
    assert receipt.received_at is not None


@pytest.mark.parametrize("field,value", [("content", "different"), ("priority", "P1"), ("alert_type", "fall")])
def test_changed_business_payload_is_conflict(client, identity, field, value):
    body = payload(identity[2].id)
    assert post(client, identity, body).status_code == 200
    changed = {**body, field: value}
    assert post(client, identity, changed).status_code == 409
    assert identity[0].query(AlertLog).count() == identity[0].query(EdgeAlertReceipt).count() == 1
    assert identity[0].query(AlertLog).one().content == body["content"]


def test_distinct_devices_have_separate_namespaces(client, identity):
    db, device, elder = identity
    body = payload(elder.id)
    first = post(client, identity, body).json()["data"]
    other = device_auth.register_device(db, elder.id)
    second = post(client, (db, other, elder), {**body, "content": "other device"}).json()["data"]
    assert first["alert_id"] != second["alert_id"]
    assert first["device_id"] == device.device_id
    assert second["device_id"] == other.device_id
    assert db.query(EdgeAlertReceipt).count() == db.query(AlertLog).count() == 2


@pytest.mark.parametrize("case", ["anonymous", "invalid", "revoked", "foreign"])
def test_security_rejection_precedes_receipt_lookup(client, identity, monkeypatch, case):
    db, device, elder = identity
    body = payload(elder.id)
    token = device_auth.issue_token(db, device.device_id)
    headers = {"Authorization": f"Bearer {token}"}
    expected = 401
    if case == "anonymous":
        headers = {}
    elif case == "invalid":
        headers = {"Authorization": "Bearer invalid-test-credential"}
    elif case == "revoked":
        device_auth.revoke_device(db, device.device_id)
    else:
        body["elder_id"] = 999999
        expected = 403
    lookup = Mock(side_effect=AssertionError("receipt query before authorization"))
    notify = AsyncMock()
    from app.services.ws_manager import manager
    monkeypatch.setattr(edge_alerts, "_lookup_receipt", lookup)
    monkeypatch.setattr(manager, "push_to_user", notify)
    monkeypatch.setattr(edge_alerts, "accept_alert", Mock(side_effect=AssertionError("admission before authorization")))
    result = client.post("/api/v1/edge/alerts", json=body, headers=headers)
    assert result.status_code == expected
    lookup.assert_not_called()
    notify.assert_not_called()
    assert db.query(AlertLog).count() == db.query(EdgeAlertReceipt).count() == 0


@pytest.mark.parametrize("failure", [False, "exception"])
def test_notification_failure_retains_committed_receipt(client, identity, test_database, failure):
    from datetime import datetime, timezone
    from app.services.alert_delivery import NotSent
    from test_delivery_api import wait_state
    notify = AsyncMock(side_effect=NotSent())
    if failure == "exception":
        notify.side_effect = OSError("controlled notification failure")
    worker = client.app.state.delivery_worker
    worker.service.sender = notify
    worker.service.online_owners = lambda: None
    worker.service.clock = lambda: datetime(2026, 10, 18, tzinfo=timezone.utc)
    body = payload(identity[2].id)
    first = post(client, identity, body)
    assert first.status_code == 200
    expected = "unknown" if failure == "exception" else "retry_wait"
    wait_state(test_database, first.json()["data"]["alert_id"], expected)
    assert first.json()["data"]["receipt_status"] == "received"
    repeated = post(client, identity, body)
    assert repeated.status_code == 200
    assert repeated.json()["data"]["notification"] == expected
    notify.assert_awaited_once()
    assert identity[0].query(AlertLog).count() == identity[0].query(EdgeAlertReceipt).count() == 1


def test_receipt_flush_failure_rolls_back_alert(client, identity, monkeypatch):
    original = Session.flush

    def fail_receipt(session, *args, **kwargs):
        if any(isinstance(obj, EdgeAlertReceipt) for obj in session.new):
            raise OperationalError("synthetic", {}, RuntimeError("controlled write failure"))
        return original(session, *args, **kwargs)

    monkeypatch.setattr(Session, "flush", fail_receipt)
    response = post(client, identity, payload(identity[2].id))
    assert response.status_code == 503
    assert identity[0].query(AlertLog).count() == identity[0].query(EdgeAlertReceipt).count() == 0


@pytest.mark.parametrize("committed", [False, True])
def test_commit_error_never_fabricates_receipt(client, identity, test_database, monkeypatch, committed):
    from test_delivery_api import wait_state
    original = Session.commit
    notify = AsyncMock()
    worker = client.app.state.delivery_worker
    worker.service.sender = notify
    worker.service.online_owners = lambda: []
    body = payload(identity[2].id)
    # Issue before patching transaction commit (issuing itself only reads).
    token = device_auth.issue_token(identity[0], identity[1].device_id)

    def uncertain(session):
        if not any(isinstance(obj, EdgeAlertReceipt) for obj in session.new):
            return original(session)
        if committed:
            original(session)
        raise OperationalError("synthetic", {}, RuntimeError("controlled uncertain commit"))

    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", uncertain)
        response = client.post("/api/v1/edge/alerts", json=body,
                               headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 503
        assert response.json()["data"] is None
    notify.assert_not_called()
    assert identity[0].query(AlertLog).count() == int(committed)
    assert identity[0].query(EdgeAlertReceipt).count() == int(committed)
    retry = post(client, identity, body)
    assert retry.status_code == 200
    assert identity[0].query(AlertLog).count() == identity[0].query(EdgeAlertReceipt).count() == 1
    worker.service.online_owners = lambda: None
    notify.return_value = 1
    wait_state(test_database, retry.json()["data"]["alert_id"], "attempt_returned")
    assert notify.await_count == 1


@pytest.mark.parametrize("different", [False, True])
def test_concurrent_unique_constraint_decides_winner(identity, test_database, monkeypatch, different):
    """Real two-thread transactions, same registered engine, checked Sessions."""
    db, device, elder = identity
    token = device_auth.issue_token(db, device.device_id)
    elder_id = elder.id
    body = payload(elder_id)
    barrier = threading.Barrier(2)
    local = threading.local()
    original = edge_alerts._lookup_receipt

    def simultaneous_lookup(*args):
        result = original(*args)
        if not getattr(local, "looked", False):
            local.looked = True
            assert result is None
            barrier.wait(timeout=15)
        return result

    monkeypatch.setattr(edge_alerts, "_lookup_receipt", simultaneous_lookup)

    def receive(index):
        with test_database.session() as session:
            authenticated = device_auth.authenticate_device(session, token)
            bound = edge.require_bound_elder(session, authenticated, elder_id)
            data = edge.EdgeAlertIn(**{**body, "content": str(index) if different else body["content"]})
            try:
                alert, receipt, created = edge_alerts.accept_alert(session, authenticated, bound, data)
                return 200, alert.id, created
            except HTTPException as exc:
                return exc.status_code, None, False

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(receive, i) for i in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert sorted(r[0] for r in results) == ([200, 409] if different else [200, 200])
    assert sum(r[2] for r in results) == 1
    if not different:
        assert results[0][1] == results[1][1]
    with test_database.session() as session:
        assert session.query(AlertLog).count() == session.query(EdgeAlertReceipt).count() == 1


def test_legacy_alert_and_family_query_remain_unchanged(auth, identity):
    db, _, elder = identity
    legacy = AlertLog(elder_id=elder.id, alert_type="legacy", priority="P0", content="historical", status="cancelled")
    db.add(legacy)
    db.commit()
    from app.services.auth import create_access_token
    token = create_access_token({"sub": "device-owner", "role": "family"})
    response = auth["client"].get("/api/v1/alerts", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert any(row["id"] == legacy.id and row["content"] == "historical" for row in response.json())
    assert db.query(EdgeAlertReceipt).count() == 0
