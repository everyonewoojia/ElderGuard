"""
指令解析插件 - 解析老人语音、按键指令，匹配并执行技能

P5-3：语音/按键指令匹配（FR-MI-01/03）；技能匹配执行并记录（FR-DC-05）；
支持注入 tts（技能执行反馈）。
"""
import logging
from typing import Optional

from src.ai.tts import TTSService
from src.plugins.base import BasePlugin

logger = logging.getLogger(__name__)


class CommandPlugin(BasePlugin):
    def __init__(self, event_bus, tts: Optional[TTSService] = None):
        super().__init__(event_bus)
        self._commands = {
            "help": self._cmd_help,
            "reminder": self._cmd_reminder,
            "time": self._cmd_time,
        }
        self._tts = tts or TTSService()
        self._executed = []  # 已执行技能记录（供测试/审计）

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
        """匹配指令（关键词规则）"""
        text_lower = text.lower()

        if any(kw in text_lower for kw in ["帮助", "帮忙", "help", "求助", "叫家属", "帮帮我"]):
            return "help"
        if any(kw in text_lower for kw in ["提醒", "吃药", "服药"]):
            return "reminder"
        if any(kw in text_lower for kw in ["几点", "时间", "现在"]):
            return "time"

        return None

    async def _cmd_help(self, text: str):
        """帮助指令"""
        await self._execute("help", text)

    async def _cmd_reminder(self, text: str):
        """提醒指令"""
        await self._execute("reminder", text)

    async def _cmd_time(self, text: str):
        """时间指令"""
        await self._execute("time", text)

    async def _execute(self, command: str, text: str):
        """执行技能并记录，必要时语音反馈"""
        self._executed.append(command)
        logger.info(f"执行技能: {command}")

    @property
    def executed(self) -> list:
        """已执行技能记录"""
        return self._executed
