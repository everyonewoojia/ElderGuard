"""P1-3 提醒任务存储：ReminderTask CRUD 与状态切换（M-DATA-02）"""
import uuid

from app.services import storage


def _elder(db):
    owner = storage.create_user(db, username=f"t_owner_{uuid.uuid4().hex[:8]}", password_hash="h")
    return storage.create_elder(db, owner_id=owner.id, name="赵爷爷")


def test_create_and_list_task(db):
    elder = _elder(db)
    task = storage.create_task(
        db, elder_id=elder.id, task_type="用药提醒", title="降压药",
        content="早晚各一次", schedule_time="08:00",
    )
    assert task.is_active is True
    tasks = storage.list_tasks_by_elder(db, elder.id)
    assert len(tasks) == 1 and tasks[0].title == "降压药"


def test_list_tasks_scoped_by_elder(db):
    """任务按 elder 隔离查询"""
    e1 = _elder(db)
    e2 = _elder(db)
    storage.create_task(db, elder_id=e1.id, task_type="用药", title="A")
    storage.create_task(db, elder_id=e2.id, task_type="用药", title="B")
    assert len(storage.list_tasks_by_elder(db, e1.id)) == 1


def test_toggle_task_active(db):
    """任务状态切换（is_active）"""
    elder = _elder(db)
    task = storage.create_task(db, elder_id=elder.id, task_type="作息", title="午休")
    assert task.is_active is True
    storage.toggle_task_active(db, task.id, False)
    assert storage.get_task(db, task.id).is_active is False
    storage.toggle_task_active(db, task.id, True)
    assert storage.get_task(db, task.id).is_active is True


def test_delete_task(db):
    elder = _elder(db)
    task = storage.create_task(db, elder_id=elder.id, task_type="作息", title="散步")
    assert storage.delete_task(db, task.id) is True
    assert storage.get_task(db, task.id) is None


def test_task_not_found_guards(db):
    """不存在的任务：查询/切换返回 None，删除返回 False"""
    assert storage.get_task(db, 9999) is None
    assert storage.toggle_task_active(db, 9999, False) is None
    assert storage.delete_task(db, 9999) is False
