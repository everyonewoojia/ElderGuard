"""
smoke 测试（P0 基线）
- 仅验证核心模块可导入、可构造、插件接口契约成立
- 本阶段不承载任何业务测试，业务测试从 P4 起逐步加入
"""
import inspect


def test_core_modules_importable():
    """老人端核心模块可正常导入并可构造"""
    from src.core.event_bus import EventBus
    from src.core.memory import MemoryManager
    from src.core.agent import ElderAgent

    assert EventBus() is not None
    assert MemoryManager() is not None
    assert ElderAgent() is not None


def test_ai_modules_importable():
    """AI 能力模块可正常导入并可构造（门面骨架）"""
    from src.ai.asr import ASRService
    from src.ai.tts import TTSService
    from src.ai.intent import IntentService

    assert ASRService() is not None
    assert TTSService() is not None
    assert IntentService() is not None


def test_plugin_base_interface():
    """BasePlugin 为抽象类且具备统一插件接口（init/start/stop/handle_event）"""
    from src.plugins.base import BasePlugin

    assert inspect.isabstract(BasePlugin)
    for method in ("init", "start", "stop", "handle_event"):
        assert hasattr(BasePlugin, method), f"缺少插件接口方法: {method}"


def test_plugins_importable():
    """三个 P0 业务插件可导入并可构造"""
    from src.plugins.alert import AlertPlugin
    from src.plugins.command import CommandPlugin
    from src.plugins.reminder import ReminderPlugin
    from src.core.event_bus import EventBus

    bus = EventBus()
    assert AlertPlugin(bus) is not None
    assert CommandPlugin(bus) is not None
    assert ReminderPlugin(bus) is not None
