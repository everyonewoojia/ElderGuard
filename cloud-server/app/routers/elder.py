"""P3-5 老人信息与监护日志接口（家属端，M-APP-04 / FR-FL-02 / FR-SC-09）。

路径（前缀 /api/v1/elder）：
- GET /{elder_id}         查询老人信息（鉴权 + 归属校验，越权 → 403）
- GET /{elder_id}/logs    查询绑定老人的监护日志（鉴权 + 归属校验，越权 → 403）

统一复用 get_bound_elder 依赖落实行级隔离；日志查询复用 services/storage.py。
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.models import Elder
from app.routers.deps import get_bound_elder
from app.services import storage

router = APIRouter()


class ElderResponse(BaseModel):
    id: int
    name: str
    age: Optional[int] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    privacy_enabled: bool

    model_config = ConfigDict(from_attributes=True)


class LogResponse(BaseModel):
    id: int
    event_type: str
    content: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


@router.get("/{elder_id}", response_model=ElderResponse)
async def get_elder(elder: Elder = Depends(get_bound_elder)):
    """查询老人信息（需家属鉴权 + 归属校验，越权 → 403）"""
    return elder


@router.get("/{elder_id}/logs", response_model=List[LogResponse])
async def get_elder_logs(
    elder: Elder = Depends(get_bound_elder),
    db: Session = Depends(get_db),
):
    """查询绑定老人的监护日志（需鉴权 + 归属校验，越权 → 403）。"""
    return storage.list_logs_by_elder(db, elder.id)
