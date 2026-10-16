"""P3-3 监护日志接口：GET /elder/{id}/logs（鉴权 + 隔离，FR-FL-02/FR-SC-09）"""
API = "/api/v1/elder"


def test_get_elder_logs(auth):
    """查询绑定老人的监护日志"""
    auth["register"]("A1", "13800003001")
    token = auth["login"]("A1")
    elder_id = auth["create_elder"]("A1", "张奶奶")
    auth["create_log"](elder_id, "voice", "对话")
    auth["create_log"](elder_id, "sedentary", "久坐")
    r = auth["client"].get(f"{API}/{elder_id}/logs", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    events = [log["event_type"] for log in r.json()]
    assert set(events) == {"voice", "sedentary"}


def test_get_elder_logs_403_other_owner(auth):
    """越权：查看他人老人日志 → 403"""
    auth["register"]("A2", "13800003002")
    auth["register"]("B2", "13800003003")
    token_b = auth["login"]("B2")
    elder_id = auth["create_elder"]("A2", "甲")
    auth["create_log"](elder_id, "voice", "对话")
    r = auth["client"].get(f"{API}/{elder_id}/logs", headers={"Authorization": f"Bearer {token_b}"})
    assert r.status_code == 403


def test_get_elder_logs_requires_token(auth):
    """无 token → 401"""
    r = auth["client"].get(f"{API}/1/logs")
    assert r.status_code == 401
