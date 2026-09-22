"""
安全告警插件 - 跌倒、求助、异常事件告警
"""
import logging
from datetime import datetime

from src.plugins.base import BasePlugin

logger = logging.getLogger(__name__)


class AlertPlugin(BasePlugin):
    def __init__(self, event_bus):
        super().__init__(event_bus)
        self._active_alerts = {}
    
    async def init(self):
        """初始化"""
        logger.info("安全告警插件初始化")
    
    async def start(self):
        """启动"""
        await self.event_bus.subscribe("fall", self.handle_event)
        await self.event_bus.subscribe("help", self.handle_event)
        await self.event_bus.subscribe("abnormal", self.handle_event)
        logger.info("安全告警插件已启动")
    
    async def stop(self):
        """停止"""
        logger.info("安全告警插件已停止")
    
    async def handle_event(self, event: dict):
        """处理告警事件"""
        event_type = event.get("type")
        priority = event.get("priority", "P1")
        
        logger.info(f"收到告警事件: {type}, 优先级: {priority}")
        
        if event_type == "fall":
            await self._handle_fall(event)
        elif event_type == "help":
            await self._handle_help(event)
        elif event_type == "abnormal":
            await self._handle_abnormal(event)
    
    async def _handle_fall(self, event: dict):
        """处理跌倒事件"""
        logger.warning("检测到疑似跌倒事件")
        await self._send_alert("跌倒", event)
    
    async def _handle_help(self, event: dict):
        """处理求助事件"""
        logger.warning("收到求助信号")
        await self._send_alert("求助", event)
    
    async def _handle_abnormal(self, event: dict):
        """处理异常事件"""
        logger.warning("检测到异常情况")
        await self._send_alert("异常", event)
    
    async def _send_alert(self, alert_type: str, event: dict):
        """发送告警"""
        alert_id = f"alert_{datetime.utcnow().timestamp()}"
        self._active_alerts[alert_id] = {
            "type": alert_type,
            "status": "active",
            "created_at": datetime.utcnow().isoformat(),
        }
        logger.info(f"已发送{alert_type}告警: {alert_id}")
