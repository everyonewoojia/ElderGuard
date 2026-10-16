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
from src.plugins.base import BasePlugin

logger = logging.getLogger(__name__)


class AlertPlugin(BasePlugin):
    def __init__(self, event_bus, tts: Optional[TTSService] = None):
        super().__init__(event_bus)
        self._active_alerts = {}
        self._tts = tts or TTSService()
        self._seq = 0

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

        logger.info(f"收到告警事件: {event_type}, 优先级: {priority}")

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
        """发送告警：记录活动告警，并进行本地语音告警兜底（KK04）"""
        alert_id = f"alert_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}_{self._seq}"
        self._seq += 1
        self._active_alerts[alert_id] = {
            "type": alert_type,
            "status": "active",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(f"已发送{alert_type}告警: {alert_id}")
        # 本地语音告警兜底（KK04）：云端推送失败时老人端本地提示
        await self._local_voice_alert(alert_type)

    async def _local_voice_alert(self, alert_type: str):
        """本地语音告警兜底：通过 TTS 本地播报警告"""
        message = f"请注意，检测到{alert_type}情况，请及时处理。"
        try:
            await self._tts.speak(message)
        except Exception as e:  # noqa: BLE001 - 语音告警失败不应中断告警流程
            logger.error(f"本地语音告警失败: {e}")

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
