"""P5-4 AI 门面测试：统一封装 + 外部 API 失败兜底（本地话术/规则匹配，FR-CM-06/CON-D-06）"""
import pytest

from src.ai.asr import ASRService
from src.ai.intent import IntentService
from src.ai.tts import TTSService


async def test_intent_cloud_none_uses_rule():
    """云端意图为空时回退到本地规则匹配"""
    svc = IntentService()
    intent = await svc.parse("救命")
    assert intent["type"] == "help"
    assert intent["priority"] == "P0"


class FailingCloud(IntentService):
    async def _cloud_parse(self, text):
        raise RuntimeError("cloud down")


async def test_intent_cloud_failure_falls_back_to_rule():
    """云端意图异常时回退到本地规则（CON-D-06）"""
    svc = FailingCloud()
    intent = await svc.parse("摔倒")
    assert intent["type"] == "fall"


async def test_asr_cloud_failure_returns_none():
    """云端识别失败/未配置返回 None（由上层处理），不崩溃"""
    svc = ASRService()
    result = await svc._cloud_recognize(b"x")
    assert result is None


async def test_tts_synthesis_failure_no_crash():
    """TTS 合成失败不崩溃，返回 False"""
    svc = TTSService()
    result = await svc.speak("你好")
    assert result is False


async def test_asr_recognize_no_recognizer_falls_to_cloud():
    """无本地识别器时回退云端（失败返回 None），不崩溃（CON-D-06）"""
    svc = ASRService()
    result = await svc.recognize(b"audio")
    assert result is None


async def test_asr_init_no_crash():
    """ASR 初始化在模型缺失/依赖未装时不崩溃（外部依赖退化）"""
    svc = ASRService()
    await svc.init()


async def test_intent_rule_branches():
    """本地规则各意图分支：reminder / time / unknown"""
    svc = IntentService()
    assert (await svc.parse("提醒我吃药"))["type"] == "reminder"
    assert (await svc.parse("现在几点"))["type"] == "time"
    assert (await svc.parse("随便聊聊"))["type"] == "unknown"
