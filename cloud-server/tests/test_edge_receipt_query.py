"""Read-only receipt lookup under the original device/elder authorization."""
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.models.models import AlertLog, EdgeAlertReceipt
from app.services import device_auth, edge_alerts
from test_device_auth import identity, device_key  # noqa: F401
from test_edge_idempotency import payload, post


def lookup(client, identity, event_id, *, token=None, elder_id=None):
    db, device, elder = identity
    token = token or device_auth.issue_token(db, device.device_id)
    return client.get(f"/api/v1/edge/alerts/receipts/{event_id}",
                      params={"elder_id": elder.id if elder_id is None else elder_id},
                      headers={"Authorization": f"Bearer {token}"})


def test_lookup_found_is_read_only_and_preserves_unknown(client, identity, test_database, monkeypatch):
    body = payload(identity[2].id)
    alert_id = post(client, identity, body).json()["data"]["alert_id"]
    with test_database.session() as session:
        row = session.query(EdgeAlertReceipt).one()
        row.notification_state = "unknown"
        session.commit()
        digest = row.payload_hash
    forbidden = Mock(side_effect=AssertionError("GET must not admit or notify"))
    monkeypatch.setattr(edge_alerts, "accept_alert", forbidden)
    from app.services.ws_manager import manager
    monkeypatch.setattr(manager, "push_to_user", forbidden)
    for _ in range(2):
        response = lookup(client, identity, body["event_id"])
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["alert_id"] == alert_id and data["payload_hash"] == digest
        assert data["notification"] == "unknown" and data["confirmation"] == "unconfirmed"
        assert data["receipt_status"] == "received"
        assert "content" not in data and "owner_id" not in data
    forbidden.assert_not_called()
    with test_database.session() as session:
        assert session.query(AlertLog).count() == session.query(EdgeAlertReceipt).count() == 1
        assert session.query(EdgeAlertReceipt).one().notification_state == "unknown"


def test_other_device_cannot_probe_original_namespace(client, identity):
    db, device, elder = identity
    body = payload(elder.id)
    post(client, identity, body)
    other = device_auth.register_device(db, elder.id)
    token = device_auth.issue_token(db, other.device_id)
    response = lookup(client, identity, body["event_id"], token=token)
    assert response.status_code == 200
    assert response.json()["data"] == {"event_id": body["event_id"], "device_id": other.device_id,
                                       "receipt_status": "not_found"}


@pytest.mark.parametrize("case", ["anonymous", "invalid", "revoked", "cross", "missing_elder", "family", "version"])
def test_lookup_rejects_before_receipt_query(client, identity, monkeypatch, case):
    db, device, elder = identity
    token = device_auth.issue_token(db, device.device_id)
    if case == "revoked":
        device_auth.revoke_device(db, device.device_id)
    if case == "version":
        device.credential_version += 1
        db.commit()
    if case == "family":
        from app.services.auth import create_access_token
        token = create_access_token({"sub": "device-owner", "role": "family"})
    forbidden = Mock(side_effect=AssertionError("unauthorized receipt read"))
    monkeypatch.setattr(edge_alerts, "_lookup_receipt", forbidden)
    headers = {} if case == "anonymous" else {"Authorization": "Bearer invalid" if case == "invalid" else f"Bearer {token}"}
    target = elder.id if case not in ("cross", "missing_elder") else elder.id + 99999
    response = client.get(f"/api/v1/edge/alerts/receipts/{uuid4()}", params={"elder_id": target}, headers=headers)
    assert response.status_code == (403 if case in ("cross", "missing_elder") else 401)
    forbidden.assert_not_called()


@pytest.mark.parametrize("content,priority", [("中文 🌿", "P0"), ("  ", "P2"), (None, "P1"), ("", "P1")])
def test_query_hash_agrees_with_local_canonical_contract(client, identity, content, priority):
    from src.core.alert_outbox import payload_hash
    body = payload(identity[2].id, content=content, priority=priority)
    assert post(client, identity, body).status_code == 200
    data = lookup(client, identity, body["event_id"]).json()["data"]
    assert data["payload_hash"] == payload_hash(dict(reversed(list(body.items()))))
    changed = {**body, "content": "different"}
    assert payload_hash(changed) != data["payload_hash"]


def test_receipt_storage_error_is_not_not_found(client, identity, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError
    monkeypatch.setattr(edge_alerts, "_lookup_receipt", Mock(side_effect=SQLAlchemyError("private storage error")))
    response = lookup(client, identity, str(uuid4()))
    assert response.status_code == 503
    assert "private storage error" not in response.text
