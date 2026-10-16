"""P5-1 提醒插件测试：用药/作息提醒、久坐 4 小时触发并写日志（FR-SC-03）、任务状态（FR-CM-02）"""
from datetime import datetime, timedelta

import pytest

from src.core.event_bus import EventBus
from src.core.memory import MemoryManager
from src.plugins.reminder import ReminderPlugin


async def test_send_reminder_records_task():
    """发送提醒后任务被记录，初始状态 pending"""
    plugin = ReminderPlugin(EventBus())
    await plugin.handle_event({
        "payload": {"task_type": "用药提醒", "title": "降压药", "schedule_time": "08:00"}
    })
    tasks = plugin.get_task_status()
    assert len(tasks) == 1
    task = list(tasks.values())[0]
    assert task["title"] == "降压药"
    assert task["status"] == "pending"


async def test_task_complete_switch():
    """任务完成/未完成状态切换（FR-CM-02）"""
    plugin = ReminderPlugin(EventBus())
    await plugin.handle_event({"payload": {"task_type": "用药提醒", "title": "药"}})
    task_id = list(plugin.get_task_status().keys())[0]
    await plugin.mark_completed(task_id)
    assert plugin.get_task_status(task_id)["status"] == "completed"
    await plugin.mark_pending(task_id)
    assert plugin.get_task_status(task_id)["status"] == "pending"


class _Clock:
    """可推进时间的假时钟，用于久坐阈值测试"""

    def __init__(self):
        self.t = datetime(2026, 1, 1, 8, 0, 0)

    def now(self):
        return self.t

    def advance(self, hours: float):
        self.t += timedelta(hours=hours)


async def test_sedentary_4h_triggers_and_logs(tmp_data_dir):
    """久坐超过 4 小时触发提醒并写日志（FR-SC-03）"""
    clock = _Clock()
    mem = MemoryManager(str(tmp_data_dir))
    plugin = ReminderPlugin(EventBus(), memory=mem, now=clock.now)
    await plugin._setup_sedentary_reminder("请起身活动")

    # 未满 4 小时不触发
    clock.advance(2)
    await plugin.check_sedentary()
    assert plugin._sedentary_triggered is False

    # 超过 4 小时触发提醒并写日志
    clock.advance(3)  # 累计 5 小时
    await plugin.check_sedentary()
    assert plugin._sedentary_triggered is True
    assert len(mem.long_term["history"]) >= 1
    assert mem.long_term["history"][-1]["type"] == "sedentary"


async def test_sedentary_no_start_no_trigger(tmp_data_dir):
    """未设置久坐起点则不触发"""
    mem = MemoryManager(str(tmp_data_dir))
    plugin = ReminderPlugin(EventBus(), memory=mem)
    await plugin.check_sedentary()
    assert plugin._sedentary_triggered is False
    assert len(mem.long_term["history"]) == 0
