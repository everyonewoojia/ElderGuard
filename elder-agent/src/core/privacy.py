"""
隐私授权守卫 - 所有感知模块执行前强制校验授权标记（FR-CM-05/AQ02/CON-B-04）

硬约束：感知采集必须经隐私授权，未授权采集次数为 0。
未授权时 check() 返回 False，调用方不得进行任何采集；capture_count 仅在授权放行并实际采集后递增。
"""
import logging
import time

logger = logging.getLogger(__name__)


class PrivacyGuard:
    def __init__(self, enabled: bool = False, memory=None):
        self._enabled = enabled is True
        self._cloud_enabled = True if enabled is True else None
        self._valid_until = None
        self._local_revoked = False
        self._storage_ok = True
        self._memory = None
        self._capture_count = 0
        self._blocked_count = 0
        if memory is not None:
            self.bind_memory(memory)

    def bind_memory(self, memory) -> None:
        """Share the existing local profile store without overriding a veto."""
        if self._memory is not None and self._memory is not memory:
            raise ValueError("Privacy store already bound")
        self._memory = memory
        try:
            stored = memory.get_local_privacy_revoked()
            if self._local_revoked and not stored:
                memory.set_local_privacy_revoked(True)
            self._local_revoked = self._local_revoked or stored
        except Exception:
            self._storage_ok = False
            self._local_revoked = True
            logger.error("Local privacy record unavailable; capture disabled")
        self._enabled = self._enabled and not self._local_revoked and self._storage_ok

    @property
    def local_revoked(self) -> bool:
        """Whether an explicit local veto or invalid local record blocks capture."""
        return self._local_revoked

    def apply_cloud(self, enabled, valid_for=None) -> None:
        """Accept only a strict, fresh cloud result; never persist or clear a veto."""
        self._cloud_enabled = enabled if type(enabled) is bool else None
        self._valid_until = None
        if valid_for is not None:
            if type(valid_for) not in (int, float) or not 0 < valid_for <= 60:
                self._cloud_enabled = None
            else:
                self._valid_until = time.monotonic() + valid_for
        self._enabled = (self._cloud_enabled is True and not self._local_revoked
                         and self._storage_ok)

    def is_enabled(self) -> bool:
        """当前是否已授权"""
        return (self._enabled and (self._valid_until is None
                                  or time.monotonic() < self._valid_until))

    def set_enabled(self, enabled: bool) -> None:
        """Explicit local revoke/re-consent; re-consent still needs a cloud grant."""
        self._enabled = False
        if type(enabled) is not bool:
            raise ValueError("Privacy decision must be boolean")
        if not enabled:
            self._local_revoked = True
        try:
            if self._memory is not None:
                self._memory.set_local_privacy_revoked(not enabled)
        except Exception:
            self._storage_ok = False
            self._local_revoked = True
            raise ValueError("Local privacy decision could not be saved; capture disabled") from None
        self._storage_ok = True
        self._local_revoked = not enabled
        self._enabled = self._cloud_enabled is True and not self._local_revoked
        logger.info("Local privacy decision updated")

    def check(self, modality: str = "audio") -> bool:
        """感知前授权校验。

        - 已授权：放行采集；
        - 未授权：记录一次被拦截的采集尝试，返回 False，调用方不得采集。
        """
        if not self.is_enabled():
            self._blocked_count += 1
            logger.warning(f"隐私未授权，拦截 {modality} 采集（累计被拦截 {self._blocked_count} 次）")
            return False
        return True

    def record_capture(self) -> None:
        """记录一次实际采集（仅在授权放行后由采集端调用）"""
        self._capture_count += 1

    @property
    def capture_count(self) -> int:
        """实际采集次数（未授权时应为 0）"""
        return self._capture_count

    @property
    def blocked_count(self) -> int:
        """被拦截的采集尝试次数"""
        return self._blocked_count
