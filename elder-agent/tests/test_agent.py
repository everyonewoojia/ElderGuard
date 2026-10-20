"""P4-4 Agent 闭环骨架测试：感知→理解→规划→执行→反馈→记忆（mock），唤醒响应 ≤2s（FR-DC-07）"""
import time

import pytest

from src.ai.asr import ASRService
from src.ai.intent import IntentService
from src.ai.tts import TTSService
from src.core.agent import ElderAgent
from src.core.event_bus import EventBus
from src.core.memory import MemoryManager
from src.plugins.alert import AlertPlugin


class FakeASR(ASRService):
    async def listen(self):
        return b"fake"

    async def recognize(self, audio):
        return "救命"


class FakeIntent(IntentService):
    async def parse(self, text):
        return {"type": "help", "priority": "P0", "content": text}


class FakeTTS(TTSService):
    def __init__(self):
        super().__init__()
        self.speaks = []

    async def speak(self, text):
        self.speaks.append(text)

    async def _synthesize(self, text):
        return b"audio"

    async def _play(self, audio):
        pass


def _make_agent(tmp_data_dir, intent_cls=FakeIntent):
    bus = EventBus()
    mem = MemoryManager(str(tmp_data_dir))
    agent = ElderAgent(
        event_bus=bus,
        memory=mem,
        asr=FakeASR(),
        tts=FakeTTS(),
        intent=intent_cls(),
    )
    alert_plugin = AlertPlugin(bus)
    agent.plugins = [alert_plugin]
    return agent, alert_plugin


async def test_closed_loop_help_flow(tmp_data_dir):
    """语音求助 → 理解→规划→执行→反馈→记忆 全链路打通"""
    agent, alert_plugin = _make_agent(tmp_data_dir)
    await alert_plugin.init()
    await alert_plugin.start()

    await agent.handle_voice_text("救命")

    # 反馈（TTS 输出）
    assert len(agent.tts.speaks) >= 1
    # 执行（告警被记录）
    assert len(alert_plugin._active_alerts) >= 1
    # 记忆（历史写入）
    assert len(agent.memory.long_term["history"]) >= 1


async def test_wakeup_response_within_2s(tmp_data_dir):
    """休眠唤醒响应 ≤2s（FR-DC-07）"""
    agent, alert_plugin = _make_agent(tmp_data_dir)
    await alert_plugin.init()
    await alert_plugin.start()

    start = time.perf_counter()
    await agent.handle_voice_text("现在几点")
    elapsed = time.perf_counter() - start
    assert elapsed <= 2.0


async def test_unknown_intent_no_alert(tmp_data_dir):
    """未知意图不触发告警，仅做反馈"""
    class UnknownIntent(IntentService):
        async def parse(self, text):
            return {"type": "unknown", "priority": "P2", "content": text}

    agent, alert_plugin = _make_agent(tmp_data_dir, intent_cls=UnknownIntent)
    await alert_plugin.init()
    await alert_plugin.start()

    await agent.handle_voice_text("随便说说")
    assert len(alert_plugin._active_alerts) == 0
    assert len(agent.tts.speaks) >= 1


async def test_default_construction_works(tmp_path, monkeypatch):
    """默认构造（无注入）仍可创建 Agent，接口契约保持"""
    monkeypatch.chdir(tmp_path)  # Default data/ must be fresh, never a deployment profile.
    agent = ElderAgent()
    assert agent.event_bus is not None
    assert agent.memory is not None
    assert agent.asr is not None and agent.tts is not None and agent.intent is not None


async def test_understand_plan_publishes_event(tmp_data_dir):
    """理解→规划：_understand 解析意图并经 _plan 发布事件，插件执行"""
    agent, alert_plugin = _make_agent(tmp_data_dir)
    await alert_plugin.init()
    await alert_plugin.start()

    await agent._understand("救命")
    assert len(alert_plugin._active_alerts) >= 1


async def test_execute_publishes_action(tmp_data_dir):
    """执行模块：execute 把动作事件经总线分发，插件响应"""
    agent, alert_plugin = _make_agent(tmp_data_dir)
    await alert_plugin.init()
    await alert_plugin.start()

    await agent.execute({"type": "help", "priority": "P0", "payload": {"content": "执行"}})
    assert len(alert_plugin._active_alerts) >= 1


async def test_feedback_direct(tmp_data_dir):
    """反馈模块：_feedback 调用 TTS 输出"""
    agent, _ = _make_agent(tmp_data_dir)
    await agent._feedback("你好")
    assert agent.tts.speaks == ["你好"]


async def test_remember_direct(tmp_data_dir):
    """记忆模块：_remember 写入档案"""
    agent, _ = _make_agent(tmp_data_dir)
    await agent._remember({"type": "voice", "payload": {"content": "记录"}})
    assert len(agent.memory.long_term["history"]) >= 1


async def test_fall_and_reminder_feedback_branches(tmp_data_dir):
    """fall / reminder 意图的反馈话术分支"""
    class BranchIntent(IntentService):
        async def parse(self, text):
            if text == "摔倒了":
                return {"type": "fall", "priority": "P0", "content": text}
            return {"type": "reminder", "priority": "P1", "content": text}

    agent, alert_plugin = _make_agent(tmp_data_dir, intent_cls=BranchIntent)
    await alert_plugin.init()
    await alert_plugin.start()

    await agent.handle_voice_text("摔倒了")
    assert len(alert_plugin._active_alerts) >= 1

    await agent.handle_voice_text("记得吃药")
    assert "提醒" in agent.tts.speaks[-1]


async def test_start_stop_lifecycle(tmp_data_dir):
    """Agent start/stop 生命周期：插件初始化启动、主循环创建、停止后退出"""
    import asyncio

    agent, _ = _make_agent(tmp_data_dir)
    await agent.start()
    assert agent._running is True
    await asyncio.sleep(0.1)
    await agent.stop()
    assert agent._running is False
