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
from src.core.privacy import PrivacyGuard
from src.core.events import ensure_event_identity
from src.core.alert_outbox import OutboxError
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
        privacy: Optional[PrivacyGuard] = None,
        edge_client=None,
        elder_id: Optional[int] = None,
        owns_edge_client: bool = False,
        alert_outbox=None,
        owns_alert_outbox: bool = False,
        reporter=None,
    ):
        if reporter is not None and reporter.outbox is not alert_outbox:
            raise OutboxError("binding_mismatch")
        self.event_bus = event_bus or EventBus()
        self.memory = memory or MemoryManager()
        self.privacy = privacy or PrivacyGuard()
        self.privacy.bind_memory(self.memory)
        self.asr = asr or ASRService(privacy=self.privacy)
        self.asr.privacy = self.privacy
        self.tts = tts or TTSService()
        self.intent = intent or IntentService()
        self.edge_client = edge_client
        self.elder_id = elder_id
        self._owns_edge_client = owns_edge_client
        self.alert_outbox = alert_outbox
        self._owns_alert_outbox = owns_alert_outbox
        self.reporter = reporter
        self._privacy_interval = 5.0
        self._next_privacy_sync = 0.0
        self._asr_ready = False
        self.capture_status = "privacy_unavailable"
        self._main_task = None
        self._started_plugins = []
        self._resources_closed = False
        self._stopping = False

        self.plugins = [
            ReminderPlugin(self.event_bus, memory=self.memory),
            AlertPlugin(self.event_bus, tts=self.tts, outbox=self.alert_outbox, reporter=self.reporter),
            CommandPlugin(self.event_bus, tts=self.tts),
        ]

        self._running = False

    async def start(self):
        """启动 Agent"""
        logger.info("正在启动老人端 Agent...")

        if self._running:
            return
        if self._resources_closed:
            raise RuntimeError("Stopped agent cannot be restarted")
        try:
            await self.sync_privacy()
            for plugin in self.plugins:
                self._started_plugins.append(plugin)
                await plugin.init()
                await plugin.start()
            if self.reporter is not None:
                await self.reporter.start()
            self._running = True
            self._main_task = asyncio.create_task(self._main_loop())
        except BaseException:
            await self.stop()
            raise

        logger.info("老人端 Agent 启动完成")

    async def stop(self):
        """停止 Agent"""
        logger.info("正在停止老人端 Agent...")
        self._stopping = True
        self._running = False
        self.privacy.apply_cloud(None)
        if self._main_task is not None:
            task = self._main_task
            self._main_task = None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("Agent background task failed during shutdown")
        if self._resources_closed:
            return
        self.capture_status = "stopping"
        reporter_error = None
        if self.reporter is not None:
            try:
                await self.reporter.stop()
            except BaseException as error:
                if self.reporter.task is not None:
                    # The sender may still use HTTP and the owned queue. Keep
                    # their owners alive and allow a subsequent stop to retry.
                    raise
                reporter_error = error
        self._resources_closed = True
        self.capture_status = "stopped"
        for plugin in reversed(self._started_plugins):
            try:
                await plugin.stop()
            except Exception:
                logger.warning("Plugin shutdown failed")
        self._started_plugins.clear()
        try:
            await self.asr.close()
        finally:
            self._asr_ready = False
            try:
                if self._owns_edge_client and self.edge_client is not None:
                    await self.edge_client.aclose()
            finally:
                if self._owns_alert_outbox and self.alert_outbox is not None:
                    self.alert_outbox.close()
        self._resources_closed = True
        self.capture_status = "stopped"
        if reporter_error is not None:
            raise reporter_error

        logger.info("老人端 Agent 已停止")

    async def _main_loop(self):
        """主循环"""
        while self._running:
            try:
                if time.monotonic() >= self._next_privacy_sync:
                    await self.sync_privacy()
                await self._sense()
                await asyncio.sleep(0.1)
            except Exception:
                logger.error("Agent loop failed: operation_unavailable")
                await asyncio.sleep(1)

    async def sync_privacy(self) -> bool:
        """Suspend sensing, verify the cloud grant, then initialize authorized ASR.

        Five-second grants expire locally. Network errors never clear a saved
        local veto, and successful responses never persist an enabled state.
        """
        self.privacy.apply_cloud(None)
        enabled = None
        try:
            if (self.edge_client is not None and type(self.elder_id) is int
                    and self.elder_id > 0):
                enabled = await self.edge_client.fetch_privacy(self.elder_id)
        except Exception:
            logger.warning("Privacy synchronization unavailable; capture disabled")
        self._next_privacy_sync = time.monotonic() + self._privacy_interval
        self.privacy.apply_cloud(enabled, valid_for=self._privacy_interval)
        if not self.privacy.is_enabled():
            self.capture_status = ("local_revoked" if self.privacy.local_revoked
                                   else "unauthorized" if enabled is False
                                   else "privacy_unavailable")
            logger.info("Capture disabled: %s", self.capture_status)
            return False
        if not self._asr_ready:
            try:
                self._asr_ready = await self.asr.init() is True
            except Exception:
                self._asr_ready = False
            if not self._asr_ready:
                self.privacy.apply_cloud(None)
                self.capture_status = "asr_unavailable"
                logger.warning("ASR unavailable; capture disabled")
                return False
        self.capture_status = "ready"
        if not self.privacy.is_enabled():
            self.capture_status = "privacy_unavailable"
            return False
        return True

    async def _sense(self):
        """感知模块 - 采集语音输入（执行前强制校验隐私授权，P5-5）"""
        if not self.privacy.check("audio"):
            return
        if not self._asr_ready:
            return
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
            "type": intent.get("type", "voice"),
            "priority": intent.get("priority", "P1"),
            "timestamp": _utcnow_iso(),
            "source": "edge",
            "payload": intent,
        }
        return await self._publish_event(event)

    async def execute(self, action: dict):
        """执行模块 - 经由事件总线分发，由对应插件响应"""
        return await self._publish_event(action)

    async def _publish_event(self, event: dict) -> dict:
        """Persist alerts synchronously before the first subscriber can suspend.

        There is no await between identity assignment, queue admission and its
        atomic commit. Stop/task cancellation can run at the later dispatch
        await, by which point an admitted alert is already durable. The plugin
        uses the same queue/ID; its duplicate validation does not write again.
        """
        if self._stopping or self._resources_closed:
            raise RuntimeError("agent_stopping")
        identity = ensure_event_identity(event)
        is_alert = event.get("type") in ("help", "fall", "abnormal")
        persisted = False
        if is_alert and self.alert_outbox is not None:
            try:
                self.alert_outbox.enqueue(event)
                persisted = True
            except OutboxError:
                logger.error("Alert admission failed; event not dispatched")
                try:
                    if await self._feedback("告警未能保存，目前无法确认家属收到，请尝试其他求助方式。") is False:
                        logger.warning("Local alert failure feedback unavailable")
                except Exception:
                    logger.warning("Local alert failure feedback unavailable")
                raise
        reports = await self.event_bus.publish(event)
        if is_alert and any(report["failed"] or (persisted and report["rejected"])
                            for report in reports):
            # The durable record, if any, is retained for recovery. Do not
            # pretend that a plugin failure completed the handling operation.
            raise RuntimeError("event_dispatch_failed")
        return {"event_id": identity, "published": True, "persisted": persisted,
                "cloud_received": None,
                "dispatch_complete": bool(reports) and all(
                    report["subscribers"] > 0 and not report["failed"] and not report["rejected"]
                    for report in reports)}

    async def _feedback(self, message: str):
        """反馈模块 - 语音输出"""
        return await self.tts.speak(message)

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
            "type": intent.get("type", "unknown"),
            "priority": intent.get("priority", "P1"),
            "timestamp": _utcnow_iso(),
            "source": "edge",
            "payload": intent,
        }
        # 执行：事件总线分发 → 插件响应（如告警）
        result = await self._publish_event(event)
        # 记忆：写入档案
        await self._remember(event)
        # 反馈：语音输出
        await self._feedback_from_intent(intent)
        if event["type"] in ("help", "fall", "abnormal"):
            return {**intent, "alert_result": result}
        return intent

    async def _feedback_from_intent(self, intent: dict):
        """依据意图类型给出语音反馈话术"""
        itype = intent.get("type")
        if itype == "help":
            message = "我已收到您的求助，请您保持冷静，目前无法确认家属收到。"
        elif itype == "fall":
            message = "检测到跌倒风险，请您注意安全，目前无法确认家属收到。"
        elif itype == "reminder":
            message = "好的，我会为您按时提醒。"
        elif itype == "time":
            now = datetime.now().strftime("%H点%M分")
            message = f"现在是{now}。"
        else:
            message = "请问您需要什么帮助？"
        await self._feedback(message)
