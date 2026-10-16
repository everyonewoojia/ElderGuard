"""P8-3 主动陪伴插件测试：基于记忆多轮闲聊 + 记忆衔接（FR-DC-08）"""
import pytest

from src.core.event_bus import EventBus
from src.core.memory import MemoryManager
from src.plugins.companion import CompanionPlugin


@pytest.fixture()
def plugin(tmp_path):
    bus = EventBus()
    mem = MemoryManager(data_dir=str(tmp_path / "mem"))
    return CompanionPlugin(bus, memory=mem)


@pytest.mark.asyncio
async def test_companion_records_and_replies(plugin):
    """第一轮：陪伴回复命中关键词规则，并写入记忆"""
    reply = await plugin.companion_reply("我今天要吃降压药")
    assert "吃药" in reply
    ctx = await plugin.memory().get_context()
    assert ctx["recent_history"], "对话应写入记忆"


@pytest.mark.asyncio
async def test_companion_memory_linkage(plugin):
    """多轮记忆衔接：第二轮问「记得…」时引用第一轮内容"""
    await plugin.companion_reply("明天早上要吃药")
    reply2 = await plugin.companion_reply("还记得我明天要做什么吗")
    assert "吃药" in reply2, f"应引用前文，实际: {reply2}"


@pytest.mark.asyncio
async def test_companion_no_history_fallback(plugin):
    """无历史时问「记得」给出兜底回复（不崩溃）"""
    reply = await plugin.companion_reply("还记得什么吗")
    assert reply  # 非空即可


@pytest.mark.asyncio
async def test_companion_respects_preference_name(plugin):
    """偏好（称呼）进入回复"""
    await plugin.memory().update_preference("name", "张奶奶")
    reply = await plugin.companion_reply("我有点累")
    assert "张奶奶" in reply


@pytest.mark.asyncio
async def test_companion_handle_event(plugin):
    """handle_event 处理 chat 事件"""
    await plugin.handle_event({"type": "chat", "payload": {"content": "无聊"}})
    ctx = await plugin.memory().get_context()
    assert any("无聊" in (i.get("content") or "") for i in ctx["recent_history"])
