"""
事件总线 - 内存实现，支持发布/订阅、取消订阅、按 priority 优先分发、插件异常隔离（KK04）

优先级口径：P0（最高）→ P1 → P2。一次 publish 多个事件时按优先级从高到低分发。
单个订阅者抛异常不会中断其他订阅者（异常隔离）。
"""
import asyncio
import logging
from typing import Callable, Dict, List
from collections import defaultdict

logger = logging.getLogger(__name__)

# 优先级权重：数值越小优先级越高
PRIORITY_ORDER: Dict[str, int] = {"P0": 0, "P1": 1, "P2": 2}


def _priority_key(event: dict) -> int:
    """事件的优先级排序键（未知优先级按最低 P2 之后的 99）"""
    return PRIORITY_ORDER.get(event.get("priority", "P1"), 99)


class EventBus:
    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def subscribe(self, event_type: str, callback: Callable):
        """订阅事件"""
        async with self._lock:
            self._subscribers[event_type].append(callback)
            logger.info(f"已订阅事件: {event_type}")

    async def unsubscribe(self, event_type: str, callback: Callable):
        """取消订阅"""
        async with self._lock:
            if event_type in self._subscribers and callback in self._subscribers[event_type]:
                self._subscribers[event_type].remove(callback)

    async def publish(self, *events) -> list[dict]:
        """发布一个或多个事件，按 priority 从高到低（P0→P1→P2）优先分发。

        返回分发计数，不代表事件已持久化或云端已接收。
        单个订阅者失败仍不阻止后续订阅者；取消异常继续向上传播。
        """
        ordered = sorted(events, key=_priority_key)
        reports = []
        for event in ordered:
            reports.append(await self._dispatch(event))
        return reports

    async def _dispatch(self, event: dict) -> dict:
        """将单个事件分发给其类型的订阅者，单订阅者异常隔离（KK04）"""
        event_type = event.get("type", "unknown")
        logger.info(f"分发事件: {event_type}, 优先级: {event.get('priority', 'P1')}")
        subscribers = list(self._subscribers.get(event_type, []))
        report = {"subscribers": len(subscribers), "completed": 0, "rejected": 0, "failed": 0}
        for callback in subscribers:
            try:
                result = await callback(event)
                report["rejected" if result is False else "completed"] += 1
            except Exception:  # 插件异常隔离；向发布者返回失败，不输出原始异常。
                report["failed"] += 1
                logger.error("Subscriber failed; isolated from remaining subscribers")
        return report

    def get_subscribers(self, event_type: str) -> List[Callable]:
        """获取订阅者"""
        return self._subscribers.get(event_type, [])
