"""WebSocket 实时告警推送（M-ACC-03 / P8-1）。

路径：GET /api/v1/ws/alerts?token=<JWT>
- 家属鉴权（query token，无效/缺失 → 关闭 4401）
- 连接时补推该家属未确认（pending）告警
- 实时收到新告警时，edge 上报触发 manager.push_to_user 推送

P8-5：补推仅查 pending（list_pending_alerts_by_owner），已取消/已通知的告警不再推送。
"""
import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.models import User
from app.services import storage
from app.services.auth import decode_token
from app.services.ws_manager import manager

logger = logging.getLogger(__name__)

router = APIRouter()


def _resolve_user(token: Optional[str]) -> Optional[User]:
    """从 JWT 解析当前家属用户（无效/过期/不存在 → None）。"""
    if not token:
        return None
    payload = decode_token(token)
    if payload is None:
        return None
    db: Session = SessionLocal()
    try:
        return storage.get_user_by_username(db, payload.get("sub"))
    finally:
        db.close()


def serialize_alert(alert) -> dict:
    """告警记录 → 推送 JSON。"""
    return {
        "type": "alert",
        "data": {
            "id": alert.id,
            "elder_id": alert.elder_id,
            "alert_type": alert.alert_type,
            "priority": alert.priority,
            "content": alert.content,
            "status": alert.status,
            "created_at": alert.created_at.isoformat() if alert.created_at else None,
        },
    }


@router.websocket("/ws/alerts")
async def ws_alerts(websocket: WebSocket):
    """家属实时告警推送：鉴权 → 补推未确认 → 持续接收心跳/应答。"""
    token = websocket.query_params.get("token")
    user = _resolve_user(token)
    if user is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    await manager.connect(user.id, websocket)
    # 补推未确认（pending）告警（P8-5：不含已取消/已通知）
    db: Session = SessionLocal()
    try:
        for alert in storage.list_pending_alerts_by_owner(db, user.id):
            await websocket.send_json(serialize_alert(alert))
    finally:
        db.close()
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(user.id, websocket)
    except Exception:  # noqa: BLE001 - 连接异常按断开处理
        await manager.disconnect(user.id, websocket)
