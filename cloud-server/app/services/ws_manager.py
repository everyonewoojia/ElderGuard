"""WebSocket 连接管理（M-ACC-03 / P8-1）。

单例 manager：维护 家属user_id -> WebSocket 连接集合；
向指定家属推送告警（push_to_user）；连接/断开管理。
"""
import logging
from typing import Dict, List

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WSManager:
    """WebSocket 连接与推送管理器。"""

    def __init__(self):
        self._connections: Dict[int, List[WebSocket]] = {}

    async def connect(self, user_id: int, ws: WebSocket) -> None:
        """登记一个家属连接。"""
        self._connections.setdefault(user_id, []).append(ws)

    async def disconnect(self, user_id: int, ws: WebSocket) -> None:
        """移除一个连接；该家属无连接时清空登记。"""
        conns = self._connections.get(user_id, [])
        if ws in conns:
            conns.remove(ws)
        if not conns:
            self._connections.pop(user_id, None)

    def online_users(self) -> List[int]:
        """当前在线的家属用户列表。"""
        return list(self._connections.keys())

    async def push_to_user(self, user_id: int, payload: dict) -> int:
        """向某家属的全部在线连接推送一条消息，返回实际送达连接数。"""
        conns = self._connections.get(user_id, [])
        sent = 0
        for ws in list(conns):
            try:
                await ws.send_json(payload)
                sent += 1
            except Exception:  # noqa: BLE001 - 单个连接失败不影响其他
                logger.warning("推送失败，连接可能已断开")
        return sent


manager = WSManager()
