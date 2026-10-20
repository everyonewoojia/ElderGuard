"""
跌倒语音通话通道（FR-SC-07，可模拟）与远程语音呼叫（FR-FL-04）。

P8-7：告警后提供通话通道（模拟）；呼叫可建立或明确失败（返回 False），
不崩溃。供老人端跌倒告警后呼叫家属、或家属远程语音呼叫老人使用。
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class CallService:
    """模拟通话通道服务。"""

    def __init__(self):
        self._connected = False
        self._target: Optional[str] = None

    async def connect(self, target: str) -> bool:
        """建立呼叫。

        有效 target（非空）→ 呼叫建立，返回 True；
        无效 target（空/未提供）→ 明确失败，返回 False。
        """
        if not target or not target.strip():
            logger.warning("呼叫目标无效，呼叫建立失败")
            return False
        self._connected = True
        self._target = target
        logger.info("Call connected")
        return True

    async def disconnect(self) -> bool:
        """挂断。未连接时返回 False（明确失败），已连接返回 True。"""
        if not self._connected:
            return False
        self._connected = False
        self._target = None
        logger.info("通话挂断")
        return True

    def is_connected(self) -> bool:
        """当前是否处于通话中。"""
        return self._connected

    def target(self) -> Optional[str]:
        """当前通话目标。"""
        return self._target
