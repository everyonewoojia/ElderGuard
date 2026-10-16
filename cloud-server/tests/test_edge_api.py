"""P6 云端边缘接入端点测试：老人端上报告警写日志、拉取任务/设置/授权（FR-SC-05/FR-FL-06/08/FR-CM-05）"""
import pytest

from app.services import storage


def _setup(auth):
    auth["register"]("fam_p6", "13800000001")
    elder_id = auth["create_elder"]("fam_p6", "张大爷")
    return auth, elder_id


def test_receive_alert_writes_log(auth):
    """老人端上报告警 → 写入告警日志（FR-SC-05）"""
    a, elder_id = _setup(auth)
    r = a["client"].post("/api/v1/edge/alerts", json={
        "elder_id": elder_id, "alert_type": "fall", "priority": "P0", "content": "疑似跌倒",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 200
    assert body["data"]["status"] == "pending"

    # 用独立 session 校验写入
    from app.core.database import SessionLocal
    s = SessionLocal()
    try:
        alerts = storage.list_alerts_by_elder(s, elder_id)
        assert len(alerts) == 1
        assert alerts[0].alert_type == "fall"
    finally:
        s.close()


def test_receive_alert_unknown_elder_404(auth):
    """上报告警到不存在的老人 → 404"""
    a, _ = _setup(auth)
    r = a["client"].post("/api/v1/edge/alerts", json={
        "elder_id": 99999, "alert_type": "fall",
    })
    assert r.status_code == 404


def test_edge_tasks_returns_active(auth):
    """老人端拉取任务：仅返回激活任务（FR-FL-06）"""
    a, elder_id = _setup(auth)
    a["create_task"](elder_id, title="降压药")
    a["create_task"](elder_id, title="作息")
    # 停用一个任务
    from app.core.database import SessionLocal
    s = SessionLocal()
    tasks = storage.list_tasks_by_elder(s, elder_id)
    storage.toggle_task_active(s, tasks[0].id, False)
    s.close()

    r = a["client"].get("/api/v1/edge/tasks", params={"elder_id": elder_id})
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data) == 1
    assert data[0]["title"] == "作息"


def test_edge_settings(auth):
    """老人端拉取设置（FR-FL-08）"""
    a, elder_id = _setup(auth)
    r = a["client"].get("/api/v1/edge/settings", params={"elder_id": elder_id})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["elder_id"] == elder_id
    assert data["privacy_enabled"] is False


def test_edge_privacy_reflects_change(auth):
    """授权状态可被家属端修改并被边缘拉取到（FR-CM-05 联动）"""
    a, elder_id = _setup(auth)
    # 家属端开启授权
    token = a["login"]("fam_p6")
    r = a["client"].post(
        "/api/v1/privacy/auth",
        json={"elder_id": elder_id, "enabled": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    # 老人端拉取 → enabled=True
    r = a["client"].get("/api/v1/edge/privacy", params={"elder_id": elder_id})
    assert r.json()["data"]["enabled"] is True
