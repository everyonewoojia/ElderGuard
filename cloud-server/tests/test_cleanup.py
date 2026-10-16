"""P8-6 日志定期清理测试：清理老旧日志、不影响核心日志查询与告警（M-DATA-04 / FR-CM-09）"""
from datetime import datetime, timezone, timedelta

from app.models.models import MonitorLog
from app.services import storage


def _setup(db):
    owner = storage.create_user(db, "u_cleanup", "hash", role="family")
    elder = storage.create_elder(db, owner.id, "张奶奶")
    return owner, elder


def test_cleanup_removes_old_keeps_recent(db):
    """清理 created_at 早于 cutoff 的日志，保留近期日志"""
    _, elder = _setup(db)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=40)
    old_log = MonitorLog(elder_id=elder.id, event_type="voice", content="旧日志", created_at=old)
    db.add(old_log)
    db.commit()
    old_log_id = old_log.id  # 删除前先取 id（delete(synchronize_session=False) 后对象不可再访问）
    new_log = storage.create_log(db, elder.id, "voice", "新日志")

    removed = storage.cleanup_old_monitor_logs(db, now - timedelta(days=30))
    assert removed >= 1
    logs = storage.list_logs_by_elder(db, elder.id)
    ids = [l.id for l in logs]
    assert old_log_id not in ids
    assert new_log.id in ids


def test_cleanup_does_not_touch_alerts(db):
    """清理监护日志不影响告警日志（AlertLog 保留）"""
    _, elder = _setup(db)
    now = datetime.now(timezone.utc)
    old_log = MonitorLog(elder_id=elder.id, event_type="voice", content="旧日志", created_at=now - timedelta(days=40))
    db.add(old_log)
    db.commit()
    alert = storage.create_alert(db, elder.id, "fall", "P0", "跌倒", status="notified")
    db.add(MonitorLog(elder_id=elder.id, event_type="voice", content="旧", created_at=now - timedelta(days=60)))
    db.commit()

    storage.cleanup_old_monitor_logs(db, now - timedelta(days=30))
    alerts = storage.list_alerts_by_elder(db, elder.id)
    assert any(a.id == alert.id for a in alerts)
