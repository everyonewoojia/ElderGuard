"""P6 端云集成测试：老人端 EdgeClient/EdgeReporter 通过 httpx ASGITransport 走真实云端 app，
验证告警上报写日志、任务同步、授权联动、告警送达 ≤10s、断网重试（KK01/FR-SC-05/FR-FL-06/FR-CM-05）。

说明：本测试位于云端 tests/，但复用老人端 src/integration 的 EdgeClient/EdgeReporter 与
src.core.privacy / src.ai.asr（pythonpath 已指向 ../elder-agent/src），构成端云链路。
"""
import asyncio
import time
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport

# 端云集成：老人端 src 已由 tests/conftest.py 加入 sys.path（跨子工程复用 EdgeClient/EdgeReporter）
from src.integration.edge import EdgeClient  # noqa: E402

from app.core.database import SessionLocal
from app.services import storage


def _register_elder(auth, username="fam_eg", name="李奶奶"):
    auth["register"](username, "13800000002")
    elder_id = auth["create_elder"](username, name)
    return elder_id


@pytest.fixture()
def edge_client(auth, device_credentials):
    """After elder creation, enroll its device and build an authenticated client."""
    from app.main import app

    clients = []

    def create(elder_id):
        """Use protected test enrollment instead of fabricating a device JWT."""
        token = device_credentials(elder_id)
        transport = ASGITransport(app=app)
        http = httpx.AsyncClient(transport=transport, base_url="http://test")
        clients.append(http)
        return EdgeClient(base_url="http://test", client=http, device_token=token)

    try:
        yield create, auth
    finally:
        for http in clients:
            asyncio.run(http.aclose())


async def test_report_alert_end_to_end(edge_client):
    """端云链路：老人端上报告警 → 云端写入告警日志（FR-SC-05）"""
    create_client, auth = edge_client
    elder_id = _register_elder(auth)
    client = create_client(elder_id)
    data = await client.report_alert(elder_id, "fall", "P0", "疑似跌倒", event_id=str(uuid4()))
    assert data is not None
    assert "alert_id" in data

    s = SessionLocal()
    try:
        alerts = storage.list_alerts_by_elder(s, elder_id)
        assert len(alerts) == 1
        assert alerts[0].alert_type == "fall"
    finally:
        s.close()


async def test_report_alert_within_10s(edge_client):
    """高危告警送达 ≤10s（KK01）"""
    create_client, auth = edge_client
    elder_id = _register_elder(auth)
    client = create_client(elder_id)
    start = time.monotonic()
    data = await client.report_alert(elder_id, "fall", "P0", "高危", event_id=str(uuid4()))
    elapsed = time.monotonic() - start
    assert data is not None
    assert elapsed <= 10.0, f"告警送达耗时 {elapsed:.2f}s > 10s"


async def test_fetch_tasks_sync_to_edge(edge_client):
    """家属端配置任务 → 云端 → 老人端拉取并执行（FR-FL-06）"""
    create_client, auth = edge_client
    elder_id = _register_elder(auth)
    client = create_client(elder_id)
    auth["create_task"](elder_id, title="降压药")
    tasks = await client.fetch_tasks(elder_id)
    assert len(tasks) == 1
    assert tasks[0]["title"] == "降压药"
    # 老人端把任务转为提醒事件执行 → ReminderPlugin 记录
    from src.core.event_bus import EventBus
    from src.plugins.reminder import ReminderPlugin

    plugin = ReminderPlugin(EventBus())
    await plugin.handle_event({
        "payload": {"task_type": tasks[0]["task_type"], "title": tasks[0]["title"]}
    })
    statuses = plugin.get_task_status()
    assert len(statuses) == 1
    assert list(statuses.values())[0]["title"] == "降压药"


async def test_privacy_linkage_cloud_to_edge(edge_client):
    """关闭授权 → 老人端拉取授权 → 本地 PrivacyGuard 关闭 → 停止采集（FR-CM-05/AQ02）"""
    create_client, auth = edge_client
    elder_id = _register_elder(auth)
    client = create_client(elder_id)

    from src.ai.asr import ASRService
    from src.core.privacy import PrivacyGuard

    guard = PrivacyGuard(enabled=True)
    asr = ASRService(privacy=guard)

    # 云端关闭授权（家属端操作）
    token = auth["login"]("fam_eg")
    r = auth["client"].post(
        "/api/v1/privacy/auth",
        json={"elder_id": elder_id, "enabled": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200

    # 老人端拉取授权并联动到本地守卫
    enabled = await client.fetch_privacy(elder_id)
    assert enabled is False
    guard.set_enabled(enabled)

    # 关闭授权后 ASR 采集 0 次（AQ02/CON-B-04）
    audio = await asr.listen()
    assert audio is None
    assert guard.capture_count == 0


async def test_reporter_retry_limits(edge_client, device_credentials):
    """断网重试：失败入队，重试耗尽后记录失败，重试次数 ≤3（KK01）"""
    create_client, auth = edge_client
    elder_id = _register_elder(auth)
    client = create_client(elder_id)

    # 构造一个始终失败的 client（覆盖 report_alert 返回 None）
    from src.integration.edge import EdgeClient, EdgeReporter

    class AlwaysFail(EdgeClient):
        async def report_alert(self, *args, **kwargs):
            return None

    reporter = EdgeReporter(AlwaysFail(base_url="http://x", device_token=device_credentials(elder_id)))
    ok = await reporter.report(elder_id, "fall", "P0", "断网", event_id=str(uuid4()))
    assert ok is False
    assert len(reporter.pending) == 1
    assert reporter.pending[0]["retries_left"] == EdgeReporter.MAX_RETRIES - 1

    # 逐次 flush 重试，直到耗尽（最多重试 3 次）
    for _ in range(EdgeReporter.MAX_RETRIES + 1):
        await reporter.flush()
    assert reporter.pending == []
    assert len(reporter.failures) == 1
    assert reporter.failures[0]["status"] == "failed"


async def test_cli_credential_file_reaches_edge_client(
    auth, device_credentials, test_database, monkeypatch,
):
    """Real CLI file writer → configured file reader → authenticated REST."""
    from app.main import app

    elder_id = _register_elder(auth)
    token_file = test_database.root / "edge-device.credential"
    device_credentials(elder_id, token_file=token_file)
    monkeypatch.setenv("EDGE_DEVICE_TOKEN_FILE", str(token_file))
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        client = EdgeClient(base_url="http://test", client=http)
        assert await client.fetch_privacy(elder_id) is False
        data = await client.report_alert(elder_id, "help", "P0", event_id=str(uuid4()))
        assert data is not None
        assert "alert_id" in data
    with test_database.session() as session:
        alerts = storage.list_alerts_by_elder(session, elder_id)
        assert len(alerts) == 1
        assert alerts[0].alert_type == "help"
