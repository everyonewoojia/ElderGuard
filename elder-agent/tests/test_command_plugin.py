"""P5-3 指令解析插件测试：语音/按键指令匹配正确率（FR-MI-01/03）、技能匹配执行（FR-DC-05）"""
import pytest

from src.core.event_bus import EventBus
from src.plugins.command import CommandPlugin


class FakeTTS:
    def __init__(self):
        self.speaks = []

    async def speak(self, text):
        self.speaks.append(text)


def test_match_accuracy_5_commands():
    """5 条常见指令，正确匹配 ≥4 条（FR-MI-01/03）"""
    plugin = CommandPlugin(EventBus(), tts=FakeTTS())
    cases = {
        "帮我叫家属": "help",
        "请帮帮忙": "help",
        "我该吃药了": "reminder",
        "提醒我按时服药": "reminder",
        "现在几点钟": "time",
    }
    correct = sum(
        1 for text, expected in cases.items()
        if plugin._match_command(text) == expected
    )
    assert correct >= 4, f"仅匹配 {correct}/5 条"


async def test_skill_execution_records():
    """匹配到指令后执行对应技能并记录"""
    plugin = CommandPlugin(EventBus(), tts=FakeTTS())
    await plugin.handle_event({"payload": {"text": "现在几点"}})
    assert "time" in plugin.executed
    await plugin.handle_event({"payload": {"text": "帮我叫家属"}})
    assert "help" in plugin.executed
    await plugin.handle_event({"payload": {"text": "我该吃药了"}})
    assert "reminder" in plugin.executed


async def test_unmatched_command_not_executed():
    """未匹配指令不执行技能"""
    plugin = CommandPlugin(EventBus(), tts=FakeTTS())
    await plugin.handle_event({"payload": {"text": "今天天气怎么样"}})
    assert plugin.executed == []
