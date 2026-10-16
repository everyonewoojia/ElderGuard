"""P9-4 多模态融合门面抽象接口（M-AI-05 / FR-CM-10）。

融合 ASR 文本、视觉行为、语音情绪等多路感知信号的综合理解门面。
P2 接口预留：仅定义契约，不完整编码。
"""
from abc import ABC, abstractmethod
from typing import Any, Dict


class MultimodalFacade(ABC):
    """多模态融合门面：融合多路感知信号，输出综合理解结果。"""

    @abstractmethod
    def fuse(self, signals: Dict[str, Any]) -> Dict[str, Any]:
        """融合多路感知信号（如 {"asr": ..., "vision": ..., "emotion": ...}），返回综合理解。"""
