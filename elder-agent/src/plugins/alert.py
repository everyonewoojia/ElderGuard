"""
安全告警插件 - 跌倒、求助、异常事件告警

P5-2：语音求助（FR-SC-01）、跌倒/异常事件告警；本地语音告警兜底（KK04）；
告警状态管理（active/cancelled）。
支持注入 tts（本地语音兜底）。
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from src.ai.tts import TTSService
from src.core.alert_outbox import OutboxError
from src.core.events import ensure_event_identity
from src.plugins.base import BasePlugin

logger = logging.getLogger(__name__)


class AlertPlugin(BasePlugin):
    def __init__(self, event_bus, tts: Optional[TTSService] = None, outbox=None, reporter=None):
        super().__init__(event_bus)
        self._active_alerts = {}
        self._tts = tts or TTSService()
        self._outbox = outbox
        self._reporter = reporter

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
        for kind in ("fall", "help", "abnormal"):
            await self.event_bus.unsubscribe(kind, self.handle_event)
        logger.info("安全告警插件已停止")

    async def handle_event(self, event: dict):
        """处理告警事件"""
        event_type = event.get("type")
        if event_type == "fall":
            return await self._handle_fall(event)
        elif event_type == "help":
            return await self._handle_help(event)
        elif event_type == "abnormal":
            return await self._handle_abnormal(event)
        return False

    async def _handle_fall(self, event: dict):
        """处理跌倒事件"""
        logger.warning("检测到疑似跌倒事件")
        return await self._send_alert("跌倒", event)

    async def _handle_help(self, event: dict):
        """处理求助事件"""
        logger.warning("收到求助信号")
        return await self._send_alert("求助", event)

    async def _handle_abnormal(self, event: dict):
        """处理异常事件"""
        logger.warning("检测到异常情况")
        return await self._send_alert("异常", event)

    async def _send_alert(self, alert_type: str, event: dict):
        """Validate persistence, wake the sole reporter, then give local feedback."""
        try:
            alert_id = ensure_event_identity(event)
        except (ValueError, TypeError, AttributeError):
            await self._local_voice_alert(alert_type, "invalid_event")
            return False
        storage_status = "unconfigured"
        if self._outbox is not None:
            try:
                self._outbox.enqueue(event)
                storage_status = "persisted"
            except OutboxError as exc:
                storage_status = exc.code
        previous = self._active_alerts.get(alert_id)
        if previous is None:
            self._active_alerts[alert_id] = {
                "type": alert_type, "status": "active",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "storage_status": storage_status,
            }
        else:
            previous["storage_status"] = storage_status
        logger.info("Local alert storage: %s", storage_status)
        if storage_status == "persisted" and self._reporter is not None:
            self._reporter.wake()
        await self._local_voice_alert(alert_type, storage_status)
        return storage_status == "persisted"

    async def _local_voice_alert(self, alert_type: str, storage_status: str):
        """本地语音告警兜底：通过 TTS 本地播报警告"""
        message = f"请注意，检测到{alert_type}情况，请及时处理。"
        if storage_status == "persisted":
            message += "告警已在本机保存，目前无法确认家属收到。"
        else:
            message += "告警未能保存，目前无法确认家属收到，请尝试其他求助方式。"
        try:
            await self._tts.speak(message)
        except Exception:  # 语音告警失败不应中断告警流程，也不输出原始异常。
            logger.error("Local alert speech unavailable")

    def active_alerts(self) -> dict:
        """当前活动/历史告警"""
        return dict(self._active_alerts)

    def get_alert(self, alert_id: str) -> Optional[dict]:
        """查询单条告警"""
        return self._active_alerts.get(alert_id)

    async def cancel_alert(self, alert_id: str):
        """手动取消告警（状态置为 cancelled）"""
        if alert_id in self._active_alerts:
            self._active_alerts[alert_id]["status"] = "cancelled"
            logger.info(f"告警已取消: {alert_id}")
