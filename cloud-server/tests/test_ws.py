"""P8-1 WebSocket 实时推送 / P8-5 取消不重复推送测试（M-ACC-03 / FR-SC-08 / KK03）"""
import pytest
from uuid import uuid4

from app.core.database import SessionLocal
from app.services import storage

API_WS = "/api/v1/ws/alerts"
EDGE = "/api/v1/edge/alerts"


def _setup(auth, device_credentials):
    auth["register"]("A1", "13800005001")
    token = auth["login"]("A1")
    elder_id = auth["create_elder"]("A1", "张奶奶")
    device_token = device_credentials(elder_id)
    return auth["client"], token, elder_id, {"Authorization": f"Bearer {device_token}"}


def test_ws_rejects_missing_token(auth, device_credentials):
    """无 token 连接 → 拒绝（关闭）"""
    client, _, _, _ = _setup(auth, device_credentials)
    with pytest.raises(Exception):
        with client.websocket_connect(API_WS):
            pass


def test_ws_replays_pending_on_connect(auth, device_credentials):
    """连接时补推未确认（pending）告警"""
    client, token, elder_id, headers = _setup(auth, device_credentials)
    response = client.post(EDGE, json={"event_id": str(uuid4()), "elder_id": elder_id, "alert_type": "fall", "priority": "P0"}, headers=headers)
    assert response.status_code == 200
    with client.websocket_connect(f"{API_WS}?token={token}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "alert"
        assert msg["data"]["alert_type"] == "fall"
        assert msg["data"]["status"] == "pending"


def test_ws_pushes_new_alert_live(auth, device_credentials):
    """连接期间新告警实时推送"""
    client, token, elder_id, headers = _setup(auth, device_credentials)
    with client.websocket_connect(f"{API_WS}?token={token}") as ws:
        response = client.post(EDGE, json={"event_id": str(uuid4()), "elder_id": elder_id, "alert_type": "help", "priority": "P0"}, headers=headers)
        assert response.status_code == 200
        msg = ws.receive_json()
        assert msg["type"] == "alert"
        assert msg["data"]["alert_type"] == "help"


def test_canceled_alert_not_replayed(auth, device_credentials):
    """P8-5：取消后不再进入待推送（不重复推送，KK03）；日志保留"""
    client, token, elder_id, headers = _setup(auth, device_credentials)
    r = client.post(EDGE, json={"event_id": str(uuid4()), "elder_id": elder_id, "alert_type": "fall", "priority": "P0"}, headers=headers)
    assert r.status_code == 200
    alert_id = r.json()["data"]["alert_id"]
    # 取消（状态 → cancelled，日志保留）
    resp = client.post(
        f"/api/v1/alerts/{alert_id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    # 待推送列表不再含该告警
    db = SessionLocal()
    try:
        elder = storage.get_elder(db, elder_id)
        pending = storage.list_pending_alerts_by_owner(db, elder.owner_id)
        assert all(a.id != alert_id for a in pending)
        # 日志仍保留（可按 id 查回，状态为 cancelled）
        saved = storage.get_alert(db, alert_id)
        assert saved is not None and saved.status == "cancelled"
    finally:
        db.close()
