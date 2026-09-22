"""
提醒插件 - 用药、作息、久坐提醒
"""
import logging
from datetime import datetime, timedelta

from src.plugins.base import BasePlugin

logger = logging.getLogger(__name__)


class ReminderPlugin(BasePlugin):
    def __init__(self, event_bus):
        super().__init__(event_bus)
        self._timers = {}
    
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
        schedule_time = payload.get("schedule_time", "")
        
        logger.info(f"收到提醒任务: {title}")
        
        if task_type == "久坐提醒":
            await self._setup_sedentary_reminder(title)
        else:
            await self._send_reminder(title)
    
    async def _setup_sedentary_reminder(self, title: str):
        """设置久坐提醒"""
        pass
    
    async def _send_reminder(self, title: str):
        """发送提醒"""
        logger.info(f"发送提醒: {title}")
