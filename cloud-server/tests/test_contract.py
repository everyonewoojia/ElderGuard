"""
接口契约校验测试（P0-3）
依据 docs/接口契约.md 用 Python 标准库校验样例负载的结构，
不依赖 jsonschema 等第三方库，保证本阶段测试可零依赖运行。
"""
from typing import Any
from uuid import uuid4

import pytest

EVENT_TYPES = {"fall", "sedentary", "voice", "config", "alert"}
PRIORITIES = {"P0", "P1", "P2"}
SOURCES = {"edge", "cloud", "family"}


def test_device_alert_contract_requires_canonical_client_identity():
    """The new edge contract is stricter than generic legacy event examples."""
    from pydantic import ValidationError
    from app.routers.edge import EdgeAlertIn
    identity = str(uuid4())
    body = {"elder_id": 1, "event_id": identity, "alert_type": "help"}
    assert EdgeAlertIn(**body).event_id == identity
    assert "event_id" in EdgeAlertIn.model_json_schema()["required"]
    for invalid in ({"elder_id": 1, "alert_type": "help"}, {**body, "event_id": "evt_legacy"}):
        with pytest.raises(ValidationError):
            EdgeAlertIn(**invalid)


def test_device_identity_cannot_be_supplied_as_alert_business_data():
    from app.routers.edge import EdgeAlertIn
    data = EdgeAlertIn(elder_id=1, event_id=str(uuid4()), alert_type="help", device_id="untrusted")
    assert "device_id" not in data.model_dump()

# 契约中约定的样例负载
SAMPLE_EVENT = {
    "eventId": "evt_1690000000.123",
    "type": "fall",
    "priority": "P0",
    "timestamp": "2024-07-22T10:00:00+08:00",
    "source": "edge",
    "payload": {"content": "检测到疑似跌倒"},
    "authToken": "",
}

SAMPLE_TASK = {
    "id": 1,
    "elder_id": 1,
    "task_type": "用药提醒",
    "title": "早晚各一次降压药",
    "content": "8:00 / 20:00 服用",
    "schedule_time": "08:00",
    "is_active": True,
}

SAMPLE_ALERT = {
    "id": 1,
    "elder_id": 1,
    "alert_type": "fall",
    "priority": "P0",
    "content": "检测到疑似跌倒",
    "status": "pending",
    "notified_at": None,
    "created_at": "2024-07-22T10:00:00+08:00",
}


def _validate_event(event: dict) -> list:
    """校验事件格式，返回不通过项列表（空表示通过）"""
    errors = []
    for key in ("eventId", "type", "priority", "timestamp", "source", "payload"):
        if key not in event:
            errors.append(f"缺少字段: {key}")
    if "type" in event and event["type"] not in EVENT_TYPES:
        errors.append(f"非法 type: {event['type']}")
    if "priority" in event and event["priority"] not in PRIORITIES:
        errors.append(f"非法 priority: {event['priority']}")
    if "source" in event and event["source"] not in SOURCES:
        errors.append(f"非法 source: {event['source']}")
    if not isinstance(event.get("payload", None), dict):
        errors.append("payload 必须为对象")
    return errors


def _validate_priority(priority: Any) -> bool:
    return priority in PRIORITIES


def test_event_contract_valid():
    """标准事件样例应完全符合契约"""
    assert _validate_event(SAMPLE_EVENT) == []


def test_event_contract_rejects_bad_type():
    """非法事件类型应被契约拒绝"""
    bad = dict(SAMPLE_EVENT, type="unknown")
    assert any("非法 type" in e for e in _validate_event(bad))


def test_event_contract_rejects_bad_priority():
    """非法优先级应被契约拒绝"""
    bad = dict(SAMPLE_EVENT, priority="P9")
    assert any("非法 priority" in e for e in _validate_event(bad))


def test_event_contract_rejects_missing_payload():
    """缺失 payload 应被契约拒绝"""
    bad = {k: v for k, v in SAMPLE_EVENT.items() if k != "payload"}
    assert any("payload" in e for e in _validate_event(bad))


def test_task_contract_fields():
    """提醒任务负载必备字段齐全且类型正确"""
    for key in ("id", "elder_id", "task_type", "title", "content", "schedule_time", "is_active"):
        assert key in SAMPLE_TASK, f"缺少字段: {key}"
    assert isinstance(SAMPLE_TASK["is_active"], bool)


def test_alert_contract_fields():
    """告警记录负载必备字段齐全且优先级合法"""
    for key in ("id", "elder_id", "alert_type", "priority", "content", "status", "created_at"):
        assert key in SAMPLE_ALERT, f"缺少字段: {key}"
    assert _validate_priority(SAMPLE_ALERT["priority"])
