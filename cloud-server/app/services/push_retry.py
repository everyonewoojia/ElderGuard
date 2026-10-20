"""告警推送重试模块（M-APP-05 / FR-SC-06）。

P8-2：推送失败最多重试 3 次（KK01 相关），最终仍失败则记录到 failures（不静默丢弃）。
支持同步 sender 与异步 sender（apush）。用于云端向家属端推送告警时的网络异常重发。
"""
import inspect
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_ERROR_CATEGORIES = frozenset({"transport_error", "timeout", "delivery_error", "unknown_error"})


def _error_category(error: Exception) -> str:
    """Classify known transport exceptions without formatting untrusted data."""
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ConnectionError):
        return "transport_error"
    return "unknown_error"


class PushRetryService:
    """带重试上限的推送器：失败重试 ≤ max_retries 次，最终失败有记录。"""

    def __init__(self, max_retries: int = 3, sender: Optional[Callable[[Any, Any], Any]] = None):
        self.max_retries = max_retries
        self.sender = sender
        self.failures: list = []

    def push(self, payload: Any, target: Any = None) -> bool:
        """同步推送：重试 ≤3 次，成功返回 True，最终失败记录并返回 False。"""
        attempts = 0
        last_error: Optional[str] = None
        for _ in range(self.max_retries):
            attempts += 1
            try:
                result = self.sender(payload, target) if self.sender else True
                if result:
                    return True
                last_error = "delivery_error"
            except Exception as e:  # noqa: BLE001 - 网络异常均应重试
                last_error = _error_category(e)
        self._record(payload, target, attempts, last_error)
        return False

    async def apush(self, payload: Any, target: Any = None) -> bool:
        """异步推送：sender 可为 async/sync，重试 ≤3 次，最终失败记录并返回 False。"""
        attempts = 0
        last_error: Optional[str] = None
        for _ in range(self.max_retries):
            attempts += 1
            try:
                result = self.sender(payload, target) if self.sender else True
                if inspect.isawaitable(result):
                    result = await result
                if result:
                    return True
                last_error = "delivery_error"
            except Exception as e:  # noqa: BLE001 - 网络异常均应重试
                last_error = _error_category(e)
        self._record(payload, target, attempts, last_error)
        return False

    def _record(self, payload, target, attempts, error):
        """Keep legacy keys, replacing private objects and error text with safe values."""
        category = error if type(error) is str and error in _ERROR_CATEGORIES else "unknown_error"
        self.failures.append(
            {"payload": None, "target": None, "attempts": attempts, "error": category}
        )
        try:
            logger.error("push_retry_failed attempts=%d category=%s", attempts, category)
        except Exception:  # A diagnostic sink failure must not expose another raw exception.
            # The completed failure remains available through failure_count/failures.
            pass

    def failure_count(self) -> int:
        """记录到的最终失败条数。"""
        return len(self.failures)
