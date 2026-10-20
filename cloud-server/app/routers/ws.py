"""WebSocket 实时告警推送（M-ACC-03 / P8-1）。

路径：GET /api/v1/ws/alerts（Bearer Header；旧 query token 兼容）
- 家属鉴权（Bearer Header 或旧 query token，无效/歧义 → 关闭 4401）
- 连接时补推该家属未确认（pending）告警
- 新接收记录由持久通知工作者认领并推送，不在 HTTP 请求内发送

P8-5：补推仅查 pending（list_pending_alerts_by_owner），已取消/已通知的告警不再推送。
"""
import asyncio
import logging
import re
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import SessionLocal
from app.core.config import FamilyAuthConfigurationError
from app.models.models import User, EdgeAlertReceipt
from app.services import storage
from app.services.auth import decode_token
from app.services.ws_manager import manager

logger = logging.getLogger(__name__)

router = APIRouter()
RECHECK_INTERVAL = 1.0
SEND_TIMEOUT = 1.0


def _resolve_user(token: Optional[str]) -> Optional[User]:
    """从 JWT 解析当前家属用户（无效/过期/不存在 → None）。"""
    if not token:
        return None
    try:
        payload = decode_token(token)
        if payload is None:
            return None
        with SessionLocal() as db:
            user = storage.get_user_by_username(db, payload["sub"])
            if user is None or user.is_active is not True or user.role != "family":
                return None
            return user
    except (SQLAlchemyError, FamilyAuthConfigurationError):
        return None


class AuthorizedSocket:
    """A connection-local send gate; never retain a database Session or task."""

    def __init__(self, websocket: WebSocket, token: Optional[str], user_id: int) -> None:
        """Keep credentials private to authorization, outside payloads and logs."""
        self.websocket, self._token, self.user_id = websocket, token, user_id
        self.closed = False
        self._lock = asyncio.Lock()

    def _authorized(self, payload: Optional[dict] = None) -> bool:
        """Recheck current account and, for a send, the current elder binding."""
        try:
            user = _resolve_user(self._token)
            if user is None or user.id != self.user_id:
                return False
            if payload is not None:
                elder_id = payload["data"]["elder_id"]
                if type(elder_id) is not int:
                    return False
                with SessionLocal() as db:
                    elder = storage.get_elder(db, elder_id)
                    if elder is None or elder.owner_id != self.user_id:
                        return False
            return True
        except Exception:
            return False

    async def _revoke(self) -> None:
        """Remove send eligibility before closing; callers hold the connection lock."""
        self.closed = True
        await manager.disconnect(self.user_id, self)
        await self.websocket.close(code=4401)

    async def check(self) -> bool:
        """Bound idle authorization checks to the route's receive timeout."""
        async with self._lock:
            if self.closed:
                return False
            if not self._authorized():
                await self._revoke()
                return False
            return True

    async def send_json(self, payload: dict) -> None:
        """Authorize every worker/legacy send and close all SQL scopes before await."""
        async with self._lock:
            if self.closed:
                raise WebSocketDisconnect(code=4401)
            if not self._authorized(payload):
                await self._revoke()
                raise WebSocketDisconnect(code=4401)
            await asyncio.wait_for(self.websocket.send_json(payload), SEND_TIMEOUT)


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


def handshake_token(websocket: WebSocket) -> Optional[str]:
    """Reject duplicate, malformed or conflicting credentials without fallback."""
    headers = websocket.headers.getlist("authorization")
    query = websocket.query_params
    if hasattr(query, "getlist"):
        tokens = query.getlist("token")
    else:
        tokens = [query["token"]] if "token" in query else []
    if len(headers) > 1 or len(tokens) > 1:
        return None
    if tokens and re.fullmatch(r"[A-Za-z0-9_.~-]+", tokens[0]) is None:
        return None
    if headers:
        match = re.fullmatch(r"(?i:Bearer) ([A-Za-z0-9_.~-]+)", headers[0])
        if match is None:
            return None
        token = match.group(1)
        return token if not tokens or tokens[0] == token else None
    return tokens[0] if tokens else None


@router.websocket("/ws/alerts")
async def ws_alerts(websocket: WebSocket):
    """家属实时告警推送：鉴权 → 补推未确认 → 持续接收心跳/应答。"""
    token = handshake_token(websocket)
    user = _resolve_user(token)
    if user is None:
        await websocket.close(code=4401)
        return
    connection = AuthorizedSocket(websocket, token, user.id)
    # Legacy records have no durable delivery state. Receipt-managed records
    # must use the worker CAS path, including on reconnect; unknown is not retry.
    try:
        await websocket.accept()
        await manager.connect(user.id, connection)
        try:
            with SessionLocal() as db:
                legacy = [serialize_alert(alert) for alert in storage.list_pending_alerts_by_owner(db, user.id)
                          if db.query(EdgeAlertReceipt.id).filter_by(alert_id=alert.id).first() is None]
        except SQLAlchemyError:
            raise RuntimeError("websocket_storage_unavailable") from None
        try:
            for payload in legacy:
                await connection.send_json(payload)
            while await connection.check():
                try:
                    await asyncio.wait_for(websocket.receive_text(), RECHECK_INTERVAL)
                except TimeoutError:
                    pass
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 - 连接异常按断开处理
            logger.warning("WebSocket connection ended unexpectedly")
    finally:
        connection.closed = True
        await manager.disconnect(user.id, connection)
