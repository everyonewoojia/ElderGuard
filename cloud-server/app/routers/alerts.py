"""P3-2 告警记录接口（家属端，M-APP-02 / FR-FL-03）。

路径（前缀 /api/v1/alerts）：
- GET    /api/v1/alerts           查询当前家属全部老人的告警（越权 0 次）
- GET    /api/v1/alerts/{id}      查看单条告警详情（仅本人老人）
- POST   /api/v1/alerts/{id}/cancel  误告警取消（保留日志，状态切 cancelled）

鉴权 + 行级隔离：统一挂载 get_current_family，并对 elder 做归属校验（越权 → 403）。
复用数据层 services/storage.py。
"""
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.models import User
from app.routers.deps import get_current_family
from app.services import storage

router = APIRouter()


class AlertResponse(BaseModel):
    id: int
    elder_id: int
    alert_type: str
    priority: str
    content: str | None = None
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


def _check_alert_owner(db: Session, alert_id: int, current_user: User):
    """校验告警存在且其老人归属当前家属：不存在 → 404；非本人绑定 → 403。"""
    alert = storage.get_alert(db, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    elder = storage.get_elder(db, alert.elder_id)
    if elder is None or elder.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权访问该告警数据")
    return alert


@router.get("", response_model=List[AlertResponse])
async def list_alerts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_family),
):
    """查询当前家属全部老人的告警记录（仅本人数据，越权 0 次）。"""
    return storage.list_alerts_by_owner(db, current_user.id)


@router.get("/{alert_id}", response_model=AlertResponse)
async def get_alert(
    alert_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_family),
):
    """查看单条告警详情（仅本人老人，越权 → 403）。"""
    return _check_alert_owner(db, alert_id, current_user)


@router.post("/{alert_id}/cancel")
async def cancel_alert(
    alert_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_family),
):
    """误告警手动取消：状态切 cancelled，日志保留（仅本人老人，越权 → 403）。"""
    _check_alert_owner(db, alert_id, current_user)
    storage.update_alert_status(db, alert_id, "cancelled")
    return {"message": "告警已取消"}
