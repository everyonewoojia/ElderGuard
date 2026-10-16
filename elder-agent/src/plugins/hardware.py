"""P9-1 第三方硬件适配抽象接口（M-ACC-04 / FR-CM-07）。

毫米波雷达、穿戴设备等第三方感知硬件的统一适配器抽象基类。
P2 接口预留：仅定义契约，不完整编码；接入方须实现抽象方法。
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class HardwareAdapter(ABC):
    """硬件设备适配器抽象基类：统一接入毫米波雷达 / 穿戴设备等感知硬件。"""

    def __init__(self, device_id: str):
        self.device_id = device_id

    @abstractmethod
    async def init(self) -> None:
        """初始化设备连接。"""

    @abstractmethod
    async def start(self) -> None:
        """启动设备采集。"""

    @abstractmethod
    async def stop(self) -> None:
        """停止设备并释放资源。"""

    @abstractmethod
    async def read_event(self) -> Optional[Dict[str, Any]]:
        """读取一次感知事件（跌倒 / 久坐 / 异常等），无事件返回 None。"""
