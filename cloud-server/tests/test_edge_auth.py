"""Four edge routes must enforce verified device scope before business access."""
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from starlette.websockets import WebSocketDisconnect

from app.models.models import AlertLog
from app.routers import edge
from app.services import device_auth as service
from app.services.ws_manager import manager
from test_device_auth import device_key, identity  # noqa: F401


ROUTES = ["alerts", "tasks", "settings", "privacy"]


def request(client, route, elder_id, token=None):
    """Do not expose credentials in assertion messages."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if route == "alerts":
        return client.post("/api/v1/edge/alerts", json={
            "event_id": str(uuid4()), "elder_id": elder_id, "alert_type": "help", "priority": "P0",
        }, headers=headers)
    return client.get(f"/api/v1/edge/{route}", params={"elder_id": elder_id}, headers=headers)


@pytest.mark.parametrize("route", ROUTES)
def test_anonymous_rejected_without_side_effects(client, identity, monkeypatch, route):
    db, _, elder = identity
    notify = AsyncMock()
    create = Mock(side_effect=AssertionError("unauthorized write"))
    tasks = Mock(side_effect=AssertionError("unauthorized read"))
    monkeypatch.setattr(manager, "push_to_user", notify)
    monkeypatch.setattr(edge.storage, "create_alert", create)
    monkeypatch.setattr(edge.storage, "list_tasks_by_elder", tasks)
    assert request(client, route, elder.id).status_code == 401
    create.assert_not_called()
    tasks.assert_not_called()
    notify.assert_not_called()
    assert db.query(AlertLog).count() == 0


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("target", ["existing", "missing"])
def test_cross_elder_rejected_before_target_query(client, identity, monkeypatch, route, target):
    db, device, elder = identity
    from app.models.models import Elder
    other = Elder(name="other", owner_id=elder.owner_id)
    db.add(other)
    db.commit()
    target_id = other.id if target == "existing" else 999999
    token = service.issue_token(db, device.device_id)
    read = Mock(side_effect=AssertionError("target lookup prohibited"))
    notify = AsyncMock()
    monkeypatch.setattr(edge.storage, "get_elder", read)
    monkeypatch.setattr(manager, "push_to_user", notify)
    from sqlalchemy.orm import Session
    original_get = Session.get
    queried = []

    def checked_get(session, entity, ident, **kwargs):
        """Record actual ORM lookups, not just storage helper calls."""
        if entity is Elder:
            queried.append(ident)
        return original_get(session, entity, ident, **kwargs)

    monkeypatch.setattr(Session, "get", checked_get)
    assert request(client, route, target_id, token).status_code == 403
    assert target_id not in queried
    read.assert_not_called()
    notify.assert_not_called()
    assert db.query(AlertLog).count() == 0


@pytest.mark.parametrize("route", ROUTES)
def test_valid_device_preserves_contract(client, identity, route):
    db, device, elder = identity
    token = service.issue_token(db, device.device_id)
    response = request(client, route, elder.id, token)
    assert response.status_code == 200
    assert response.json()["code"] == 200
    if route == "alerts":
        assert response.json()["data"]["status"] == "pending"
        assert db.query(AlertLog).one().elder_id == elder.id
    elif route == "tasks":
        assert response.json()["data"] == []
    else:
        assert response.json()["data"]["elder_id"] == elder.id


def test_family_and_device_tokens_are_not_interchangeable(client, identity):
    from app.services.auth import create_access_token
    db, device, elder = identity
    family = create_access_token({"sub": "device-owner", "role": "family"})
    assert request(client, "privacy", elder.id, family).status_code == 401
    token = service.issue_token(db, device.device_id)
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/api/v1/ws/alerts?token={token}"):
            pass
    assert exc.value.code == 4401


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("kind", ["forged", "expired", "purpose", "audience", "unknown", "disabled", "revoked", "old_version"])
def test_invalid_devices_rejected_by_every_route(client, identity, monkeypatch, route, kind):
    import secrets
    from jose import jwt
    from app.core.config import settings
    db, device, elder = identity
    token = service.issue_token(db, device.device_id)
    claims = jwt.get_unverified_claims(token)
    key = settings.EDGE_JWT_SECRET
    if kind == "forged":
        key = secrets.token_urlsafe(48)
    elif kind == "expired":
        claims["exp"] = claims["iat"] - 1
    elif kind == "purpose":
        claims["purpose"] = "family"
    elif kind == "audience":
        claims["aud"] = "family"
    elif kind == "unknown":
        claims["sub"] = "unknown"
    elif kind == "disabled":
        device.is_active = False
        db.commit()
    elif kind == "revoked":
        service.revoke_device(db, device.device_id)
    else:
        service.rotate_token(db, device.device_id)
    invalid = jwt.encode(claims, key, algorithm="HS256")
    create = Mock(side_effect=AssertionError("unauthorized write"))
    tasks = Mock(side_effect=AssertionError("unauthorized task lookup"))
    lookup = Mock(side_effect=AssertionError("unauthorized target lookup"))
    notify = AsyncMock()
    monkeypatch.setattr(edge.storage, "create_alert", create)
    monkeypatch.setattr(edge.storage, "list_tasks_by_elder", tasks)
    monkeypatch.setattr(edge.storage, "get_elder", lookup)
    monkeypatch.setattr(manager, "push_to_user", notify)
    from app.models.models import Elder
    from sqlalchemy.orm import Session
    original_get = Session.get
    elder_reads = []

    def checked_get(session, entity, ident, **kwargs):
        """Invalid credentials must fail before loading any elder privacy row."""
        if entity is Elder:
            elder_reads.append(ident)
        return original_get(session, entity, ident, **kwargs)

    monkeypatch.setattr(Session, "get", checked_get)
    response = request(client, route, elder.id, invalid)
    assert response.status_code == 401
    assert not bool(invalid in response.text)
    create.assert_not_called()
    tasks.assert_not_called()
    lookup.assert_not_called()
    notify.assert_not_called()
    assert elder_reads == []
    assert db.query(AlertLog).count() == 0


def test_missing_configuration_and_registry_fail_closed(client, identity, monkeypatch):
    from app.core.config import settings
    from app.models.models import Device
    db, device, elder = identity
    token = service.issue_token(db, device.device_id)
    with monkeypatch.context() as patch:
        patch.setattr(settings, "EDGE_JWT_SECRET", "")
        assert request(client, "privacy", elder.id, token).status_code == 503
    Device.__table__.drop(db.get_bind())
    assert request(client, "privacy", elder.id, token).status_code == 503
