"""P1-1 模型定义：建表、约束、关系（M-DATA-01/02/03）"""
import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.models.models import User, Elder, ReminderTask, AlertLog, MonitorLog


def test_all_tables_created(db):
    """五张表均能成功建表"""
    inspector = inspect(db.get_bind())
    tables = set(inspector.get_table_names())
    assert {"users", "elders", "reminder_tasks", "alert_logs", "monitor_logs"} <= tables


def test_user_username_unique(db):
    """用户名唯一约束生效"""
    db.add(User(username="alice", password_hash="x"))
    db.commit()
    db.add(User(username="alice", password_hash="y"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_user_phone_unique(db):
    """手机号唯一约束生效"""
    db.add(User(username="u1", password_hash="x", phone="13800000000"))
    db.commit()
    db.add(User(username="u2", password_hash="y", phone="13800000000"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_elder_owner_relationship(db):
    """elder.owner / owner.elders 关系正确"""
    owner = User(username="owner1", password_hash="x")
    db.add(owner)
    db.commit()
    elder = Elder(owner_id=owner.id, name="张奶奶")
    db.add(elder)
    db.commit()
    db.refresh(owner)
    db.refresh(elder)
    assert elder.owner.username == "owner1"
    assert [e.name for e in owner.elders] == ["张奶奶"]


def test_elder_children_relationships(db):
    """elder→tasks/logs/alerts 关系正确（P1-1 关系覆盖）"""
    elder = Elder(name="李爷爷")
    db.add(elder)
    db.commit()
    db.refresh(elder)
    db.add(ReminderTask(elder_id=elder.id, task_type="用药提醒", title="吃药"))
    db.add(MonitorLog(elder_id=elder.id, event_type="voice", content="对话"))
    db.add(AlertLog(elder_id=elder.id, alert_type="fall", priority="P0", content="跌倒"))
    db.commit()
    db.refresh(elder)
    assert len(elder.tasks) == 1
    assert len(elder.logs) == 1
    assert len(elder.alerts) == 1
    assert elder.tasks[0].elder.id == elder.id
    assert elder.alerts[0].elder.id == elder.id
