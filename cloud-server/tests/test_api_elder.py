"""P3-5 老人信息接口：GET /elder/{id}（返回绑定 elder 信息，越权 0 次）"""
API = "/api/v1/elder"


def test_get_elder_info(auth):
    """返回绑定老人的完整信息"""
    auth["register"]("A1", "13800005001")
    token = auth["login"]("A1")
    elder_id = auth["create_elder"]("A1", "张奶奶", privacy_enabled=True)
    r = auth["client"].get(f"{API}/{elder_id}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "张奶奶"
    assert body["privacy_enabled"] is True
    assert "id" in body and "age" in body


def test_get_elder_requires_token(auth):
    """无 token → 401"""
    r = auth["client"].get(f"{API}/1")
    assert r.status_code == 401
