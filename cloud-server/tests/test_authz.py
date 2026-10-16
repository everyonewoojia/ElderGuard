"""P2-2/3/4 鉴权依赖、行级隔离（越权 403）、统一错误码"""
from app.core.database import SessionLocal
from app.models.models import User, Elder
from app.services.auth import create_access_token


def _register(client, username, phone):
    r = client.post("/api/v1/auth/register", json={"username": username, "password": "pw123456", "phone": phone})
    assert r.status_code == 200


def _login(client, username):
    r = client.post("/api/v1/auth/login", data={"username": username, "password": "pw123456"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _create_elder_for(username, name):
    """直接通过会话为指定用户名创建老人档案（测试辅助）"""
    db = SessionLocal()
    owner = db.query(User).filter(User.username == username).first()
    elder = Elder(owner_id=owner.id, name=name)
    db.add(elder)
    db.commit()
    db.refresh(elder)
    db.close()
    return elder.id


def test_get_elder_requires_token(client_clean):
    """未带 token 访问受保护接口 → 401"""
    r = client_clean.get("/api/v1/elder/1")
    assert r.status_code == 401


def test_get_elder_invalid_token(client_clean):
    """非法 token → 401"""
    r = client_clean.get("/api/v1/elder/1", headers={"Authorization": "Bearer bad-token"})
    assert r.status_code == 401


def test_get_elder_own_data(client_clean):
    """家属可查询自己绑定的老人 → 200"""
    _register(client_clean, "own", "13800000021")
    token = _login(client_clean, "own")
    elder_id = _create_elder_for("own", "张奶奶")
    r = client_clean.get(f"/api/v1/elder/{elder_id}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["name"] == "张奶奶"


def test_get_elder_403_unauthorized(client_clean):
    """越权访问他人老人数据 → 403（越权 0 次，AQ01）"""
    _register(client_clean, "ownerA", "13800000022")
    _register(client_clean, "ownerB", "13800000023")
    token_b = _login(client_clean, "ownerB")
    elder_id = _create_elder_for("ownerA", "甲老人")
    r = client_clean.get(f"/api/v1/elder/{elder_id}", headers={"Authorization": f"Bearer {token_b}"})
    assert r.status_code == 403


def test_get_elder_404_not_found(client_clean):
    """查询不存在的老人 → 404"""
    _register(client_clean, "own2", "13800000024")
    token = _login(client_clean, "own2")
    r = client_clean.get("/api/v1/elder/9999", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404


def test_error_response_shape(client_clean):
    """统一错误码响应结构 {code, message, data}"""
    r = client_clean.get("/api/v1/elder/1")
    assert r.status_code == 401
    body = r.json()
    assert set(body.keys()) == {"code", "message", "data"}
    assert body["code"] == 401
    assert body["data"] is None


def test_get_elder_non_family_forbidden(client_clean):
    """非家属角色访问受保护接口 → 403"""
    client_clean.post("/api/v1/auth/register", json={
        "username": "admin1", "password": "pw123456", "phone": "13800000031", "role": "admin",
    })
    token = client_clean.post("/api/v1/auth/login", data={"username": "admin1", "password": "pw123456"}).json()["access_token"]
    r = client_clean.get("/api/v1/elder/1", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_me_token_for_unknown_user(client_clean):
    """token 对应的用户不存在 → 401"""
    token = create_access_token({"sub": "ghost_user", "role": "family"})
    r = client_clean.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
