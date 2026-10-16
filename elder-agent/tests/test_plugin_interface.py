"""P4-5 插件统一接口测试：BasePlugin 抽象约束，新增插件无需改内核（XG01）"""
import pytest

from src.core.event_bus import EventBus
from src.plugins.base import BasePlugin


class CustomPlugin(BasePlugin):
    """自定义插件：实现标准接口，不要求内核改动"""

    def __init__(self, event_bus):
        super().__init__(event_bus)
        self.init_called = False
        self.start_called = False
        self.stop_called = False
        self.events = []

    async def init(self):
        self.init_called = True

    async def start(self):
        self.start_called = True

    async def stop(self):
        self.stop_called = True

    async def handle_event(self, event):
        self.events.append(event)


def test_plugin_abstract_cannot_instantiate():
    """BasePlugin 为抽象类，不可直接实例化（契约强制）"""
    with pytest.raises(TypeError):
        BasePlugin(EventBus())


async def test_custom_plugin_lifecycle_via_bus():
    """新增插件实现标准接口即可接入总线分发，无需修改内核（XG01）"""
    bus = EventBus()
    plugin = CustomPlugin(bus)
    await plugin.init()
    await plugin.start()
    assert plugin.init_called and plugin.start_called

    await bus.subscribe("custom", plugin.handle_event)
    await bus.publish({"type": "custom", "priority": "P1"})
    assert len(plugin.events) == 1

    await plugin.stop()
    assert plugin.stop_called
