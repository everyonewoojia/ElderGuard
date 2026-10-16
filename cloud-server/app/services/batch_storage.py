"""P9-4 社区批量存储优化方案接口（M-DATA-05 / FR-CM-10）。

社区场景下大量记录批量写入 / 缓存落盘的存储优化抽象接口。
P2 接口预留：仅定义契约，不完整编码。
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List


class BatchStorage(ABC):
    """社区批量存储优化抽象接口（批量 upsert / 定期 flush）。"""

    @abstractmethod
    def batch_upsert(self, records: List[Dict[str, Any]]) -> int:
        """批量写入记录，返回成功条数。"""

    @abstractmethod
    def flush(self) -> int:
        """将缓存批量落盘，返回落盘条数。"""
