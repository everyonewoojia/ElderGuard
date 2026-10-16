"""P3-2 告警记录接口：GET /alerts、GET /alerts/{id}（鉴权 + 隔离，FR-FL-03）"""
API = "/api/v1/alerts"


def test_get_alerts_only_own(auth):
    """GET /alerts 仅返回当前家属绑定老人的告警"""
    auth["register"]("A1", "13800002001")
    auth["register"]("B1", "13800002002")
    token_a = auth["login"]("A1")
    ea = auth["create_elder"]("A1", "甲")
    eb = auth["create_elder"]("B1", "乙")
    auth["create_alert"](ea, "fall", "P0", "A告警")
    auth["create_alert"](eb, "fall", "P0", "B告警")
    r = auth["client"].get(API, headers={"Authorization": f"Bearer {token_a}"})
    assert r.status_code == 200, r.text
    contents = [a["content"] for a in r.json()]
    assert contents == ["A告警"]


def test_get_alert_detail(auth):
    """GET /alerts/{id} 返回单条告警详情"""
    auth["register"]("A2", "13800002003")
    token = auth["login"]("A2")
    elder_id = auth["create_elder"]("A2", "甲")
    alert_id = auth["create_alert"](elder_id, "fall", "P0", "跌倒告警")
    r = auth["client"].get(f"{API}/{alert_id}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["alert_type"] == "fall" and r.json()["content"] == "跌倒告警"


def test_get_alert_detail_403_other_owner(auth):
    """越权：查看他人老人告警详情 → 403"""
    auth["register"]("A3", "13800002004")
    auth["register"]("B3", "13800002005")
    token_b = auth["login"]("B3")
    elder_id = auth["create_elder"]("A3", "甲")
    alert_id = auth["create_alert"](elder_id, "fall", "P0", "A告警")
    r = auth["client"].get(f"{API}/{alert_id}", headers={"Authorization": f"Bearer {token_b}"})
    assert r.status_code == 403


def test_get_alerts_requires_token(auth):
    """无 token → 401"""
    r = auth["client"].get(API)
    assert r.status_code == 401
