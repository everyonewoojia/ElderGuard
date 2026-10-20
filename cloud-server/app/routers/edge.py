"""P6 老人端设备接入路由（端云集成闭环）。

路径（前缀 /api/v1/edge，老人端边缘设备调用，非家属端）：
- POST /alerts      老人端上报告警事件 → 写入告警日志（FR-SC-05）
- GET  /tasks       老人端拉取该老人的激活提醒任务（FR-FL-06）
- GET  /settings    老人端拉取老人设置（FR-FL-08）
- GET  /privacy     老人端拉取隐私授权状态（FR-CM-05，授权联动）

返回统一契约 {code, message, data}。老人端仅能操作自身 elder_id 数据（按 elder 归属）。
"""
from typing import List, Optional, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.models import Device
from app.routers.edge_deps import get_current_device, require_bound_elder
from app.services import storage, edge_alerts

router = APIRouter()


@router.get("/alerts/receipts/{event_id}")
async def query_alert_receipt(event_id: str, elder_id: int, db: Session = Depends(get_db),
                              device: Device = Depends(get_current_device)):
    """Read only the authenticated device namespace, after current binding checks."""
    elder = require_bound_elder(db, device, elder_id)
    try:
        if str(UUID(event_id)) != event_id:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="事件 ID 格式无效") from None
    try:
        receipt = edge_alerts._lookup_receipt(db, device.device_id, event_id)
        if receipt is None:
            data = {"event_id": event_id, "device_id": device.device_id, "receipt_status": "not_found"}
        else:
            if receipt.elder_id != elder.id:
                raise HTTPException(status_code=409, detail="接收记录绑定不一致")
            alert = edge_alerts._existing_alert(db, receipt, elder.id, receipt.payload_hash)[0]
            data = {"event_id": receipt.event_id, "device_id": receipt.device_id,
                    "alert_id": receipt.alert_id, "receipt_status": "received", "payload_hash": receipt.payload_hash,
                    "status": alert.status, "notification": receipt.notification_state,
                    "notification_attempts": receipt.notification_attempts,
                    "notification_send_count": receipt.notification_send_count,
                    "notification_last_result": receipt.notification_last_result, "confirmation": "unconfirmed"}
        return {"code": 200, "message": "success", "data": data}
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="接收记录查询暂不可用") from None


class EdgeAlertIn(BaseModel):
    """老人端上报的告警事件（标准事件 JSON 的子集）"""
    elder_id: int
    event_id: str
    alert_type: Literal["help", "fall", "abnormal"]
    priority: Literal["P0", "P1", "P2"] = "P1"
    content: Optional[str] = Field(default=None, max_length=4096)

    @field_validator("event_id")
    @classmethod
    def canonical_event_id(cls, value: str) -> str:
        """Require an existing canonical UUID; do not generate a server fallback."""
        if str(UUID(value)) != value:
            raise ValueError("event_id must be a canonical UUID")
        return value


@router.post("/alerts")
async def receive_alert(data: EdgeAlertIn, db: Session = Depends(get_db),
                        device: Device = Depends(get_current_device)):
    """同事务保存告警与待发任务；实际通知由生命周期工作者处理。"""
    elder = require_bound_elder(db, device, data.elder_id)
    alert, receipt, _created = edge_alerts.accept_alert(db, device, elder, data)
    return {
        "code": 200,
        "message": "告警已上报",
        "data": {"alert_id": alert.id, "status": alert.status,
                 "event_id": receipt.event_id, "device_id": receipt.device_id,
                 "receipt_status": "received", "notification": receipt.notification_state,
                 "notification_attempts": receipt.notification_attempts,
                 "notification_send_count": receipt.notification_send_count,
                 "notification_last_result": receipt.notification_last_result,
                 "confirmation": "unconfirmed"},
    }


@router.get("/tasks")
async def edge_tasks(elder_id: int, db: Session = Depends(get_db),
                     device: Device = Depends(get_current_device)):
    """老人端拉取激活提醒任务（is_active=True）。"""
    elder = require_bound_elder(db, device, elder_id)
    tasks = storage.list_tasks_by_elder(db, elder.id)
    active = [t for t in tasks if t.is_active]
    data: List[dict] = [
        {
            "id": t.id,
            "task_type": t.task_type,
            "title": t.title,
            "content": t.content,
            "schedule_time": t.schedule_time,
        }
        for t in active
    ]
    return {"code": 200, "message": "success", "data": data}


@router.get("/settings")
async def edge_settings(elder_id: int, db: Session = Depends(get_db),
                        device: Device = Depends(get_current_device)):
    """老人端拉取老人设置（含隐私授权开关）。"""
    elder = require_bound_elder(db, device, elder_id)
    return {
        "code": 200,
        "message": "success",
        "data": {"elder_id": elder.id, "privacy_enabled": elder.privacy_enabled},
    }


@router.get("/privacy")
async def edge_privacy(elder_id: int, db: Session = Depends(get_db),
                       device: Device = Depends(get_current_device)):
    """老人端拉取隐私授权状态（FR-CM-05 授权联动）。"""
    elder = require_bound_elder(db, device, elder_id)
    return {
        "code": 200,
        "message": "success",
        "data": {"elder_id": elder.id, "enabled": elder.privacy_enabled},
    }
