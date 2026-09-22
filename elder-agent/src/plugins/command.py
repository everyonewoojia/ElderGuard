"""
指令解析插件 - 解析老人语音、按键指令，匹配技能
"""
import logging
from typing import Optional

from src.plugins.base import BasePlugin

logger = logging.getLogger(__name__)


class CommandPlugin(BasePlugin):
    def __init__(self, event_bus):
        super().__init__(event_bus)
        self._commands = {
            "help": self._cmd_help,
            "reminder": self._cmd_reminder,
            "time": self._cmd_time,
        }
    
    async def init(self):
        """初始化"""
        logger.info("指令解析插件初始化")
    
    async def start(self):
        """启动"""
        await self.event_bus.subscribe("voice", self.handle_event)
        await self.event_bus.subscribe("button", self.handle_event)
        logger.info("指令解析插件已启动")
    
    async def stop(self):
        """停止"""
        logger.info("指令解析插件已停止")
    
    async def handle_event(self, event: dict):
        """处理语音/按键事件"""
        payload = event.get("payload", {})
        text = payload.get("text", "")
        
        if text:
            command = self._match_command(text)
            if command:
                await self._commands[command](text)
            else:
                logger.info(f"未匹配到指令: {text}")
    
    def _match_command(self, text: str) -> Optional[str]:
        """匹配指令"""
        text_lower = text.lower()
        
        if any(kw in text_lower for kw in ["帮助", "帮忙", "help"]):
            return "help"
        if any(kw in text_lower for kw in ["提醒", "吃药", "服药"]):
            return "reminder"
        if any(kw in text_lower for kw in ["几点", "时间", "现在"]):
            return "time"
        
        return None
    
    async def _cmd_help(self, text: str):
        """帮助指令"""
        logger.info("执行帮助指令")
    
    async def _cmd_reminder(self, text: str):
        """提醒指令"""
        logger.info("执行提醒指令")
    
    async def _cmd_time(self, text: str):
        """时间指令"""
        from datetime import datetime
        now = datetime.now().strftime("%H点%M分")
        logger.info(f"当前时间: {now}")
