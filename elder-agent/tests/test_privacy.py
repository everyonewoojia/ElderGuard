"""P5-5 隐私授权校验测试：感知模块执行前强制校验，未授权采集 0 次（FR-CM-05/AQ02/CON-B-04）"""
import pytest

from src.ai.asr import ASRService
from src.ai.intent import IntentService
from src.ai.tts import TTSService
from src.core.agent import ElderAgent
from src.core.event_bus import EventBus
from src.core.memory import MemoryManager
from src.core.privacy import PrivacyGuard


def test_privacy_guard_block_and_switch():
    """PrivacyGuard：授权放行、未授权拦截并计数"""
    guard = PrivacyGuard(enabled=True)
    assert guard.check("audio") is True
    guard.set_enabled(False)
    assert guard.check("audio") is False
    assert guard.blocked_count >= 1
    assert guard.capture_count == 0


async def test_asr_unauthorized_no_capture():
    """关闭授权后 ASR 不采集，采集次数为 0（AQ02/CON-B-04）"""
    guard = PrivacyGuard(enabled=False)
    asr = ASRService(privacy=guard)
    audio = await asr.listen()
    assert audio is None
    assert guard.capture_count == 0
    assert guard.blocked_count >= 1


class RecordingASR(ASRService):
    def __init__(self):
        self.listen_calls = 0
        self.recognize_calls = 0

    async def init(self):
        """Initialize the fake only after the shared guard permits it."""
        assert self.privacy.is_enabled()
        return True

    async def listen(self):
        self.listen_calls += 1
        return b"x"

    async def recognize(self, audio):
        self.recognize_calls += 1
        return "救命"


async def test_agent_sense_unauthorized_no_sense(tmp_data_dir):
    """Agent 感知入口：未授权则不进入采集，ASR 采集 0 次"""
    guard = PrivacyGuard(enabled=False)
    bus = EventBus()
    mem = MemoryManager(str(tmp_data_dir))
    asr = RecordingASR()
    agent = ElderAgent(
        event_bus=bus,
        memory=mem,
        asr=asr,
        tts=TTSService(),
        intent=IntentService(),
        privacy=guard,
    )
    await agent._sense()
    assert asr.listen_calls == 0
    assert guard.capture_count == 0


async def test_agent_sense_authorized_captures(tmp_data_dir):
    """授权开启后，Agent 感知进入采集流程"""
    guard = PrivacyGuard(enabled=True)
    bus = EventBus()
    mem = MemoryManager(str(tmp_data_dir))
    asr = RecordingASR()
    agent = ElderAgent(
        event_bus=bus,
        memory=mem,
        asr=asr,
        tts=TTSService(),
        intent=IntentService(),
        privacy=guard,
    )
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    agent.edge_client = SimpleNamespace(fetch_privacy=AsyncMock(return_value=True))
    agent.elder_id = 1
    assert await agent.sync_privacy() is True
    await agent._sense()
    assert asr.listen_calls == 1
