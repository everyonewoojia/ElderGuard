"""
提醒插件 - 用药、作息、久坐提醒

P5-1：久坐超 4 小时触发提醒并写日志（FR-SC-03）；任务完成/未完成状态（FR-CM-02）。
支持注入 memory（写久坐日志）与 now（可测时钟）。
"""
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

from src.core.memory import MemoryManager
from src.plugins.base import BasePlugin

logger = logging.getLogger(__name__)


class ReminderPlugin(BasePlugin):
    SEDENTARY_THRESHOLD = timedelta(hours=4)

    def __init__(
        self,
        event_bus,
        memory: Optional[MemoryManager] = None,
        now: Optional[Callable[[], datetime]] = None,
    ):
        super().__init__(event_bus)
        self._timers = {}
        self._tasks = {}          # task_id -> {title, schedule_time, status}
        self._memory = memory
        self._now = now or (lambda: datetime.now())
        self._sedentary_start = None
        self._sedentary_triggered = False
        self._sedentary_title = None
        self._seq = 0

    async def init(self):
        """初始化"""
        logger.info("提醒插件初始化")

    async def start(self):
        """启动"""
        await self.event_bus.subscribe("reminder", self.handle_event)
        logger.info("提醒插件已启动")

    async def stop(self):
        """停止"""
        for timer in self._timers.values():
            timer.cancel()
        logger.info("提醒插件已停止")

    async def handle_event(self, event: dict):
        """处理提醒事件"""
        payload = event.get("payload", {})
        task_type = payload.get("task_type", "")
        title = payload.get("title", "")

        logger.info(f"收到提醒任务: {title}")

        if task_type == "久坐提醒":
            await self._setup_sedentary_reminder(title)
        else:
            await self._send_reminder(title, payload)

    async def _setup_sedentary_reminder(self, title: str):
        """设置久坐提醒：记录久坐起点，等待超时触发"""
        self._sedentary_start = self._now()
        self._sedentary_triggered = False
        self._sedentary_title = title or "久坐提醒"
        logger.info(f"已设置久坐提醒起点: {self._sedentary_start}")

    async def check_sedentary(self) -> bool:
        """检查是否久坐超过阈值（4 小时）；触发时发送提醒并写日志（FR-SC-03）。

        返回本次是否触发。
        """
        if self._sedentary_start is None or self._sedentary_triggered:
            return False

        elapsed = self._now() - self._sedentary_start
        if elapsed < self.SEDENTARY_THRESHOLD:
            return False

        # 超过 4 小时：触发提醒，仅触发一次
        self._sedentary_triggered = True
        await self._send_reminder(self._sedentary_title or "久坐提醒", {})
        await self._log_sedentary()
        return True

    async def _log_sedentary(self):
        """写久坐提醒日志"""
        if self._memory is not None:
            await self._memory.save({
                "type": "sedentary",
                "timestamp": self._now().isoformat(),
                "payload": {"content": f"久坐超{self.SEDENTARY_THRESHOLD}，已提醒: {self._sedentary_title}"},
            })
            logger.info("已写入久坐提醒日志")

    async def _send_reminder(self, title: str, payload: Optional[dict] = None):
        """发送提醒并记录任务（初始状态 pending）"""
        task_id = f"task_{self._seq}"
        self._seq += 1
        self._tasks[task_id] = {
            "title": title,
            "status": "pending",
            "schedule_time": (payload or {}).get("schedule_time"),
        }
        logger.info(f"发送提醒: {title} ({task_id})")

    def get_task_status(self, task_id: Optional[str] = None):
        """任务状态查询（FR-CM-02）；不传 id 返回全部任务"""
        if task_id is None:
            return self._tasks
        return self._tasks.get(task_id)

    async def mark_completed(self, task_id: str):
        """标记任务完成"""
        if task_id in self._tasks:
            self._tasks[task_id]["status"] = "completed"
            logger.info(f"任务完成: {task_id}")

    async def mark_pending(self, task_id: str):
        """标记任务未完成"""
        if task_id in self._tasks:
            self._tasks[task_id]["status"] = "pending"
            logger.info(f"任务置为未完成: {task_id}")
