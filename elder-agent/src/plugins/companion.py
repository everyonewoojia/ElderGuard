"""
主动陪伴插件 - 基于记忆的多轮闲聊陪伴（M-AGT-07 / FR-DC-08，允许模拟）。

P8-3：基于 MemoryManager 长期/短期记忆做多轮闲聊，回复可引用前文与偏好，
体现「多轮对话记忆衔接」；所有对话事件写入记忆，供后续引用。
"""
import logging
from typing import Optional

from src.core.memory import MemoryManager
from src.plugins.base import BasePlugin

logger = logging.getLogger(__name__)


class CompanionPlugin(BasePlugin):
    """主动陪伴插件：基于记忆多轮闲聊。"""

    def __init__(self, event_bus, memory: Optional[MemoryManager] = None):
        super().__init__(event_bus)
        self._memory = memory or MemoryManager()

    async def init(self):
        """初始化"""
        logger.info("主动陪伴插件初始化")

    async def start(self):
        """启动：订阅闲聊/语音事件"""
        await self.event_bus.subscribe("chat", self.handle_event)
        await self.event_bus.subscribe("voice", self.handle_event)
        logger.info("主动陪伴插件已启动")

    async def stop(self):
        """停止"""
        logger.info("主动陪伴插件已停止")

    async def handle_event(self, event: dict):
        """处理闲聊事件，生成陪伴回复"""
        payload = event.get("payload", {})
        text = payload.get("content", "") if isinstance(payload, dict) else ""
        if text:
            await self.companion_reply(text, event)

    async def companion_reply(self, text: str, event: Optional[dict] = None) -> str:
        """基于记忆上下文生成陪伴回复（多轮记忆衔接）。

        先从记忆读取最近对话与偏好（称呼等），按规则回复；
        若用户表达「记得/刚才/上次」，则引用记忆中最近一条对话（记忆衔接）。
        """
        ctx = await self._memory.get_context()
        prefs = ctx.get("preferences", {})
        history = ctx.get("recent_history", [])
        name = prefs.get("name", "")

        reply = self._compose_reply(text, name, history)
        # 写入记忆（历史），供后续轮次衔接
        await self._memory.save({
            "timestamp": None,
            "type": "chat",
            "payload": {"content": text, "reply": reply},
        })
        return reply

    def _compose_reply(self, text: str, name: str, history: list) -> str:
        """关键词规则 + 记忆衔接生成回复（模拟陪伴话术）"""
        greet = f"{name}，" if name else ""
        # 记忆衔接：用户问「记得/刚才/上次」时引用最近一次对话
        if any(kw in text for kw in ("记得", "刚才", "上次", "昨天")):
            for item in reversed(history):
                content = item.get("content") or item.get("payload", {}).get("content", "")
                if content and content != text:
                    return f"{greet}上次我们聊到「{content}」，我一直帮你记着呢。"
            return f"{greet}我这边还没聊过相关的内容，你可以告诉我，我会记住的。"
        # 关键词规则
        rules = {
            "吃药": "记得按时吃药，身体要紧。",
            "药": "记得按时吃药，身体要紧。",
            "累": "累了就歇一歇，我陪你聊聊天。",
            "睡": "早点休息，睡眠很重要。",
            "想": "别担心，我一直在陪着你。",
            "无聊": "要不要我陪你聊聊天，讲个故事？",
            "饿": "记得按时吃饭，别饿着。",
        }
        for kw, reply in rules.items():
            if kw in text:
                return f"{greet}{reply}"
        return f"{greet}我在的，你可以多跟我说说话。"

    def memory(self) -> MemoryManager:
        """暴露记忆管理器（测试/外部使用）"""
        return self._memory
