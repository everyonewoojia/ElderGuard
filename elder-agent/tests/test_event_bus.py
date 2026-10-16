"""P4-1 事件总线测试：发布/订阅/取消订阅、按 priority 优先分发、插件异常隔离（KK04）"""
import pytest

from src.core.event_bus import EventBus


async def test_subscribe_and_publish():
    """订阅后发布事件，回调被调用"""
    bus = EventBus()
    received = []

    async def cb(event):
        received.append(event["type"])

    await bus.subscribe("voice", cb)
    await bus.publish({"type": "voice", "priority": "P1"})
    assert received == ["voice"]


async def test_unsubscribe():
    """取消订阅后不再收到事件"""
    bus = EventBus()
    received = []

    async def cb(event):
        received.append(1)

    await bus.subscribe("x", cb)
    await bus.unsubscribe("x", cb)
    await bus.publish({"type": "x", "priority": "P1"})
    assert received == []


async def test_priority_dispatch_order():
    """一次发布多个事件时，按 priority 从高到低（P0→P1→P2）优先分发"""
    bus = EventBus()
    order = []

    async def cb(event):
        order.append(event["priority"])

    await bus.subscribe("x", cb)
    await bus.publish(
        {"type": "x", "priority": "P1"},
        {"type": "x", "priority": "P0"},
        {"type": "x", "priority": "P2"},
    )
    assert order == ["P0", "P1", "P2"]


async def test_subscriber_exception_isolation():
    """一个订阅者抛异常，不影响其他订阅者继续处理（KK04）"""
    bus = EventBus()
    called = []

    async def bad(event):
        raise RuntimeError("boom")

    async def good(event):
        called.append(event["type"])

    await bus.subscribe("x", bad)
    await bus.subscribe("x", good)
    await bus.publish({"type": "x", "priority": "P1"})
    assert called == ["x"]


async def test_multiple_subscribers_same_type():
    """同一类型可挂多个订阅者，全部被调用"""
    bus = EventBus()
    hits = []

    async def a(event):
        hits.append("a")

    async def b(event):
        hits.append("b")

    await bus.subscribe("x", a)
    await bus.subscribe("x", b)
    await bus.publish({"type": "x", "priority": "P1"})
    assert set(hits) == {"a", "b"}


def test_get_subscribers():
    """get_subscribers 返回当前订阅者列表"""
    bus = EventBus()
    assert bus.get_subscribers("nope") == []
