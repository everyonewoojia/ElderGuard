"""P6 老人端设备接入路由（端云集成闭环）。

路径（前缀 /api/v1/edge，老人端边缘设备调用，非家属端）：
- POST /alerts      老人端上报告警事件 → 写入告警日志（FR-SC-05）
- GET  /tasks       老人端拉取该老人的激活提醒任务（FR-FL-06）
- GET  /settings    老人端拉取老人设置（FR-FL-08）
- GET  /privacy     老人端拉取隐私授权状态（FR-CM-05，授权联动）

返回统一契约 {code, message, data}。老人端仅能操作自身 elder_id 数据（按 elder 归属）。
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services import storage

router = APIRouter()


class EdgeAlertIn(BaseModel):
    """老人端上报的告警事件（标准事件 JSON 的子集）"""
    elder_id: int
    alert_type: str
    priority: str = "P1"
    content: Optional[str] = None


@router.post("/alerts")
async def receive_alert(data: EdgeAlertIn, db: Session = Depends(get_db)):
    """老人端上报告警：写入告警日志（初始 pending），供家属端查询。"""
    elder = storage.get_elder(db, data.elder_id)
    if elder is None:
        raise HTTPException(status_code=404, detail="老人不存在")
    alert = storage.create_alert(
        db, data.elder_id, data.alert_type, data.priority, data.content
    )
    return {
        "code": 200,
        "message": "告警已上报",
        "data": {"alert_id": alert.id, "status": alert.status},
    }


@router.get("/tasks")
async def edge_tasks(elder_id: int, db: Session = Depends(get_db)):
    """老人端拉取激活提醒任务（is_active=True）。"""
    elder = storage.get_elder(db, elder_id)
    if elder is None:
        raise HTTPException(status_code=404, detail="老人不存在")
    tasks = storage.list_tasks_by_elder(db, elder_id)
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
async def edge_settings(elder_id: int, db: Session = Depends(get_db)):
    """老人端拉取老人设置（含隐私授权开关）。"""
    elder = storage.get_elder(db, elder_id)
    if elder is None:
        raise HTTPException(status_code=404, detail="老人不存在")
    return {
        "code": 200,
        "message": "success",
        "data": {"elder_id": elder.id, "privacy_enabled": elder.privacy_enabled},
    }


@router.get("/privacy")
async def edge_privacy(elder_id: int, db: Session = Depends(get_db)):
    """老人端拉取隐私授权状态（FR-CM-05 授权联动）。"""
    elder = storage.get_elder(db, elder_id)
    if elder is None:
        raise HTTPException(status_code=404, detail="老人不存在")
    return {
        "code": 200,
        "message": "success",
        "data": {"elder_id": elder.id, "enabled": elder.privacy_enabled},
    }
