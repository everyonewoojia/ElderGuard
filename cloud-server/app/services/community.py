"""P9-2 社区养老后台对接抽象接口（M-APP-06 / FR-SC-10）。

社区养老后台的告警对接抽象接口。P2 接口预留：仅定义契约，不完整编码；
接入方须实现 push_alert / sync_status。
"""
from abc import ABC, abstractmethod
from typing import Any, Dict


class CommunityBackend(ABC):
    """社区养老后台告警对接抽象接口。"""

    @abstractmethod
    def push_alert(self, alert: Dict[str, Any]) -> bool:
        """向社区后台推送告警，成功返回 True。"""

    @abstractmethod
    def sync_status(self) -> Dict[str, Any]:
        """同步社区后台接入状态，返回结构化状态。"""
