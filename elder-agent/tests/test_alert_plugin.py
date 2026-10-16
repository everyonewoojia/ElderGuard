"""P5-2 安全告警插件测试：语音求助/跌倒/异常告警 + 本地语音告警兜底（KK04）、状态管理"""
import pytest

from src.core.event_bus import EventBus
from src.plugins.alert import AlertPlugin


class FakeTTS:
    def __init__(self):
        self.speaks = []

    async def speak(self, text):
        self.speaks.append(text)


async def _plugin():
    bus = EventBus()
    tts = FakeTTS()
    plugin = AlertPlugin(bus, tts=tts)
    await plugin.init()
    await plugin.start()
    return plugin, tts


async def test_help_local_voice_alert():
    """语音求助 → 记录告警 + 本地语音兜底（KK04）"""
    plugin, tts = await _plugin()
    await plugin.handle_event({"type": "help", "priority": "P0"})
    assert len(plugin.active_alerts()) >= 1
    assert len(tts.speaks) >= 1


async def test_fall_and_abnormal_alerts():
    """跌倒 / 异常事件均产生告警并本地语音提示"""
    plugin, tts = await _plugin()
    await plugin.handle_event({"type": "fall", "priority": "P0"})
    await plugin.handle_event({"type": "abnormal", "priority": "P1"})
    alerts = plugin.active_alerts()
    types = {a["type"] for a in alerts.values()}
    assert types == {"跌倒", "异常"}
    assert len(tts.speaks) >= 2


async def test_alert_status_and_cancel():
    """告警状态记录与手动取消"""
    plugin, _ = await _plugin()
    await plugin.handle_event({"type": "help", "priority": "P0"})
    alert_id = list(plugin.active_alerts().keys())[0]
    assert plugin.get_alert(alert_id)["status"] == "active"
    await plugin.cancel_alert(alert_id)
    assert plugin.get_alert(alert_id)["status"] == "cancelled"
