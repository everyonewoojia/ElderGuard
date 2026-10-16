"""P8-7 跌倒语音通话通道测试：呼叫建立或明确失败（FR-SC-07 / FR-FL-04，可模拟）"""
import pytest

from src.ai.call import CallService


@pytest.mark.asyncio
async def test_connect_valid_target():
    """有效目标 → 呼叫建立成功"""
    svc = CallService()
    ok = await svc.connect("13800000000")
    assert ok is True
    assert svc.is_connected()
    assert svc.target() == "13800000000"


@pytest.mark.asyncio
async def test_connect_invalid_target_fails():
    """无效目标 → 明确失败（返回 False，不崩溃）"""
    svc = CallService()
    assert await svc.connect("") is False
    assert await svc.connect("   ") is False
    assert not svc.is_connected()


@pytest.mark.asyncio
async def test_disconnect():
    """挂断成功"""
    svc = CallService()
    await svc.connect("13800000000")
    assert await svc.disconnect() is True
    assert not svc.is_connected()
    assert svc.target() is None


@pytest.mark.asyncio
async def test_disconnect_when_idle_fails():
    """未连接时挂断 → 明确失败"""
    svc = CallService()
    assert await svc.disconnect() is False
