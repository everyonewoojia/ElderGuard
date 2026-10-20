from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Sign with an explicitly validated family key, never the device key."""
    key = settings.family_signing_key()
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, key, algorithm=settings.ALGORITHM)
    return encoded_jwt


def decode_token(token: str) -> Optional[dict]:
    """Require a current identity and expiry; configuration failures stay distinct."""
    key = settings.family_signing_key()
    try:
        payload = jwt.decode(token, key, algorithms=[settings.ALGORITHM],
                             options={"require_exp": True, "require_sub": True})
        if (not isinstance(payload.get("sub"), str) or not payload["sub"]
                or type(payload.get("exp")) is not int
                or payload["exp"] <= datetime.now(timezone.utc).timestamp()):
            return None
        return payload
    except (JWTError, ValueError, TypeError):
        return None
