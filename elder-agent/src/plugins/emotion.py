"""P9-3 高级情绪识别插件抽象接口（M-AGT-08 / FR-CM-08）。

继承 BasePlugin 的高阶情绪识别抽象插件。P2 接口预留：仅定义契约，不完整编码；
实现类须同时满足 BasePlugin 生命周期接口与 recognize_emotion 情绪识别接口。
"""
from abc import abstractmethod
from typing import Any, Dict, Optional

from src.plugins.base import BasePlugin


class EmotionPlugin(BasePlugin):
    """高级情绪识别插件抽象基类：在多模态输入（文本 / 语音特征）上识别情绪。"""

    @abstractmethod
    async def recognize_emotion(
        self, text: str, features: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """识别情绪并返回结构化结果（如 {"emotion": "sad", "confidence": 0.8}）。"""
