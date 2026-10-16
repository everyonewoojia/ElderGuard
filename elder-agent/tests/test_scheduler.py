"""P4-2 任务规划调度器测试：风险分级、高优先级告警优先调度"""
import pytest

from src.core.scheduler import TaskScheduler


async def test_priority_schedule_order():
    """按 priority 入队后，next() 依 P0→P1→P2 顺序取出（高优先级优先）"""
    sched = TaskScheduler()
    await sched.schedule("low", priority="P2")
    await sched.schedule("urgent", priority="P0")
    await sched.schedule("mid", priority="P1")
    assert await sched.next() == "urgent"
    assert await sched.next() == "mid"
    assert await sched.next() == "low"


async def test_empty_queue_returns_none():
    """空队列 next() 返回 None"""
    sched = TaskScheduler()
    assert await sched.next() is None


async def test_run_next_executes_highest_priority():
    """run_next() 执行最高优先级任务（可调用对象）"""
    sched = TaskScheduler()
    executed = []
    await sched.schedule(lambda: executed.append("P2"), priority="P2")
    await sched.schedule(lambda: executed.append("P0"), priority="P0")
    await sched.run_next()
    assert executed == ["P0"]


async def test_queue_size_and_clear():
    """队列 size() 与 clear()"""
    sched = TaskScheduler()
    await sched.schedule("a", "P1")
    await sched.schedule("b", "P1")
    assert sched.size() == 2
    sched.clear()
    assert sched.size() == 0
