"""云端鉴权依赖与行级隔离（P2，M-APP-01）。

提供可复用的鉴权依赖：
- get_current_user：解析 Bearer token → 当前用户（无效/过期 → 401）
- get_current_family：校验家属角色（非家属 → 403）
- get_bound_elder：校验 elder 归属（不存在 → 404；非本人绑定 → 403 越权）

供各受保护接口统一挂载，落实 AQ01（越权访问 0 次）。
"""
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.models import User, Elder
from app.services.auth import decode_token
from app.services.storage import get_elder

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """解析 token 并返回当前登录用户；无效 / 过期 / 用户不存在 → 401。"""
    payload = decode_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="无效的令牌")
    user = db.query(User).filter(User.username == payload.get("sub")).first()
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在或令牌无效")
    return user


def get_current_family(current_user: User = Depends(get_current_user)) -> User:
    """校验当前用户为家属角色；非家属 → 403。"""
    if current_user.role != "family":
        raise HTTPException(status_code=403, detail="非家属账号无权访问")
    return current_user


def get_bound_elder(
    elder_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_family),
) -> Elder:
    """校验 elder 归属：不存在 → 404；非当前家属绑定 → 403（越权隔离）。"""
    elder = get_elder(db, elder_id)
    if elder is None:
        raise HTTPException(status_code=404, detail="老人不存在")
    if elder.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权访问该老人数据")
    return elder
