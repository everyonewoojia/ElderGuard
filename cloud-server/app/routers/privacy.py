"""P3-4 隐私授权接口（家属端，FR-CM-05 / M-APP-01）。

路径（前缀 /api/v1/privacy）：
- POST /auth   设置/关闭老人隐私授权开关（仅本人老人，越权 → 403）

鉴权 + 行级隔离：统一挂载 get_current_family，并对 elder 做归属校验。
复用数据层 services/storage.py 的 update_elder_privacy。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.models import User
from app.routers.deps import get_current_family
from app.services import storage

router = APIRouter()


class PrivacyUpdate(BaseModel):
    elder_id: int
    enabled: bool


@router.post("/auth")
async def update_privacy(
    data: PrivacyUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_family),
):
    """更新老人隐私授权开关：不存在 → 404；非本人绑定 → 403（越权 0 次）。"""
    elder = storage.get_elder(db, data.elder_id)
    if elder is None:
        raise HTTPException(status_code=404, detail="老人不存在")
    if elder.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权访问该老人数据")
    storage.update_elder_privacy(db, data.elder_id, data.enabled)
    return {"message": "隐私授权已更新", "enabled": data.enabled}
