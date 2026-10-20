"""P0 family authorization: registered SQLite fixtures and in-process ASGI only."""
import asyncio
from datetime import timedelta
import secrets
import time
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.exc import SQLAlchemyError
from starlette.websockets import WebSocketDisconnect
from starlette.datastructures import Headers

from app.core.config import Settings, settings
from app.core import database
from app.main import app
from app.models.models import User
from app.routers import ws as ws_router
from app.services.auth import create_access_token, decode_token
from app.services.ws_manager import manager


INVALID_KEYS = ["", "   ", "your-secret-key-change-in-production", "short-synthetic-key"]


@pytest.mark.parametrize("value", INVALID_KEYS)
def test_invalid_key_rejects_sign_and_verify_without_echo(monkeypatch, caplog, value):
    """Runtime changes must not bypass startup validation or expose input."""
    token = create_access_token({"sub": "synthetic-family"})
    monkeypatch.setattr(settings, "SECRET_KEY", value)
    for operation in (lambda: create_access_token({"sub": "synthetic-family"}), lambda: decode_token(token)):
        with pytest.raises(RuntimeError, match="SECRET_KEY") as failure:
            operation()
        if value.strip():
            assert value not in str(failure.value) + caplog.text


def test_missing_key_has_no_usable_default(monkeypatch):
    """Construct settings without opening any dotenv file or implicit secret."""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    isolated = Settings(_env_file=None)
    monkeypatch.setattr(settings, "SECRET_KEY", isolated.SECRET_KEY)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_access_token({"sub": "synthetic-family"})


@pytest.mark.parametrize("value", INVALID_KEYS)
def test_invalid_key_refuses_lifespan_before_worker(db_state, monkeypatch, value):
    """Use the real app initialization but never start a deployed service."""
    monkeypatch.setattr(settings, "SECRET_KEY", value)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        with TestClient(app):
            pass


def test_explicit_synthetic_key_and_rotation(monkeypatch):
    """Family rotation invalidates old tokens, with no device-key fallback."""
    device_key = settings.EDGE_JWT_SECRET
    monkeypatch.setattr(settings, "SECRET_KEY", secrets.token_urlsafe(48))
    token = create_access_token({"sub": "synthetic-family"})
    assert decode_token(token)["sub"] == "synthetic-family"
    monkeypatch.setattr(settings, "SECRET_KEY", secrets.token_urlsafe(48))
    assert decode_token(token) is None
    assert settings.EDGE_JWT_SECRET == device_key


def test_family_key_cannot_reuse_device_key(monkeypatch):
    """Independence is enforced in the family path too."""
    key = secrets.token_urlsafe(48)
    monkeypatch.setattr(settings, "SECRET_KEY", key)
    monkeypatch.setattr(settings, "EDGE_JWT_SECRET", key)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_access_token({"sub": "synthetic-family"})


