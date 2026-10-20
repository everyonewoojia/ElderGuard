from sqlalchemy import Boolean, Column, Integer, String, DateTime, ForeignKey, Text, UniqueConstraint, Index, text
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


class Device(Base):
    """Locally enrolled edge identity; authorization follows the live binding."""

    __tablename__ = "edge_devices"

    device_id = Column(String(64), primary_key=True)
    elder_id = Column(Integer, ForeignKey("elders.id"), nullable=False, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    credential_version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)


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


class EdgeAlertReceipt(Base):
    """Device-scoped idempotency receipt, committed with its business alert."""

    __tablename__ = "edge_alert_receipts"
    __table_args__ = (UniqueConstraint("device_id", "event_id", name="uq_edge_alert_event"),
                      Index("ix_edge_receipt_delivery", "notification_state", "notification_next_at"))

    id = Column(Integer, primary_key=True)
    device_id = Column(String(64), ForeignKey("edge_devices.device_id"), nullable=False)
    event_id = Column(String(36), nullable=False)
    elder_id = Column(Integer, ForeignKey("elders.id"), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    alert_id = Column(Integer, ForeignKey("alert_logs.id"), nullable=False, unique=True)
    received_at = Column(DateTime, nullable=False, default=_utcnow)
    # ORM inserts schedule new events. Additive SQLite migration gives old
    # receipts an unknown outcome, never inventing historical pending work.
    notification_state = Column(String(32), nullable=False, default="pending", server_default=text("'unknown'"))
    notification_attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    notification_updated_at = Column(DateTime, default=_utcnow)
    notification_next_at = Column(DateTime)
    notification_claim_token = Column(String(64))
    notification_lease_until = Column(DateTime)
    notification_last_error = Column(String(64))
    notification_last_attempt_at = Column(DateTime)
    notification_last_result = Column(String(32))
    notification_send_count = Column(Integer)
    notification_revision = Column(Integer, nullable=False, default=0, server_default=text("0"))


class NotificationRecovery(Base):
    """Append-only local decisions; operation UUID resolves uncertain commits."""

    __tablename__ = "notification_recoveries"
    operation_id = Column(String(36), primary_key=True)
    receipt_id = Column(Integer, ForeignKey("edge_alert_receipts.id"), nullable=False, index=True)
    device_id = Column(String(64), nullable=False)
    elder_id = Column(Integer, nullable=False)
    operator = Column(String(64), nullable=False)
    reason = Column(String(64), nullable=False)
    action = Column(String(16), nullable=False)
    result = Column(String(32), nullable=False)
    expected_revision = Column(Integer, nullable=False)
    attempts = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)


class SchemaMigration(Base):
    """Explicit additive migration audit; never written by application startup."""

    __tablename__ = "schema_migrations"
    version = Column(String(100), primary_key=True)
    applied_at = Column(DateTime, nullable=False, default=_utcnow)


class MonitorLog(Base):
    __tablename__ = "monitor_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    elder_id = Column(Integer, ForeignKey("elders.id"))
    event_type = Column(String(50))
    content = Column(Text)
    created_at = Column(DateTime, default=_utcnow)
