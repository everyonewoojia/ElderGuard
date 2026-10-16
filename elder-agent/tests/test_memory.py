"""P4-3 记忆管理测试：短期会话 + 长期档案持久化（重启后仍可读，FR-DC-06）、偏好更新"""
import pytest

from src.core.memory import MemoryManager


async def test_save_and_get_context(tmp_data_dir):
    """保存事件后可读取短期上下文与最近历史"""
    mem = MemoryManager(str(tmp_data_dir))
    await mem.save({"type": "voice", "timestamp": "t1", "payload": {"content": "你好"}})
    ctx = await mem.get_context()
    assert len(ctx["short_term"]) == 1
    assert ctx["recent_history"][0]["content"] == "你好"


async def test_preference_persist_across_restart(tmp_data_dir):
    """偏好写入后，新实例（模拟重启）仍可读取（FR-DC-06）"""
    m1 = MemoryManager(str(tmp_data_dir))
    await m1.update_preference("服药时间", "08:00")
    m2 = MemoryManager(str(tmp_data_dir))
    assert await m2.get_preference("服药时间") == "08:00"


async def test_history_persist_across_restart(tmp_data_dir):
    """历史记忆写入后，新实例（模拟重启）仍可读取（FR-DC-06）"""
    m1 = MemoryManager(str(tmp_data_dir))
    await m1.save({"type": "voice", "timestamp": "t1", "payload": {"content": "记录"}})
    m2 = MemoryManager(str(tmp_data_dir))
    ctx = await m2.get_context()
    assert ctx["recent_history"][0]["content"] == "记录"


async def test_short_term_bounded(tmp_data_dir):
    """短期记忆有上限，超限只保留最近若干条"""
    mem = MemoryManager(str(tmp_data_dir))
    for i in range(120):
        await mem.save({"type": "x", "timestamp": str(i), "payload": {"content": str(i)}})
    assert len(mem.short_term) <= 50
