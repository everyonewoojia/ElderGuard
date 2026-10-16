"""P3-4 隐私授权接口：POST /privacy/auth（鉴权 + 隔离，FR-CM-05）"""
from app.core.database import SessionLocal
from app.models.models import Elder

API = "/api/v1/privacy/auth"


def _privacy_state(elder_id):
    db = SessionLocal()
    enabled = db.query(Elder).filter(Elder.id == elder_id).first().privacy_enabled
    db.close()
    return enabled


def test_update_privacy_on(auth):
    """开启隐私授权 → 保存生效"""
    auth["register"]("A1", "13800004001")
    token = auth["login"]("A1")
    elder_id = auth["create_elder"]("A1", "甲")
    r = auth["client"].post(API, headers={"Authorization": f"Bearer {token}"}, json={
        "elder_id": elder_id, "enabled": True,
    })
    assert r.status_code == 200, r.text
    assert _privacy_state(elder_id) is True


def test_update_privacy_off(auth):
    """关闭隐私授权 → 保存生效"""
    auth["register"]("A2", "13800004002")
    token = auth["login"]("A2")
    elder_id = auth["create_elder"]("A2", "甲", privacy_enabled=True)
    r = auth["client"].post(API, headers={"Authorization": f"Bearer {token}"}, json={
        "elder_id": elder_id, "enabled": False,
    })
    assert r.status_code == 200, r.text
    assert _privacy_state(elder_id) is False


def test_update_privacy_403_other_owner(auth):
    """越权：修改他人老人授权 → 403"""
    auth["register"]("A3", "13800004003")
    auth["register"]("B3", "13800004004")
    token_b = auth["login"]("B3")
    elder_id = auth["create_elder"]("A3", "甲")
    r = auth["client"].post(API, headers={"Authorization": f"Bearer {token_b}"}, json={
        "elder_id": elder_id, "enabled": True,
    })
    assert r.status_code == 403


def test_update_privacy_requires_token(auth):
    """无 token → 401"""
    r = auth["client"].post(API, json={"elder_id": 1, "enabled": True})
    assert r.status_code == 401
