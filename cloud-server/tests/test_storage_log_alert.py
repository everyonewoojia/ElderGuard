"""P1-4 告警与监护日志存储：AlertLog/MonitorLog 写入与按 elder 查询（M-DATA-03）"""
import uuid

from app.services import storage


def _elder(db, name="孙奶奶"):
    owner = storage.create_user(db, username=f"l_owner_{uuid.uuid4().hex[:8]}", password_hash="h")
    return storage.create_elder(db, owner_id=owner.id, name=name)


def test_create_alert_and_query(db):
    elder = _elder(db)
    alert = storage.create_alert(
        db, elder_id=elder.id, alert_type="fall", priority="P0",
        content="疑似跌倒", status="pending",
    )
    assert alert.priority == "P0" and alert.status == "pending"
    alerts = storage.list_alerts_by_elder(db, elder.id)
    assert len(alerts) == 1 and alerts[0].alert_type == "fall"


def test_alert_status_update(db):
    """告警状态切换（pending→notified，并记录通知时间）"""
    elder = _elder(db)
    alert = storage.create_alert(db, elder_id=elder.id, alert_type="help", priority="P0", content="求助")
    updated = storage.update_alert_status(db, alert.id, "notified")
    assert updated.status == "notified" and updated.notified_at is not None


def test_update_alert_status_not_found(db):
    """更新不存在的告警返回 None"""
    assert storage.update_alert_status(db, 9999, "notified") is None


def test_alerts_scoped_by_elder(db):
    e1 = _elder(db, "甲")
    e2 = _elder(db, "乙")
    storage.create_alert(db, elder_id=e1.id, alert_type="fall", priority="P0")
    storage.create_alert(db, elder_id=e2.id, alert_type="fall", priority="P0")
    assert len(storage.list_alerts_by_elder(db, e1.id)) == 1


def test_create_log_and_query_scoped(db):
    """监护日志写入并按 elder 隔离查询"""
    e1 = _elder(db, "甲")
    e2 = _elder(db, "乙")
    storage.create_log(db, elder_id=e1.id, event_type="voice", content="对话")
    storage.create_log(db, elder_id=e2.id, event_type="sedentary", content="久坐")
    logs1 = storage.list_logs_by_elder(db, e1.id)
    logs2 = storage.list_logs_by_elder(db, e2.id)
    assert len(logs1) == 1 and logs1[0].event_type == "voice"
    assert len(logs2) == 1 and logs2[0].event_type == "sedentary"