@pytest.mark.parametrize("missing", ["sub", "exp"])
def test_family_token_requires_identity_and_expiry(missing):
    """A signed token without an expiry cannot maintain an endless WS grant."""
    claims = {"sub": "synthetic-family", "exp": int(time.time()) + 60}
    claims.pop(missing)
    token = jwt.encode(claims, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    assert decode_token(token) is None


@pytest.fixture
def family(auth, test_database):
    """Create only synthetic accounts in the existing protected fixture."""
    user = auth["register"]("p0-family", "13800006666")
    elder = auth["create_elder"]("p0-family", "synthetic")
    return auth["client"], user["id"], elder, auth["login"]("p0-family")


def change_user(owner, user_id, **changes):
    """Commit an account change through a registered, short-lived Session."""
    with owner.session() as session:
        user = session.get(User, user_id)
        for name, value in changes.items():
            setattr(user, name, value)
        session.commit()


def test_disabled_account_cannot_login(family, test_database):
    client, user_id, _, _ = family
    change_user(test_database, user_id, is_active=False)
    response = client.post("/api/v1/auth/login", data={"username": "p0-family", "password": "pw123456"})
    assert response.status_code == 401
    assert "access_token" not in response.json()


@pytest.mark.parametrize("route", ["/auth/me", "/elder", "/tasks", "/alerts", "/elder/{elder}/logs"])
def test_disabled_account_old_token_refused_by_shared_rest_dependency(family, test_database, route):
    client, user_id, elder, token = family
    change_user(test_database, user_id, is_active=False)
    response = client.get("/api/v1" + route.format(elder=elder), headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_disabled_account_cannot_change_privacy(family, test_database):
    client, user_id, elder, token = family
    change_user(test_database, user_id, is_active=False)
    response = client.post("/api/v1/privacy/auth", json={"elder_id": elder, "enabled": True},
                           headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


@pytest.mark.parametrize("change", [{"is_active": False}, {"role": "admin"}])
def test_ws_handshake_rejects_current_account_state(family, test_database, change):
    client, user_id, _, token = family
    change_user(test_database, user_id, **change)
    with pytest.raises(WebSocketDisconnect) as failure:
        with client.websocket_connect(f"/api/v1/ws/alerts?token={token}"):
            pytest.fail("Invalid account was accepted")
    assert failure.value.code == 4401
    assert user_id not in manager.online_users()


@pytest.mark.parametrize("change", ["disable", "role", "expire", "database"])
def test_real_ws_route_rechecks_idle_connection_and_releases_resources(family, test_database, monkeypatch, change):
    """Exercise the actual route, including idle receive and close lifecycle."""
    client, user_id, _, token = family
    monkeypatch.setattr(ws_router, "RECHECK_INTERVAL", 0.05, raising=False)
    if change == "expire":
        token = create_access_token({"sub": "p0-family"}, expires_delta=timedelta(seconds=2))
    started = time.monotonic()
    with client.websocket_connect(f"/api/v1/ws/alerts?token={token}") as socket:
        if change in ("disable", "role"):
            change_user(test_database, user_id, **({"is_active": False} if change == "disable" else {"role": "admin"}))
        elif change == "database":
            monkeypatch.setattr(ws_router.storage, "get_user_by_username",
                                Mock(side_effect=SQLAlchemyError("synthetic-private-error")))
        # A bounded receive prevents a broken implementation hanging the suite.
        # TestClient.receive() returns the ASGI close frame without raising.
        future = socket.portal.start_task_soon(socket._send_rx.receive)
        try:
            message = future.result(timeout=4)
            assert message["type"] == "websocket.close" and message["code"] == 4401
        finally:
            future.cancel()
    assert time.monotonic() - started < 4
    assert user_id not in manager.online_users()


@pytest.mark.parametrize("change", ["disable", "role", "expire", "binding", "database"])
def test_ws_worker_send_gate_refuses_new_sensitive_data(family, test_database, monkeypatch, change):
    """The worker's actual manager path must reauthorize before sending."""
    client, user_id, elder, token = family
    monkeypatch.setattr(ws_router, "RECHECK_INTERVAL", 60, raising=False)
    with client.websocket_connect(f"/api/v1/ws/alerts?token={token}") as socket:
        if change in ("disable", "role"):
            change_user(test_database, user_id, **({"is_active": False} if change == "disable" else {"role": "admin"}))
        elif change == "expire":
            from app.services import auth as auth_service
            def expired(*args, **kwargs):
                """Simulate signature verification observing token expiration."""
                from jose import ExpiredSignatureError
                raise ExpiredSignatureError("synthetic expired")

            monkeypatch.setattr(auth_service.jwt, "decode", expired)
        elif change == "database":
            monkeypatch.setattr(ws_router.storage, "get_user_by_username",
                                Mock(side_effect=SQLAlchemyError("synthetic")))
        payload = {"type": "alert", "data": {"elder_id": elder if change != "binding" else elder + 1000,
                                                "content": "synthetic-sensitive-data"}}
        assert client.portal.call(manager.push_to_user, user_id, payload) == 0
        message = socket.receive()
        assert message["type"] == "websocket.close" and message["code"] == 4401
    assert user_id not in manager.online_users()


async def test_ws_route_cancellation_releases_registration_and_sessions(family):
    """Cancel the route in its receive scope with real account authorization."""
    client, user_id, _, token = family
    client.portal.call(app.state.delivery_worker.stop)
    waiting = asyncio.Event()

    async def receive():
        """Suspend without holding a database connection."""
        assert database.engine.pool.checkedout() == 0
        waiting.set()
        await asyncio.Event().wait()

    socket = Mock(headers=Headers(), query_params={"token": token}, accept=AsyncMock(), close=AsyncMock(),
                  receive_text=AsyncMock(side_effect=receive))
    task = asyncio.create_task(ws_router.ws_alerts(socket))
    await asyncio.wait_for(waiting.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert user_id not in manager.online_users()
    assert database.engine.pool.checkedout() == 0


def test_ws_revocation_is_not_blocked_by_cooperative_stalled_send(family, test_database, monkeypatch):
    """Real WS routing must bound a stalled send before the idle close check."""
    client, user_id, elder, token = family
    monkeypatch.setattr(ws_router, "RECHECK_INTERVAL", 0.05)
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def stalled(socket, payload):
        """No SQL may remain checked out while a transport awaits."""
        assert database.engine.pool.checkedout() == 0
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(ws_router.WebSocket, "send_json", stalled)
    with client.websocket_connect(f"/api/v1/ws/alerts?token={token}") as socket:
        sending = client.portal.start_task_soon(manager.push_to_user, user_id,
            {"type": "alert", "data": {"elder_id": elder, "content": "synthetic"}})
        client.portal.call(asyncio.wait_for, entered.wait(), 2)
        change_user(test_database, user_id, is_active=False)
        receiving = socket.portal.start_task_soon(socket._send_rx.receive)
        try:
            message = receiving.result(timeout=3)
            assert message["type"] == "websocket.close" and message["code"] == 4401
            assert sending.result(timeout=1) == 0 and cleaned.is_set()
        finally:
            sending.cancel()
            receiving.cancel()
    assert user_id not in manager.online_users()
