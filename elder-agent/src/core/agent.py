"""
老人端核心 Agent - 实现感知→理解→规划→执行→反馈→记忆闭环

- 支持依赖注入（asr/tts/intent/memory/event_bus），便于测试与替换 AI 门面。
- handle_voice_text 提供「文字输入→完整闭环」入口（供测试与按键/文字指令复用）。
- 默认构造（无注入）保持 P0 接口契约（test_smoke 兼容）。
"""
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from src.ai.asr import ASRService
from src.ai.tts import TTSService
from src.ai.intent import IntentService
from src.core.event_bus import EventBus
from src.core.memory import MemoryManager
from src.plugins.reminder import ReminderPlugin
from src.plugins.alert import AlertPlugin
from src.plugins.command import CommandPlugin

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    """带时区 UTC 的 ISO 时间字符串（替代已弃用的 datetime.utcnow().isoformat()）。"""
    return datetime.now(timezone.utc).isoformat()


class ElderAgent:
    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        memory: Optional[MemoryManager] = None,
        asr: Optional[ASRService] = None,
        tts: Optional[TTSService] = None,
        intent: Optional[IntentService] = None,
    ):
        self.event_bus = event_bus or EventBus()
        self.memory = memory or MemoryManager()
        self.asr = asr or ASRService()
        self.tts = tts or TTSService()
        self.intent = intent or IntentService()

        self.plugins = [
            ReminderPlugin(self.event_bus),
            AlertPlugin(self.event_bus),
            CommandPlugin(self.event_bus),
        ]

        self._running = False

    async def start(self):
        """启动 Agent"""
        logger.info("正在启动老人端 Agent...")

        for plugin in self.plugins:
            await plugin.init()
            await plugin.start()

        self._running = True
        asyncio.create_task(self._main_loop())

        logger.info("老人端 Agent 启动完成")

    async def stop(self):
        """停止 Agent"""
        logger.info("正在停止老人端 Agent...")
        self._running = False

        for plugin in self.plugins:
            await plugin.stop()

        logger.info("老人端 Agent 已停止")

    async def _main_loop(self):
        """主循环"""
        while self._running:
            try:
                await self._sense()
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"主循环错误: {e}")
                await asyncio.sleep(1)

    async def _sense(self):
        """感知模块 - 采集语音输入"""
        audio_data = await self.asr.listen()
        if audio_data:
            text = await self.asr.recognize(audio_data)
            if text:
                await self.handle_voice_text(text)

    async def _understand(self, text: str):
        """理解模块 - 解析意图"""
        intent = await self.intent.parse(text)
        if intent:
            await self._plan(intent)

    async def _plan(self, intent: dict):
        """规划模块 - 封装标准事件并发布"""
        event = {
            "eventId": f"evt_{time.time()}",
            "type": intent.get("type", "voice"),
            "priority": intent.get("priority", "P1"),
            "timestamp": _utcnow_iso(),
            "source": "edge",
            "payload": intent,
        }
        await self.event_bus.publish(event)

    async def execute(self, action: dict):
        """执行模块 - 经由事件总线分发，由对应插件响应"""
        await self.event_bus.publish(action)

    async def _feedback(self, message: str):
        """反馈模块 - 语音输出"""
        await self.tts.speak(message)

    async def _remember(self, event: dict):
        """记忆模块 - 更新档案"""
        await self.memory.save(event)

    async def handle_voice_text(self, text: str) -> Optional[dict]:
        """文字/语音指令入口：走完整 理解→规划→执行→反馈→记忆 闭环。

        返回解析出的意图；intent 解析为空时按未知意图处理。
        """
        intent = await self.intent.parse(text)
        if intent is None:
            intent = {"type": "unknown", "priority": "P2", "content": text}

        event = {
            "eventId": f"evt_{time.time()}",
            "type": intent.get("type", "unknown"),
            "priority": intent.get("priority", "P1"),
            "timestamp": _utcnow_iso(),
            "source": "edge",
            "payload": intent,
        }
        # 执行：事件总线分发 → 插件响应（如告警）
        await self.event_bus.publish(event)
        # 记忆：写入档案
        await self._remember(event)
        # 反馈：语音输出
        await self._feedback_from_intent(intent)
        return intent

    async def _feedback_from_intent(self, intent: dict):
        """依据意图类型给出语音反馈话术"""
        itype = intent.get("type")
        if itype == "help":
            message = "我已收到您的求助，正在通知家属，请您保持冷静。"
        elif itype == "fall":
            message = "检测到跌倒风险，正在通知家属，请您不要移动。"
        elif itype == "reminder":
            message = "好的，我会为您按时提醒。"
        elif itype == "time":
            now = datetime.now().strftime("%H点%M分")
            message = f"现在是{now}。"
        else:
            message = "请问您需要什么帮助？"
        await self._feedback(message)
