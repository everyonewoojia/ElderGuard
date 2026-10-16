"""P9 P2 接口预留契约测试（老人端）：抽象接口定义完整、可导入、子类实现后可实例化。"""
import pytest

from src.plugins import emotion, hardware
from src.plugins.base import BasePlugin
from src.ai import multimodal


class TestHardwareAdapter:
    def test_abstract_cannot_instantiate(self):
        """抽象基类不可直接实例化（契约强制实现）。"""
        with pytest.raises(TypeError):
            hardware.HardwareAdapter("radar-1")

    def test_concrete_subclass_works(self):
        """实现全部抽象方法后可实例化并保留 device_id。"""
        class Radar(hardware.HardwareAdapter):
            async def init(self): pass
            async def start(self): pass
            async def stop(self): pass
            async def read_event(self): return None
        radar = Radar("radar-1")
        assert radar.device_id == "radar-1"
        assert callable(radar.read_event)

    def test_contract_methods_present(self):
        """契约方法齐全（init/start/stop/read_event）。"""
        for name in ("init", "start", "stop", "read_event"):
            assert hasattr(hardware.HardwareAdapter, name)


class TestEmotionPlugin:
    def test_abstract_cannot_instantiate(self):
        """抽象插件不可直接实例化。"""
        with pytest.raises(TypeError):
            emotion.EmotionPlugin(None)

    def test_concrete_subclass_is_base_plugin_and_recognizes(self):
        """实现类既满足 BasePlugin 生命周期，又具备 recognize_emotion。"""
        class Emo(emotion.EmotionPlugin):
            async def init(self): pass
            async def start(self): pass
            async def stop(self): pass
            async def handle_event(self, event): pass
            async def recognize_emotion(self, text, features=None):
                return {"emotion": "neutral", "confidence": 0.5}
        e = Emo(None)
        assert isinstance(e, BasePlugin)
        assert callable(e.recognize_emotion)

    def test_contract_method_present(self):
        assert hasattr(emotion.EmotionPlugin, "recognize_emotion")


class TestMultimodalFacade:
    def test_abstract_cannot_instantiate(self):
        with pytest.raises(TypeError):
            multimodal.MultimodalFacade()

    def test_concrete_facade_fuses(self):
        class F(multimodal.MultimodalFacade):
            def fuse(self, signals): return {"result": "ok"}
        assert F().fuse({"asr": "你好"})["result"] == "ok"
