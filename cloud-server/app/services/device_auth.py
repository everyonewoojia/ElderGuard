"""Independent device JWTs and trusted local enrollment; no startup DDL."""
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from jose import JWTError, jwt
from sqlalchemy import inspect, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core import database
from app.models.models import Device, Elder, User

ALGORITHM = "HS256"
PURPOSE = "edge-device"


class DeviceAuthError(Exception):
    """A deliberately credential-free authentication or management failure."""


class DeviceConfigurationError(DeviceAuthError):
    """Device service is not safely configured or initialized."""


def _key() -> str:
    """Require a separately generated URL-safe secret of at least 256 bits."""
    key = settings.EDGE_JWT_SECRET
    if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,}", key)
            or len(set(key)) < 16 or key == settings.SECRET_KEY
            or not settings.EDGE_JWT_ISSUER or not settings.EDGE_JWT_AUDIENCE
            or type(settings.EDGE_JWT_EXPIRE_MINUTES) is not int
            or not 1 <= settings.EDGE_JWT_EXPIRE_MINUTES <= 43200):
        raise DeviceConfigurationError("Device authentication configuration unavailable")
    return key


def _bound_device(db: Session, device_id: str, version: int | None = None) -> Device:
    """Validate the registered device and its current elder/family ownership."""
    try:
        device = db.get(Device, device_id, populate_existing=True)
        if device is None or not device.is_active:
            raise DeviceAuthError("Invalid device credentials")
        if version is not None and device.credential_version != version:
            raise DeviceAuthError("Invalid device credentials")
        elder = db.get(Elder, device.elder_id, populate_existing=True)
        owner = db.get(User, device.owner_id, populate_existing=True)
        if (elder is None or elder.owner_id != device.owner_id or owner is None
                or owner.role != "family" or not owner.is_active):
            raise DeviceAuthError("Invalid device binding")
        return device
    except SQLAlchemyError:
        db.rollback()
        raise DeviceConfigurationError("Device registry unavailable") from None


def _encode(device: Device, version: int) -> str:
    """Sign only device identity, never a caller-supplied elder authorization."""
    key = _key()
    now = int(datetime.now(timezone.utc).timestamp())
    return jwt.encode({
        "sub": device.device_id, "version": version, "purpose": PURPOSE,
        "iss": settings.EDGE_JWT_ISSUER, "aud": settings.EDGE_JWT_AUDIENCE,
        "iat": now, "exp": now + settings.EDGE_JWT_EXPIRE_MINUTES * 60,
        "jti": secrets.token_urlsafe(24),
    }, key, algorithm=ALGORITHM)


def authenticate_device(db: Session, token: str) -> Device:
    """Check signature and required claims before any registry lookup."""
    key = _key()
    try:
        claims = jwt.decode(token, key, algorithms=[ALGORITHM],
                            audience=settings.EDGE_JWT_AUDIENCE,
                            issuer=settings.EDGE_JWT_ISSUER,
                            options={"require_exp": True, "require_iat": True,
                                     "require_sub": True, "require_aud": True,
                                     "require_iss": True, "require_jti": True})
        now = int(datetime.now(timezone.utc).timestamp())
        if (claims.get("purpose") != PURPOSE
                or claims.get("aud") != settings.EDGE_JWT_AUDIENCE
                or type(claims.get("version")) is not int or claims["version"] < 1
                or type(claims.get("iat")) is not int
                or type(claims.get("exp")) is not int
                or claims["iat"] > now or claims["exp"] <= now
                or claims["exp"] <= claims["iat"]
                or claims["exp"] - claims["iat"] > 43200 * 60):
            raise DeviceAuthError("Invalid device credentials")
    except (JWTError, ValueError, TypeError, KeyError):
        raise DeviceAuthError("Invalid device credentials") from None
    return _bound_device(db, claims["sub"], claims["version"])


def register_device(db: Session, elder_id: int) -> Device:
    """Trusted local operation: derive owner exclusively from the database."""
    _key()
    try:
        elder = db.get(Elder, elder_id)
        owner = db.get(User, elder.owner_id) if elder is not None else None
        if owner is None or owner.role != "family" or not owner.is_active:
            raise DeviceAuthError("Elder has no active family binding")
        device = Device(device_id=secrets.token_urlsafe(24), elder_id=elder.id,
                        owner_id=owner.id, is_active=True, credential_version=1)
        db.add(device)
        db.commit()
        return device
    except Exception:
        db.rollback()
        raise DeviceAuthError("Device enrollment failed") from None


def issue_token(db: Session, device_id: str,
                publish: Callable[[str], None] | None = None) -> str:
    """Issue without changing binding/version; caller must protect token output."""
    device = _bound_device(db, device_id)
    token = _encode(device, device.credential_version)
    if publish is not None:
        publish(token)
    return token


def rotate_token(db: Session, device_id: str,
                 publish: Callable[[str], None] | None = None) -> str:
    """Publish recoverable output before atomically replacing the credential version.

    Publication failure leaves the old version intact. Commit failure rolls back;
    an uncertain commit keeps the output file for administrator recovery. Reissuing
    the current version is always possible without re-enrollment.
    """
    _key()
    try:
        device = _bound_device(db, device_id)
        previous = device.credential_version
        token = _encode(device, previous + 1)
        if publish is not None:
            publish(token)
        result = db.execute(update(Device).where(
            Device.device_id == device_id, Device.credential_version == previous,
            Device.is_active.is_(True),
        ).values(credential_version=previous + 1))
        if result.rowcount != 1:
            raise DeviceAuthError("Concurrent credential change")
        db.commit()
        return token
    except Exception:
        db.rollback()
        raise DeviceAuthError("Rotation failed; verify registry and retained output before reissuing") from None


def revoke_device(db: Session, device_id: str) -> None:
    """Permanently disable this registration and invalidate its issued tokens."""
    try:
        device = db.get(Device, device_id)
        if device is None:
            raise DeviceAuthError("Unknown device")
        db.execute(update(Device).where(Device.device_id == device_id).values(
            is_active=False, credential_version=Device.credential_version + 1))
        db.commit()
    except Exception:
        db.rollback()
        raise DeviceAuthError("Device revocation failed") from None


def validate_database_target(engine: Engine, expected_path: str | Path) -> Path:
    """Before connecting, require the application engine and an explicit real file."""
    target = Path(expected_path)
    actual = Path(engine.url.database or "")
    if (engine is not database.engine or engine.url.drivername != "sqlite"
            or not target.is_absolute() or not target.is_file()
            or target.resolve() != target or target.is_symlink()
            or actual.resolve() != target
            or any(p.is_symlink() or p.is_junction() for p in [target, *target.parents])):
        raise DeviceConfigurationError("Database target not explicitly verified")
    return target


def initialize_device_table(engine: Engine, expected_path: str | Path) -> None:
    """Explicit additive DDL for the new table only, against an existing database."""
    validate_database_target(engine, expected_path)
    with engine.begin() as connection:
        existing = set(inspect(connection).get_table_names())
        if not {"users", "elders"}.issubset(existing):
            raise DeviceConfigurationError("Existing application schema required")
        Device.__table__.create(connection, checkfirst=True)
