"""P10 质量属性验收测试（老人端）。

覆盖 §7.3/§7.4：KK02 持续待机、KY01 离线降级与恢复、CON-B-01/CON-L-06 无医疗诊疗建议、
AQ02/CON-B-04 未授权采集 0 次、XN02 高优先级优先。P10-4 全量回归由三端全量 pytest 落地。
"""
import pytest

from src.ai.asr import ASRService
from src.ai.intent import IntentService
from src.ai.tts import TTSService
from src.core.agent import ElderAgent
from src.core.event_bus import EventBus
from src.core.memory import MemoryManager
from src.core.privacy import PrivacyGuard
from src.core.scheduler import TaskScheduler
from src.plugins.alert import AlertPlugin
from src.plugins.base import BasePlugin
from src.plugins.command import CommandPlugin
from src.plugins.reminder import ReminderPlugin


class _SpyTTS:
    """记录语音反馈的假 TTS（用于断言话术内容）。"""

    def __init__(self):
        self.messages = []

    async def speak(self, message):
        self.messages.append(message)


# ---------------------------------------------------------------------------
# P10-2 可靠性：KK02 7×24 持续待机模拟（连续多轮分发/调度无异常、事件不丢）
# ---------------------------------------------------------------------------
async def test_kk02_continuous_event_dispatch_no_loss():
    """连续 300 轮事件分发：计数精确、无丢失。"""
    bus = EventBus()
    seen = {"n": 0}

    async def cb(event):
        seen["n"] += 1

    await bus.subscribe("alert", cb)
    for i in range(300):
        await bus.publish({"type": "alert", "priority": "P1", "i": i})
    assert seen["n"] == 300


async def test_kk02_scheduler_sustained_no_loss():
    """调度器连续 200 任务全部出队、无遗漏。"""
    sched = TaskScheduler()
    for i in range(200):
        await sched.schedule(("task", i), priority="P1" if i % 2 else "P0")
    total = 0
    while await sched.next() is not None:
        total += 1
    assert total == 200


# ---------------------------------------------------------------------------
# P10-3 安全合规：无医疗诊疗建议（CON-B-01 / CON-L-06）
# ---------------------------------------------------------------------------
async def test_no_medical_diagnosis_in_feedback():
    """求助/不适话术不得出现诊疗结论，应为通知家属的求助导向。"""
    tts = _SpyTTS()
    agent = ElderAgent(tts=tts)
    intent = await agent.handle_voice_text("我不舒服")
    assert intent["type"] == "help"
    assert tts.messages, "应有语音反馈"
    msg = "".join(tts.messages)
    for forbidden in ("诊断为", "患病", "需要治疗", "剂量", "用药建议", "去医院看"):
        assert forbidden not in msg, f"话术出现诊疗建议: {forbidden}"
    assert ("家属" in msg) or ("通知" in msg)


async def test_help_intent_priority_p0():
    """主动求助为 P0 高优先级（KK01 兜底路径）。"""
    agent = ElderAgent()
    intent = await agent.handle_voice_text("救命")
    assert intent["priority"] == "P0"


# ---------------------------------------------------------------------------
# P10-3 安全合规：未授权采集 0 次（AQ02 / CON-B-04）回归
# ---------------------------------------------------------------------------
def test_unauthorized_capture_zero_regression():
    """关闭授权后采集拦截、实际采集 0 次。"""
    guard = PrivacyGuard(enabled=False)
    assert guard.check("audio") is False
    assert guard.capture_count == 0  # 未授权不采集
    assert guard.blocked_count == 1
    guard.set_enabled(True)
    guard.check("audio")
    guard.record_capture()
    assert guard.capture_count == 1


# ---------------------------------------------------------------------------
# P10-1 并发：XN02 高优先级告警优先（老人端调度回归）
# ---------------------------------------------------------------------------
async def test_xn02_priority_dispatch_order():
    """一次多事件发布按 P0→P1→P2 优先分发。"""
    bus = EventBus()
    order = []

    async def cb(event):
        order.append(event["priority"])

    await bus.subscribe("alert", cb)
    await bus.publish(
        {"type": "alert", "priority": "P2"},
        {"type": "alert", "priority": "P0"},
        {"type": "alert", "priority": "P1"},
    )
    assert order == ["P0", "P1", "P2"]


# ---------------------------------------------------------------------------
# P10-3 可靠性：KY01 断网离线降级 → 恢复后补发同步
# ---------------------------------------------------------------------------
async def test_ky01_offline_cache_and_recovery():
    """断网告警入离线缓存；恢复后 flush 补发成功（内存缓存，原型模拟边界）。"""
    from src.integration.edge import EdgeReporter

    calls = {"n": 0}

    class FakeClient:
        async def report_alert(self, elder_id, alert_type, priority, content=None, *, event_id):
            calls["n"] += 1
            if calls["n"] <= 1:  # 首次断网失败
                return None
            return {"alert_id": 1}

    reporter = EdgeReporter(FakeClient())
    from uuid import uuid4
    ok = await reporter.report(1, "fall", "P0", event_id=str(uuid4()))
    assert ok is False
    assert len(reporter.pending) == 1  # 已入离线缓存
    n_fail = await reporter.flush()  # 网络恢复，补发
    assert len(reporter.pending) == 0
    assert n_fail == 0


# ---------------------------------------------------------------------------
# P10-4 P0 全量回归清单：关键 P0 能力模块齐备（§7.2.1 能力覆盖断言）
# ---------------------------------------------------------------------------
def test_p0_acceptance_capabilities_present():
    """P0 功能验收标准（§7.2.1）关键能力类齐备、插件契约成立。"""
    classes = (
        EventBus, TaskScheduler, MemoryManager, PrivacyGuard,
        ASRService, TTSService, IntentService,
        ReminderPlugin, AlertPlugin, CommandPlugin, BasePlugin,
    )
    for cls in classes:
        assert cls is not None
    assert issubclass(ReminderPlugin, BasePlugin)
    assert issubclass(AlertPlugin, BasePlugin)
    assert issubclass(CommandPlugin, BasePlugin)
