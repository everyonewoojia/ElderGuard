"""Device-only authentication, distinct from family OAuth/JWT dependencies."""
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.models import Device, Elder
from app.services.device_auth import (
    DeviceAuthError, DeviceConfigurationError, authenticate_device,
)

device_bearer = HTTPBearer(auto_error=False)


def get_current_device(
    credentials: HTTPAuthorizationCredentials | None = Depends(device_bearer),
    db: Session = Depends(get_db),
) -> Device:
    """Authenticate before executing any edge business operation."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="需要设备身份认证")
    try:
        return authenticate_device(db, credentials.credentials)
    except DeviceConfigurationError:
        raise HTTPException(status_code=503, detail="设备认证服务不可用") from None
    except DeviceAuthError:
        raise HTTPException(status_code=401, detail="设备凭证无效") from None


def require_bound_elder(db: Session, device: Device, elder_id: int) -> Elder:
    """Reject foreign IDs before looking up the requested target."""
    if elder_id != device.elder_id:
        raise HTTPException(status_code=403, detail="无权访问该老人数据")
    elder = db.get(Elder, device.elder_id)
    if elder is None or elder.owner_id != device.owner_id:
        raise HTTPException(status_code=401, detail="设备绑定无效")
    return elder
