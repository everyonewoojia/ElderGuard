"""
基础插件类 - 所有插件必须实现的接口
"""
from abc import ABC, abstractmethod
from typing import Any


class BasePlugin(ABC):
    def __init__(self, event_bus):
        self.event_bus = event_bus
    
    @abstractmethod
    async def init(self) -> None:
        """插件初始化"""
        pass
    
    @abstractmethod
    async def start(self) -> None:
        """启动插件"""
        pass
    
    @abstractmethod
    async def stop(self) -> None:
        """停止插件"""
        pass
    
    @abstractmethod
    async def handle_event(self, event: dict) -> None:
        """处理事件"""
        pass
