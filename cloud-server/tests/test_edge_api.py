"""P6 云端边缘接入端点测试：老人端上报告警写日志、拉取任务/设置/授权（FR-SC-05/FR-FL-06/08/FR-CM-05）"""
import pytest
from uuid import uuid4

from app.services import storage


def _setup(auth, device_credentials):
    auth["register"]("fam_p6", "13800000001")
    elder_id = auth["create_elder"]("fam_p6", "张大爷")
    token = device_credentials(elder_id)
    return auth, elder_id, {"Authorization": f"Bearer {token}"}


def test_receive_alert_writes_log(auth, device_credentials):
    """老人端上报告警 → 写入告警日志（FR-SC-05）"""
    a, elder_id, headers = _setup(auth, device_credentials)
    r = a["client"].post("/api/v1/edge/alerts", json={
        "event_id": str(uuid4()), "elder_id": elder_id, "alert_type": "fall", "priority": "P0", "content": "疑似跌倒",
    }, headers=headers)
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


def test_receive_alert_unknown_elder_403(auth, device_credentials):
    """设备访问非绑定 ID → 403，不暴露其他老人是否存在。"""
    a, _, headers = _setup(auth, device_credentials)
    r = a["client"].post("/api/v1/edge/alerts", json={
        "event_id": str(uuid4()), "elder_id": 99999, "alert_type": "fall",
    }, headers=headers)
    assert r.status_code == 403


def test_edge_tasks_returns_active(auth, device_credentials):
    """老人端拉取任务：仅返回激活任务（FR-FL-06）"""
    a, elder_id, headers = _setup(auth, device_credentials)
    a["create_task"](elder_id, title="降压药")
    a["create_task"](elder_id, title="作息")
    # 停用一个任务
    from app.core.database import SessionLocal
    s = SessionLocal()
    tasks = storage.list_tasks_by_elder(s, elder_id)
    storage.toggle_task_active(s, tasks[0].id, False)
    s.close()

    r = a["client"].get("/api/v1/edge/tasks", params={"elder_id": elder_id}, headers=headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data) == 1
    assert data[0]["title"] == "作息"


def test_edge_settings(auth, device_credentials):
    """老人端拉取设置（FR-FL-08）"""
    a, elder_id, headers = _setup(auth, device_credentials)
    r = a["client"].get("/api/v1/edge/settings", params={"elder_id": elder_id}, headers=headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["elder_id"] == elder_id
    assert data["privacy_enabled"] is False


def test_edge_privacy_reflects_change(auth, device_credentials):
    """授权状态可被家属端修改并被边缘拉取到（FR-CM-05 联动）"""
    a, elder_id, headers = _setup(auth, device_credentials)
    # 家属端开启授权
    token = a["login"]("fam_p6")
    r = a["client"].post(
        "/api/v1/privacy/auth",
        json={"elder_id": elder_id, "enabled": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    # 老人端拉取 → enabled=True
    r = a["client"].get("/api/v1/edge/privacy", params={"elder_id": elder_id}, headers=headers)
    assert r.json()["data"]["enabled"] is True
