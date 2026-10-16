"""P2-1 注册 / 登录 / JWT 鉴权（M-APP-01）"""
from datetime import timedelta

from app.core.database import SessionLocal
from app.models.models import User
from app.services.auth import create_access_token, verify_password


def _register(client, username, password, phone):
    return client.post("/api/v1/auth/register", json={
        "username": username, "password": password, "phone": phone,
    })


def test_register_success(client_clean):
    """注册成功；密码不落明文"""
    r = _register(client_clean, "u1", "secret123", "13800000001")
    assert r.status_code == 200
    assert r.json()["username"] == "u1"
    db = SessionLocal()
    user = db.query(User).filter(User.username == "u1").first()
    db.close()
    assert user is not None
    assert user.password_hash != "secret123"          # 不落明文
    assert verify_password("secret123", user.password_hash) is True


def test_register_duplicate_username(client_clean):
    """重复用户名注册 → 400"""
    _register(client_clean, "dup", "x123456", "13800000002")
    r = _register(client_clean, "dup", "y123456", "13800000003")
    assert r.status_code == 400


def test_register_duplicate_phone(client_clean):
    """重复手机号注册 → 400"""
    _register(client_clean, "p1", "x123456", "13800000009")
    r = _register(client_clean, "p2", "y123456", "13800000009")
    assert r.status_code == 400


def test_login_success(client_clean):
    """登录成功返回 access_token"""
    _register(client_clean, "log1", "pw123456", "13800000004")
    r = client_clean.post("/api/v1/auth/login", data={"username": "log1", "password": "pw123456"})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


def test_login_wrong_password(client_clean):
    """密码错误 → 401"""
    _register(client_clean, "log2", "pw123456", "13800000005")
    r = client_clean.post("/api/v1/auth/login", data={"username": "log2", "password": "wrong"})
    assert r.status_code == 401


def test_login_unknown_user(client_clean):
    """用户不存在 → 401"""
    r = client_clean.post("/api/v1/auth/login", data={"username": "nobody", "password": "x"})
    assert r.status_code == 401


def test_me_requires_token(client_clean):
    """无 token 访问受保护接口 → 401"""
    r = client_clean.get("/api/v1/auth/me")
    assert r.status_code == 401


def test_me_invalid_token(client_clean):
    """非法 token → 401"""
    r = client_clean.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-token"})
    assert r.status_code == 401


def test_me_expired_token(client_clean):
    """过期 token → 401"""
    _register(client_clean, "tok1", "pw123456", "13800000006")
    token = create_access_token({"sub": "tok1", "role": "family"}, expires_delta=timedelta(minutes=-5))
    r = client_clean.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_me_valid(client_clean):
    """有效 token 返回当前用户信息"""
    _register(client_clean, "tok2", "pw123456", "13800000007")
    token = client_clean.post("/api/v1/auth/login", data={"username": "tok2", "password": "pw123456"}).json()["access_token"]
    me = client_clean.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == "tok2"
