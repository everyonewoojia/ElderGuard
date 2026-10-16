"""P3-1 提醒任务接口：POST/GET /tasks、DELETE /tasks/{id}（鉴权 + 隔离）"""
from app.core.database import SessionLocal
from app.models.models import ReminderTask

API = "/api/v1/tasks"


def test_create_task_success(auth):
    """创建任务成功并入库"""
    auth["register"]("A1", "13800001001")
    token = auth["login"]("A1")
    elder_id = auth["create_elder"]("A1", "张奶奶")
    r = auth["client"].post(API, headers={"Authorization": f"Bearer {token}"}, json={
        "elder_id": elder_id, "task_type": "用药提醒", "title": "降压药",
        "content": "早晚各一次", "schedule_time": "08:00",
    })
    assert r.status_code == 200, r.text
    assert r.json()["id"] is not None
    db = SessionLocal()
    task = db.query(ReminderTask).filter(ReminderTask.id == r.json()["id"]).first()
    db.close()
    assert task is not None and task.title == "降压药"


def test_create_task_403_other_owner(auth):
    """越权：创建他人老人任务 → 403"""
    auth["register"]("A2", "13800001002")
    auth["register"]("B2", "13800001003")
    token_b = auth["login"]("B2")
    elder_id = auth["create_elder"]("A2", "甲老人")
    r = auth["client"].post(API, headers={"Authorization": f"Bearer {token_b}"}, json={
        "elder_id": elder_id, "task_type": "用药提醒", "title": "药",
    })
    assert r.status_code == 403


def test_get_tasks_only_own(auth):
    """GET /tasks 仅返回当前家属绑定老人的任务"""
    auth["register"]("A3", "13800001004")
    auth["register"]("B3", "13800001005")
    token_a = auth["login"]("A3")
    ea = auth["create_elder"]("A3", "甲")
    eb = auth["create_elder"]("B3", "乙")
    auth["create_task"](ea, "A的任务")
    auth["create_task"](eb, "B的任务")
    r = auth["client"].get(API, headers={"Authorization": f"Bearer {token_a}"})
    assert r.status_code == 200, r.text
    titles = [t["title"] for t in r.json()]
    assert titles == ["A的任务"]


def test_delete_task_success(auth):
    """删除自己老人的任务 → 200"""
    auth["register"]("A4", "13800001006")
    token = auth["login"]("A4")
    elder_id = auth["create_elder"]("A4", "甲")
    task_id = auth["create_task"](elder_id, "待删任务")
    r = auth["client"].delete(f"{API}/{task_id}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_delete_task_403_other_owner(auth):
    """越权：删除他人老人任务 → 403"""
    auth["register"]("A5", "13800001007")
    auth["register"]("B5", "13800001008")
    token_b = auth["login"]("B5")
    elder_id = auth["create_elder"]("A5", "甲")
    task_id = auth["create_task"](elder_id, "A的任务")
    r = auth["client"].delete(f"{API}/{task_id}", headers={"Authorization": f"Bearer {token_b}"})
    assert r.status_code == 403


def test_tasks_requires_token(auth):
    """无 token → 401"""
    r = auth["client"].get(API)
    assert r.status_code == 401
