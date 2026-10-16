"""
任务规划调度器 - 风险分级、高优先级告警优先调度

以 priority（P0 > P1 > P2）作为风险分级；入队即按优先级排序，
next()/run_next() 始终取最高优先级任务。
"""
import asyncio
import logging
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}


class TaskScheduler:
    def __init__(self):
        # (优先级权重, 任务)；权重越小优先级越高
        self._queue: List[Tuple[int, Any]] = []

    async def schedule(self, task: Any, priority: str = "P1"):
        """入队任务并按优先级排序（P0 最高）。"""
        self._queue.append((PRIORITY_ORDER.get(priority, 99), task))
        self._queue.sort(key=lambda x: x[0])

    async def next(self) -> Optional[Any]:
        """取出并移除最高优先级任务；队列为空返回 None。"""
        if not self._queue:
            return None
        return self._queue.pop(0)[1]

    async def run_next(self) -> Optional[Any]:
        """执行下一个最高优先级任务；任务为可调用对象时执行之。"""
        task = await self.next()
        if task is None:
            return None
        if callable(task):
            if asyncio.iscoroutinefunction(task):
                return await task()
            return task()
        return task

    def size(self) -> int:
        """当前队列任务数。"""
        return len(self._queue)

    def clear(self) -> None:
        """清空队列。"""
        self._queue.clear()
