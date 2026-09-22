"""
事件总线 - 内存实现，支持发布/订阅
"""
import asyncio
import logging
from typing import Callable, Dict, List
from collections import defaultdict

logger = logging.getLogger(__name__)


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
            if event_type in self._subscribers:
                self._subscribers[event_type].remove(callback)
    
    async def publish(self, event: dict):
        """发布事件"""
        event_type = event.get("type", "unknown")
        priority = event.get("priority", "P1")
        
        logger.info(f"发布事件: {event_type}, 优先级: {priority}")
        
        subscribers = self._subscribers.get(event_type, [])
        for callback in subscribers:
            try:
                await callback(event)
            except Exception as e:
                logger.error(f"事件处理错误: {e}")
    
    def get_subscribers(self, event_type: str) -> List[Callable]:
        """获取订阅者"""
        return self._subscribers.get(event_type, [])
