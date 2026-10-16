"""数据持久层服务（M-DATA-01/02/03）。

提供五模型（User/Elder/ReminderTask/AlertLog/MonitorLog）的 CRUD 与查询服务，
所有查询均以「按 elder 归属」为边界，为 P2 的越权隔离与 P3 的接口层预留能力。
"""
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.models import User, Elder, ReminderTask, AlertLog, MonitorLog

# ---------------------------------------------------------------------------
# 用户档案存储（M-DATA-01）
# ---------------------------------------------------------------------------


def create_user(
    db: Session,
    username: str,
    password_hash: str,
    role: str = "family",
    phone: Optional[str] = None,
) -> User:
    """创建家属用户。password_hash 为加密后的哈希，绝不存明文。"""
    user = User(username=username, password_hash=password_hash, role=role, phone=phone)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def get_user_by_username(db: Session, username: str) -> Optional[User]:
    """按用户名查询用户（登录用）。"""
    return db.query(User).filter(User.username == username).first()


def get_user_by_id(db: Session, user_id: int) -> Optional[User]:
    """按主键查询用户。"""
    return db.query(User).filter(User.id == user_id).first()


def create_elder(
    db: Session,
    owner_id: int,
    name: str,
    age: Optional[int] = None,
    phone: Optional[str] = None,
    address: Optional[str] = None,
    privacy_enabled: bool = False,
) -> Elder:
    """为指定家属（owner_id）创建老人档案。"""
    elder = Elder(
        owner_id=owner_id,
        name=name,
        age=age,
        phone=phone,
        address=address,
        privacy_enabled=privacy_enabled,
    )
    db.add(elder)
    db.commit()
    db.refresh(elder)
    return elder


def get_elder(db: Session, elder_id: int) -> Optional[Elder]:
    """按主键查询老人。"""
    return db.query(Elder).filter(Elder.id == elder_id).first()


def list_elders_by_owner(db: Session, owner_id: int) -> List[Elder]:
    """仅返回指定家属绑定的老人（越权隔离前置，供 P2 复用）。"""
    return db.query(Elder).filter(Elder.owner_id == owner_id).all()


def update_elder_privacy(db: Session, elder_id: int, enabled: bool) -> Optional[Elder]:
    """更新老人隐私授权开关（FR-CM-05 数据层）。"""
    elder = get_elder(db, elder_id)
    if elder is None:
        return None
    elder.privacy_enabled = enabled
    db.commit()
    db.refresh(elder)
    return elder


# ---------------------------------------------------------------------------
# 提醒任务存储（M-DATA-02）
# ---------------------------------------------------------------------------


def create_task(
    db: Session,
    elder_id: int,
    task_type: str,
    title: str,
    content: Optional[str] = None,
    schedule_time: Optional[str] = None,
) -> ReminderTask:
    """为老人创建提醒任务，默认启用（is_active=True）。"""
    task = ReminderTask(
        elder_id=elder_id,
        task_type=task_type,
        title=title,
        content=content,
        schedule_time=schedule_time,
        is_active=True,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def get_task(db: Session, task_id: int) -> Optional[ReminderTask]:
    """按主键查询任务。"""
    return db.query(ReminderTask).filter(ReminderTask.id == task_id).first()


def list_tasks_by_elder(db: Session, elder_id: int) -> List[ReminderTask]:
    """按 elder 归属查询任务（越权隔离前置）。"""
    return db.query(ReminderTask).filter(ReminderTask.elder_id == elder_id).all()


def list_tasks_by_owner(db: Session, owner_id: int) -> List[ReminderTask]:
    """按家属 owner 聚合其全部老人的任务（P3 GET /tasks 越权隔离）。"""
    elder_ids = [e.id for e in list_elders_by_owner(db, owner_id)]
    if not elder_ids:
        return []
    return (
        db.query(ReminderTask)
        .filter(ReminderTask.elder_id.in_(elder_ids))
        .order_by(ReminderTask.id)
        .all()
    )


def toggle_task_active(db: Session, task_id: int, is_active: bool) -> Optional[ReminderTask]:
    """切换任务启用/停用状态（状态切换）。"""
    task = get_task(db, task_id)
    if task is None:
        return None
    task.is_active = is_active
    task.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)
    return task


def delete_task(db: Session, task_id: int) -> bool:
    """删除任务，返回是否真的删除了记录。"""
    task = get_task(db, task_id)
    if task is None:
        return False
    db.delete(task)
    db.commit()
    return True


# ---------------------------------------------------------------------------
# 告警日志存储（M-DATA-03）
# ---------------------------------------------------------------------------


def create_alert(
    db: Session,
    elder_id: int,
    alert_type: str,
    priority: str,
    content: Optional[str] = None,
    status: str = "pending",
) -> AlertLog:
    """写入告警日志，初始状态为 pending。"""
    alert = AlertLog(
        elder_id=elder_id,
        alert_type=alert_type,
        priority=priority,
        content=content,
        status=status,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return alert


def get_alert(db: Session, alert_id: int) -> Optional[AlertLog]:
    """按主键查询告警。"""
    return db.query(AlertLog).filter(AlertLog.id == alert_id).first()


def list_alerts_by_elder(db: Session, elder_id: int) -> List[AlertLog]:
    """按 elder 归属查询告警（越权隔离前置）。"""
    return db.query(AlertLog).filter(AlertLog.elder_id == elder_id).all()


def list_alerts_by_owner(db: Session, owner_id: int) -> List[AlertLog]:
    """按家属 owner 聚合其全部老人的告警（P3 GET /alerts 越权隔离）。"""
    elder_ids = [e.id for e in list_elders_by_owner(db, owner_id)]
    if not elder_ids:
        return []
    return (
        db.query(AlertLog)
        .filter(AlertLog.elder_id.in_(elder_ids))
        .order_by(AlertLog.id)
        .all()
    )


def update_alert_status(db: Session, alert_id: int, status: str) -> Optional[AlertLog]:
    """更新告警状态；切到 notified 时记录通知时间。"""
    alert = get_alert(db, alert_id)
    if alert is None:
        return None
    alert.status = status
    if status == "notified":
        alert.notified_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(alert)
    return alert


# ---------------------------------------------------------------------------
# 监护日志存储（M-DATA-03）
# ---------------------------------------------------------------------------


def create_log(
    db: Session,
    elder_id: int,
    event_type: str,
    content: Optional[str] = None,
) -> MonitorLog:
    """写入一条监护日志（对话、久坐、活动等事件）。"""
    log = MonitorLog(elder_id=elder_id, event_type=event_type, content=content)
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def list_logs_by_elder(db: Session, elder_id: int) -> List[MonitorLog]:
    """按 elder 归属查询监护日志（越权隔离前置）。"""
    return db.query(MonitorLog).filter(MonitorLog.elder_id == elder_id).all()
