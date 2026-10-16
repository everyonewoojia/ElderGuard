"""
轻量行为识别门面接口（M-AI-04 / FR-SC-02，可模拟）。

P8-4：行为识别门面（行为识别本身为模拟，返回行为 + 置信度）；
核心可测逻辑为「疑似跌倒后 10 秒无应答进入告警流程」（FR-SC-04，可模拟）。
支持注入可测时钟（clock）与告警回调（on_alert），便于确定性测试。
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

AlertCallback = Callable[[dict], Awaitable[None]]


class VisionService:
    """视觉行为识别门面接口（模拟）。"""

    def __init__(
        self,
        timeout_seconds: int = 10,
        clock: Optional[Callable[[], datetime]] = None,
        on_alert: Optional[AlertCallback] = None,
    ):
        self.timeout_seconds = timeout_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.on_alert = on_alert
        self._suspected = None  # {"time": datetime, "confirmed": bool}

    def detect(self, behavior: str, confidence: float) -> dict:
        """行为识别（模拟）。

        返回识别结果；疑似跌倒（confidence >= 0.5）时启动 10 秒无应答计时。
        """
        if behavior == "fall" and confidence >= 0.5:
            self._suspected = {"time": self.clock(), "confirmed": False}
            logger.info("疑似跌倒，等待确认（10 秒无应答将告警）")
            return {"behavior": behavior, "confidence": confidence, "action": "verify_fall"}
        return {"behavior": behavior, "confidence": confidence, "action": "record"}

    def confirm(self) -> None:
        """老人/家属在时限内有应答，取消跌倒告警流程。"""
        if self._suspected is not None:
            self._suspected["confirmed"] = True
            logger.info("疑似跌倒已确认无碍，取消告警流程")

    def has_pending_fall(self) -> bool:
        """是否存在未确认的疑似跌倒。"""
        return self._suspected is not None and not self._suspected["confirmed"]

    async def check_timeout(self) -> Optional[dict]:
        """检查疑似跌倒是否超时未确认（FR-SC-04）。

        超过 timeout_seconds 且未确认 → 进入告警流程（触发 on_alert 回调并返回告警）。
        未超时或已确认 → 返回 None。
        """
        if not self.has_pending_fall():
            return None
        elapsed = (self.clock() - self._suspected["time"]).total_seconds()
        if elapsed < self.timeout_seconds:
            return None
        alert = {
            "type": "fall",
            "reason": "疑似跌倒 10 秒无应答",
            "confirmed": False,
        }
        logger.warning("疑似跌倒 10 秒无应答，进入告警流程")
        if self.on_alert is not None:
            result = self.on_alert(alert)
            if asyncio.iscoroutine(result):
                await result
        return alert
