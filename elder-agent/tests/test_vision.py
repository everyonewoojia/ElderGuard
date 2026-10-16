"""P8-4 视觉行为识别接口测试：行为识别门面 + 疑似跌倒 10 秒无应答告警（FR-SC-02/04）"""
from datetime import datetime, timezone, timedelta

import pytest

from src.ai.vision import VisionService


def make_clock(start=None):
    """可变的可测时钟"""
    now = [start or datetime(2026, 10, 16, 8, 0, 0, tzinfo=timezone.utc)]
    return now, (lambda: now[0])


@pytest.mark.asyncio
async def test_detect_fall_starts_verification():
    """疑似跌倒（高置信）→ 进入 verify 计时"""
    now, clock = make_clock()
    svc = VisionService(clock=clock)
    r = svc.detect("fall", confidence=0.8)
    assert r["action"] == "verify_fall"
    assert svc.has_pending_fall()


@pytest.mark.asyncio
async def test_10s_no_response_triggers_alert():
    """疑似跌倒 10 秒无应答 → 进入告警流程（FR-SC-04）"""
    now, clock = make_clock()
    triggered = []
    svc = VisionService(clock=clock, on_alert=lambda a: triggered.append(a))
    svc.detect("fall", 0.8)
    now[0] = now[0] + timedelta(seconds=11)
    alert = await svc.check_timeout()
    assert alert is not None and alert["type"] == "fall"
    assert triggered, "应触发 on_alert 告警回调"


@pytest.mark.asyncio
async def test_confirm_before_timeout_no_alert():
    """时限内确认无碍 → 不告警"""
    now, clock = make_clock()
    triggered = []
    svc = VisionService(clock=clock, on_alert=lambda a: triggered.append(a))
    svc.detect("fall", 0.8)
    svc.confirm()
    now[0] = now[0] + timedelta(seconds=20)
    alert = await svc.check_timeout()
    assert alert is None
    assert not triggered


@pytest.mark.asyncio
async def test_under_10s_no_alert_yet():
    """未到 10 秒且未确认 → 暂不告警"""
    now, clock = make_clock()
    triggered = []
    svc = VisionService(clock=clock, on_alert=lambda a: triggered.append(a))
    svc.detect("fall", 0.8)
    now[0] = now[0] + timedelta(seconds=5)
    alert = await svc.check_timeout()
    assert alert is None
    assert not triggered


@pytest.mark.asyncio
async def test_detect_low_confidence_no_fall():
    """低置信不触发疑似跌倒"""
    now, clock = make_clock()
    svc = VisionService(clock=clock)
    r = svc.detect("fall", confidence=0.2)
    assert r["action"] == "record"
    assert not svc.has_pending_fall()
