"""
老人端核心 Agent - 实现感知→理解→规划→执行→反馈→记忆闭环
"""
import asyncio
import logging
from datetime import datetime
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


class ElderAgent:
    def __init__(self):
        self.event_bus = EventBus()
        self.memory = MemoryManager()
        self.asr = ASRService()
        self.tts = TTSService()
        self.intent = IntentService()
        
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
                await self._understand(text)
    
    async def _understand(self, text: str):
        """理解模块 - 解析意图"""
        intent = await self.intent.parse(text)
        if intent:
            await self._plan(intent)
    
    async def _plan(self, intent: dict):
        """规划模块 - 任务编排"""
        event = {
            "eventId": f"evt_{datetime.utcnow().timestamp()}",
            "type": intent.get("type", "voice"),
            "priority": intent.get("priority", "P1"),
            "timestamp": datetime.utcnow().isoformat(),
            "source": "edge",
            "payload": intent,
        }
        await self.event_bus.publish(event)
    
    async def execute(self, action: dict):
        """执行模块 - 调用插件"""
        pass
    
    async def _feedback(self, message: str):
        """反馈模块 - 语音输出"""
        await self.tts.speak(message)
    
    async def _remember(self, event: dict):
        """记忆模块 - 更新档案"""
        await self.memory.save(event)
