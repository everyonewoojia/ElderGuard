from sqlalchemy import Boolean, Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from app.core.database import Base


def _utcnow():
    """返回带时区的当前 UTC 时间（替代已弃用的 datetime.utcnow）。"""
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True)
    password_hash = Column(String(128))
    role = Column(String(20), default="family")
    phone = Column(String(20), unique=True)
    created_at = Column(DateTime, default=_utcnow)
    is_active = Column(Boolean, default=True)


class Elder(Base):
    __tablename__ = "elders"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50))
    age = Column(Integer)
    phone = Column(String(20))
    address = Column(Text)
    privacy_enabled = Column(Boolean, default=False)
    owner_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=_utcnow)
    
    owner = relationship("User", backref="elders")
    tasks = relationship("ReminderTask", backref="elder")
    logs = relationship("MonitorLog", backref="elder")
    alerts = relationship("AlertLog", backref="elder")


class ReminderTask(Base):
    __tablename__ = "reminder_tasks"
    
    id = Column(Integer, primary_key=True, index=True)
    elder_id = Column(Integer, ForeignKey("elders.id"))
    task_type = Column(String(50))
    title = Column(String(100))
    content = Column(Text)
    schedule_time = Column(String(50))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow)


class AlertLog(Base):
    __tablename__ = "alert_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    elder_id = Column(Integer, ForeignKey("elders.id"))
    alert_type = Column(String(50))
    priority = Column(String(10))
    content = Column(Text)
    status = Column(String(20), default="pending")
    notified_at = Column(DateTime)
    created_at = Column(DateTime, default=_utcnow)


class MonitorLog(Base):
    __tablename__ = "monitor_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    elder_id = Column(Integer, ForeignKey("elders.id"))
    event_type = Column(String(50))
    content = Column(Text)
    created_at = Column(DateTime, default=_utcnow)
