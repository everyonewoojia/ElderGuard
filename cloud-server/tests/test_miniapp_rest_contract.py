"""Feed real protected REST responses into production miniapp page logic via Node."""
import json
import os
from pathlib import Path
import subprocess


def test_real_routes_reach_miniapp_pages(auth):
    """Verify bare lists, empty lists, objects and ownership errors across runtimes."""
    client = auth["client"]
    auth["register"]("contract-owner", "13800001001")
    auth["register"]("contract-other", "13800001002")
    headers = {"Authorization": f"Bearer {auth['login']('contract-owner')}"}

    def capture(method, url, expected=200, **kwargs):
        """Capture only synthetic response status/body, never credentials."""
        response = client.request(method, f"/api/v1{url}", headers=headers, **kwargs)
        assert response.status_code == expected
        return {"status": response.status_code, "body": response.json()}

    urls = ["/elder", "/tasks", "/alerts"]
    empty = {url: capture("GET", url) for url in urls}
    assert all(response["body"] == [] for response in empty.values())
    elder_id = auth["create_elder"]("contract-owner", "合成老人", True)
    sibling_id = auth["create_elder"]("contract-owner", "合成老人二")
    other_id = auth["create_elder"]("contract-other", "隔离老人")
    task_id = auth["create_task"](elder_id, task_type="medication")
    sibling_task = auth["create_task"](sibling_id, task_type="rest")
    foreign_task = auth["create_task"](other_id)
    alert_ids = [auth["create_alert"](elder_id) for _ in range(4)]
    foreign_alert = auth["create_alert"](other_id)
    populated = {url: capture("GET", url) for url in urls}
    assert all(isinstance(response["body"], list) for response in populated.values())
    assert {row["id"] for row in populated["/elder"]["body"]} == {elder_id, sibling_id}
    assert {row["id"] for row in populated["/tasks"]["body"]} == {task_id, sibling_task}
    assert {row["id"] for row in populated["/alerts"]["body"]} == set(alert_ids)
    forbidden = capture("GET", f"/elder/{other_id}", 403)
    capture("DELETE", f"/tasks/{foreign_task}", 403)
    capture("GET", f"/alerts/{foreign_alert}", 403)
    capture("POST", f"/alerts/{foreign_alert}/cancel", 403)
    unauthorized = client.get("/api/v1/elder")
    assert unauthorized.status_code == 401
    objects = {
        f"/elder/{elder_id}": capture("GET", f"/elder/{elder_id}"),
        "/tasks": capture("POST", "/tasks", json={
            "elder_id": elder_id, "task_type": "rest", "title": "合成新增提醒",
        }),
        "/privacy/auth": capture("POST", "/privacy/auth", json={
            "elder_id": elder_id, "enabled": True,
        }),
        f"/alerts/{alert_ids[0]}/cancel": capture("POST", f"/alerts/{alert_ids[0]}/cancel"),
    }
    assert all(isinstance(response["body"], dict) and "data" not in response["body"]
               for response in objects.values())
    fixture = {
        "populated": populated, "empty": empty, "forbidden": forbidden,
        "unauthorized": {"status": unauthorized.status_code, "body": unauthorized.json()},
        "objects": objects,
    }
    env = {**os.environ, "ELDERGUARD_REST_CONTRACT": json.dumps(fixture, ensure_ascii=True)}
    result = subprocess.run(
        ["node", "--test", "tests/rest-contract.test.js"],
        cwd=Path(__file__).resolve().parents[2] / "family-miniapp",
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    print(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr
