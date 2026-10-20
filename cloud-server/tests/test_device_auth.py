"""Device identity and local administration tests on the registered test database."""
import secrets
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from jose import jwt
from sqlalchemy import inspect

from app.core.config import settings
from app.models.models import Device, Elder, User
from app.services import device_auth as service


@pytest.fixture()
def device_key(monkeypatch):
    """Never inherit a deployment device key."""
    monkeypatch.setattr(settings, "EDGE_JWT_SECRET", secrets.token_urlsafe(48))


@pytest.fixture()
def identity(db, device_key):
    """Create a family and elder without external services."""
    owner = User(username="device-owner", password_hash="unused", role="family")
    db.add(owner)
    db.flush()
    elder = Elder(name="test elder", owner_id=owner.id)
    db.add(elder)
    db.commit()
    device = service.register_device(db, elder.id)
    return db, device, elder


def test_registration_reads_actual_owner(identity):
    db, device, elder = identity
    assert device.owner_id == elder.owner_id
    assert device.elder_id == elder.id
    assert device.is_active and device.credential_version == 1
    assert device.created_at is not None
    with pytest.raises(service.DeviceAuthError):
        service.register_device(db, 99999)
    assert db.query(Device).count() == 1


def test_issue_rotate_revoke(identity):
    db, device, _ = identity
    first = service.issue_token(db, device.device_id)
    assert service.authenticate_device(db, first).device_id == device.device_id
    second = service.rotate_token(db, device.device_id)
    with pytest.raises(service.DeviceAuthError):
        service.authenticate_device(db, first)
    assert service.authenticate_device(db, second).credential_version == 2
    service.revoke_device(db, device.device_id)
    with pytest.raises(service.DeviceAuthError):
        service.authenticate_device(db, second)


@pytest.mark.parametrize("change", ["expired", "audience", "purpose", "issuer", "signature", "missing_exp", "version_type"])
def test_invalid_claims(identity, change):
    db, device, _ = identity
    token = service.issue_token(db, device.device_id)
    claims = jwt.get_unverified_claims(token)
    key = settings.EDGE_JWT_SECRET
    if change == "expired":
        claims["exp"] = int((datetime.now(timezone.utc) - timedelta(seconds=10)).timestamp())
    elif change == "audience":
        claims["aud"] = "family"
    elif change == "purpose":
        claims["purpose"] = "family"
    elif change == "issuer":
        claims["iss"] = "other"
    elif change == "signature":
        key = secrets.token_urlsafe(48)
    elif change == "missing_exp":
        del claims["exp"]
    else:
        claims["version"] = True
    invalid = jwt.encode(claims, key, algorithm="HS256")
    with pytest.raises(service.DeviceAuthError):
        service.authenticate_device(db, invalid)


@pytest.mark.parametrize("key", ["", "short", None])
def test_bad_key_fails_closed(identity, monkeypatch, key):
    db, device, _ = identity
    monkeypatch.setattr(settings, "EDGE_JWT_SECRET", key)
    with pytest.raises(service.DeviceConfigurationError):
        service.issue_token(db, device.device_id)


def test_family_key_cannot_be_reused(identity, monkeypatch):
    db, device, _ = identity
    key = secrets.token_urlsafe(48)
    monkeypatch.setattr(settings, "SECRET_KEY", key)
    monkeypatch.setattr(settings, "EDGE_JWT_SECRET", key)
    with pytest.raises(service.DeviceConfigurationError):
        service.issue_token(db, device.device_id)


def test_disabled_unknown_and_transferred_binding(identity):
    db, device, elder = identity
    token = service.issue_token(db, device.device_id)
    device.is_active = False
    db.commit()
    with pytest.raises(service.DeviceAuthError):
        service.authenticate_device(db, token)
    device.is_active = True
    elder.owner_id = None
    db.commit()
    with pytest.raises(service.DeviceAuthError):
        service.authenticate_device(db, token)
    claims = jwt.get_unverified_claims(token)
    claims["sub"] = "unknown-device"
    unknown = jwt.encode(claims, settings.EDGE_JWT_SECRET, algorithm="HS256")
    with pytest.raises(service.DeviceAuthError):
        service.authenticate_device(db, unknown)


def test_rotation_publication_failure_preserves_old_token(identity):
    db, device, _ = identity
    token = service.issue_token(db, device.device_id)
    publish = Mock(side_effect=OSError("controlled output failure"))
    with pytest.raises(service.DeviceAuthError):
        service.rotate_token(db, device.device_id, publish=publish)
    assert publish.call_count == 1
    assert service.authenticate_device(db, token).credential_version == 1


def test_rotation_commit_failure_rolls_back(identity, monkeypatch):
    db, device, _ = identity
    token = service.issue_token(db, device.device_id)
    with monkeypatch.context() as patch:
        patch.setattr(db, "commit", Mock(side_effect=RuntimeError("commit failure")))
        with pytest.raises(service.DeviceAuthError):
            service.rotate_token(db, device.device_id)
    assert service.authenticate_device(db, token).credential_version == 1


def test_explicit_initialization_only_creates_device_table(identity, test_database):
    db, device, elder = identity
    engine = db.get_bind()
    before = set(inspect(engine).get_table_names())
    # Only the newly introduced table is removed to simulate an existing deployment.
    Device.__table__.drop(engine)
    service.initialize_device_table(engine, test_database.path)
    assert set(inspect(engine).get_table_names()) == before
    assert db.get(Elder, elder.id).name == "test elder"
    assert db.query(User).count() == 1


def test_initialization_rejects_wrong_path_before_connect(identity, monkeypatch):
    db, _, _ = identity
    engine = db.get_bind()
    connect = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(engine, "connect", connect)
    with pytest.raises(service.DeviceConfigurationError):
        service.initialize_device_table(engine, "unregistered.sqlite")
    connect.assert_not_called()


def test_wrong_engine_is_rejected_before_connect(identity, test_database):
    """A mock engine cannot impersonate the registered application engine."""
    db, _, _ = identity
    engine = Mock(url=db.get_bind().url)
    with pytest.raises(service.DeviceConfigurationError):
        service.initialize_device_table(engine, test_database.path)
    engine.begin.assert_not_called()
    engine.connect.assert_not_called()


def test_local_management_commands(identity, test_database, capsys):
    """Real CLI dispatch uses the same factory, and prints no token."""
    from scripts.manage_edge_devices import main
    db, _, elder = identity
    base = ["--database", str(test_database.path)]
    assert main([*base, "register", "--elder-id", str(elder.id)]) == 0
    registered = db.query(Device).filter(Device.device_id != identity[1].device_id).one()
    token_file = test_database.root / "issued.credential"
    assert main([*base, "issue", "--device-id", registered.device_id,
                 "--output", str(token_file)]) == 0
    token = token_file.read_text(encoding="utf-8")
    assert service.authenticate_device(db, token).elder_id == elder.id
    rotated_file = test_database.root / "rotated.credential"
    assert main([*base, "rotate", "--device-id", registered.device_id,
                 "--output", str(rotated_file)]) == 0
    with pytest.raises(service.DeviceAuthError):
        service.authenticate_device(db, token)
    rotated = rotated_file.read_text(encoding="utf-8")
    assert service.authenticate_device(db, rotated).credential_version == 2
    assert main([*base, "revoke", "--device-id", registered.device_id]) == 0
    with pytest.raises(service.DeviceAuthError):
        service.authenticate_device(db, rotated)
    output = capsys.readouterr()
    assert not any(value in output.out + output.err for value in (token, rotated))


def test_cli_output_failure_does_not_rotate_or_overwrite(identity, test_database):
    from scripts.manage_edge_devices import main
    db, device, _ = identity
    token = service.issue_token(db, device.device_id)
    output = test_database.root / "occupied.credential"
    output.write_text("owned sentinel", encoding="utf-8")
    assert main(["--database", str(test_database.path), "rotate",
                 "--device-id", device.device_id, "--output", str(output)]) == 1
    assert output.read_text(encoding="utf-8") == "owned sentinel"
    assert service.authenticate_device(db, token).credential_version == 1


def test_cli_bad_target_never_opens_session(identity, monkeypatch):
    from scripts.manage_edge_devices import main
    from app.core import database
    factory = Mock(side_effect=AssertionError("must not open session"))
    monkeypatch.setattr(database, "SessionLocal", factory)
    assert main(["--database", "unverified.sqlite", "register", "--elder-id", "1"]) == 1
    factory.assert_not_called()


def test_failed_commit_output_is_recoverable(identity, test_database, monkeypatch):
    from scripts.manage_edge_devices import write_token
    db, device, _ = identity
    previous = service.issue_token(db, device.device_id)
    output = test_database.root / "recoverable.credential"
    with monkeypatch.context() as patch:
        patch.setattr(db, "commit", Mock(side_effect=RuntimeError("controlled commit failure")))
        with pytest.raises(service.DeviceAuthError):
            service.rotate_token(db, device.device_id, publish=lambda token: write_token(output, token))
    assert output.is_file()
    assert service.authenticate_device(db, previous).credential_version == 1
    with pytest.raises(service.DeviceAuthError):
        service.authenticate_device(db, output.read_text(encoding="utf-8"))
    # Recovery does not require enrollment or discarding the retained file.
    recovered = service.issue_token(db, device.device_id)
    assert service.authenticate_device(db, recovered).credential_version == 1


def test_registry_missing_is_configuration_failure(identity):
    db, device, _ = identity
    token = service.issue_token(db, device.device_id)
    Device.__table__.drop(db.get_bind())
    with pytest.raises(service.DeviceConfigurationError):
        service.authenticate_device(db, token)


def test_enrollment_commit_failure_leaves_no_record(identity, monkeypatch):
    db, _, elder = identity
    count = db.query(Device).count()
    with monkeypatch.context() as patch:
        patch.setattr(db, "commit", Mock(side_effect=RuntimeError("controlled failure")))
        with pytest.raises(service.DeviceAuthError):
            service.register_device(db, elder.id)
    assert db.query(Device).count() == count


def test_acl_failure_never_writes_token_or_rotates(identity, test_database, monkeypatch):
    from scripts import manage_edge_devices as command
    db, device, _ = identity
    token = service.issue_token(db, device.device_id)
    output = test_database.root / "permission-failure.credential"
    monkeypatch.setattr(command, "secure_output", Mock(side_effect=OSError("ACL unavailable")))
    assert command.main(["--database", str(test_database.path), "rotate",
                         "--device-id", device.device_id, "--output", str(output)]) == 1
    assert output.read_bytes() == b""
    assert service.authenticate_device(db, token).credential_version == 1
