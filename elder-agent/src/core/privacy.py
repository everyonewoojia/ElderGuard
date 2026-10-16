"""
隐私授权守卫 - 所有感知模块执行前强制校验授权标记（FR-CM-05/AQ02/CON-B-04）

硬约束：感知采集必须经隐私授权，未授权采集次数为 0。
未授权时 check() 返回 False，调用方不得进行任何采集；capture_count 仅在授权放行并实际采集后递增。
"""
import logging

logger = logging.getLogger(__name__)


class PrivacyGuard:
    def __init__(self, enabled: bool = True):
        self._enabled = bool(enabled)
        self._capture_count = 0
        self._blocked_count = 0

    def is_enabled(self) -> bool:
        """当前是否已授权"""
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """设置授权状态"""
        self._enabled = bool(enabled)
        logger.info(f"隐私授权状态: {'开启' if self._enabled else '关闭'}")

    def check(self, modality: str = "audio") -> bool:
        """感知前授权校验。

        - 已授权：放行采集；
        - 未授权：记录一次被拦截的采集尝试，返回 False，调用方不得采集。
        """
        if not self._enabled:
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
