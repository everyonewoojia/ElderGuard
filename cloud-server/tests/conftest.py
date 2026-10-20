"""Cloud test bootstrap: one owned SQLite file, checked before every schema reset."""
import atexit
import os
import secrets
from pathlib import Path
import sys
import warnings
from unittest.mock import patch

from pydantic_settings import DotEnvSettingsSource

# This must precede every application import and ordinary fixture execution.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_safety import TestDatabase

_TEST_DATABASE = TestDatabase()


def _close_test_resources():
    """Report retained paths without masking an original bootstrap exception."""
    try:
        _TEST_DATABASE.close()
    except Exception as exc:
        warnings.warn(
            f"Test resources retained at {_TEST_DATABASE._allocated_root}: "
            f"{_TEST_DATABASE.failure_reason or str(exc)}",
            RuntimeWarning,
        )
    finally:
        _TEST_DATABASE.restore_environment()


atexit.register(_close_test_resources)
os.environ["SECRET_KEY"] = secrets.token_urlsafe(48)
os.environ["EDGE_JWT_SECRET"] = secrets.token_urlsafe(48)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "elder-agent"))

try:
    import pytest
    from fastapi.testclient import TestClient
    # Never open the deployment .env, even when environment variables override it.
    with patch.object(DotEnvSettingsSource, "_read_env_files", return_value={}):
        from app.core import database
        from app.core.config import settings

    # Establish the known database boundary before importing remaining application code.
    _TEST_DATABASE.bind(database.engine, database.SessionLocal)
    _TEST_DATABASE.validate_application(settings, database, [])
    from app.main import app
    from app.models.models import User, Elder, AlertLog, MonitorLog, ReminderTask
    from app.routers import auth as auth_router, tasks, alerts, elder, privacy, edge, deps, ws, edge_deps

    _ROUTERS = [auth_router, tasks, alerts, elder, privacy, edge, deps, ws, edge_deps]
    _TEST_DATABASE.validate_application(settings, database, _ROUTERS)
except BaseException:
    _close_test_resources()
    atexit.unregister(_close_test_resources)
    raise


def _validate_application():
    """Check the live module objects before any fixture database work."""
    _TEST_DATABASE.validate_application(settings, database, _ROUTERS)


def pytest_unconfigure(config):
    """Also release resources on collection failure, before fixtures have run."""
    try:
        _close_test_resources()
    finally:
        atexit.unregister(_close_test_resources)


@pytest.fixture(scope="session")
def db_setup():
    """Own process-level bindings; table lifecycle belongs to db_state."""
    _validate_application()
    yield _TEST_DATABASE


@pytest.fixture(scope="session")
def test_database(db_setup):
    """Expose the registered owner for safety and backup tests."""
    return db_setup


@pytest.fixture()
def db_state(db_setup):
    """Create/drop tables once per test, including partially failed setup."""
    _validate_application()
    try:
        db_setup.schema(database.Base.metadata, "create_all")
        yield
    finally:
        _validate_application()
        db_setup.schema(database.Base.metadata, "drop_all")


@pytest.fixture()
def db(db_state, test_database):
    """A checked data-layer Session, closed before table cleanup."""
    with test_database.session() as session:
        yield session


@pytest.fixture()
def client(db_state, test_database):
    """Share the same factory with REST, WS and direct test sessions."""
    def override_get_db():
        """Open a request-local checked Session."""
        _validate_application()
        with test_database.session() as session:
            yield session

    dependency = database.get_db
    missing = object()
    previous = app.dependency_overrides.get(dependency, missing)
    app.dependency_overrides[dependency] = override_get_db
    try:
        with TestClient(app) as http:
            yield http
    finally:
        if previous is missing:
            app.dependency_overrides.pop(dependency, None)
        else:
            app.dependency_overrides[dependency] = previous


@pytest.fixture()
def client_clean(client):
    """Compatibility alias; requesting both clients does not reset twice."""
    return client


PASSWORD = "pw123456"


@pytest.fixture()
def device_credentials(db_state, test_database, monkeypatch):
    """Enroll only through checked Sessions, using a fresh test-only signing key."""
    from app.services import device_auth

    monkeypatch.setattr(settings, "EDGE_JWT_SECRET", secrets.token_urlsafe(48))

    def enroll(elder_id, token_file=None):
        """Return a device JWT, optionally publishing with the real local CLI writer."""
        _validate_application()
        publish = None
        if token_file is not None:
            from scripts.manage_edge_devices import write_token

            path = Path(token_file)
            if path.parent != test_database.root:
                raise ValueError("Credential output must belong to this test database sandbox")
            publish = lambda token: write_token(path, token)
        with test_database.session() as session:
            device = device_auth.register_device(session, elder_id)
            return device_auth.issue_token(session, device.device_id, publish=publish)

    return enroll


@pytest.fixture()
def auth(client_clean, test_database):
    """P3 接口测试公共辅助：注册/登录/直插数据，基于每测试清库的 client_clean"""
    client = client_clean

    def register(username, phone):
        r = client.post("/api/v1/auth/register", json={
            "username": username, "password": PASSWORD, "phone": phone,
        })
        assert r.status_code == 200, r.text
        return r.json()

    def login(username):
        r = client.post("/api/v1/auth/login", data={"username": username, "password": PASSWORD})
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    def create_elder(username, name, privacy_enabled=False):
        with test_database.session() as db:
            owner = db.query(User).filter(User.username == username).first()
            elder = Elder(owner_id=owner.id, name=name, privacy_enabled=privacy_enabled)
            db.add(elder)
            db.commit()
            db.refresh(elder)
            return elder.id

    def create_task(elder_id, title="服药", task_type="用药提醒"):
        with test_database.session() as db:
            task = ReminderTask(elder_id=elder_id, task_type=task_type, title=title, is_active=True)
            db.add(task)
            db.commit()
            db.refresh(task)
            return task.id

    def create_alert(elder_id, alert_type="fall", priority="P0", content="疑似跌倒"):
        with test_database.session() as db:
            alert = AlertLog(elder_id=elder_id, alert_type=alert_type, priority=priority, content=content, status="pending")
            db.add(alert)
            db.commit()
            db.refresh(alert)
            return alert.id

    def create_log(elder_id, event_type="voice", content="对话"):
        with test_database.session() as db:
            log = MonitorLog(elder_id=elder_id, event_type=event_type, content=content)
            db.add(log)
            db.commit()
            db.refresh(log)
            return log.id

    return {
        "client": client,
        "register": register,
        "login": login,
        "create_elder": create_elder,
        "create_task": create_task,
        "create_alert": create_alert,
        "create_log": create_log,
    }
