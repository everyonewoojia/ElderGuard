"""Real startup/Agent/plugin/outbox/reporter/client, synthetic HTTP and audio only."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import secrets
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from src.core.alert_outbox import OutboxError
from src.integration.edge import EdgeClient, EdgeReporter
from test_alert_outbox import event, queue


def receipt(item, **changes):
    """Match the actual cloud admission contract; never invent confirmation."""
    data = {"alert_id": 17, "event_id": item["eventId"], "device_id": "device-a", "status": "pending",
            "receipt_status": "received", "notification": "unknown", "notification_attempts": 1,
            "notification_send_count": None, "notification_last_result": "unknown", "confirmation": "unconfirmed"}
    data.update(changes)
    return {"code": 200, "message": "accepted", "data": data}


def token():
    """An ephemeral transport-format token, not a production credential."""
    return ".".join(secrets.token_urlsafe(24) for _ in range(3))


def setup_reporter(outbox, handler):
    """Caller owns the injected HTTP transport; reporter owns no second queue."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = EdgeClient(base_url="https://example.invalid", client=http, device_token=token())
    return EdgeReporter(client, outbox=outbox, poll_interval=0.01, shutdown_timeout=0.02), http


async def test_default_main_wires_one_worker_and_full_local_chain(tmp_path, monkeypatch):
    from src import main
    from src.plugins.alert import AlertPlugin
    secret = token()
    credential = tmp_path / "device.credential"
    credential.write_text(secret, encoding="utf-8")
    for name, value in {"ELDER_DATA_DIR": str(tmp_path), "EDGE_DEVICE_ID": "device-a", "EDGE_ELDER_ID": "1",
                        "EDGE_BASE_URL": "https://example.invalid", "EDGE_DEVICE_TOKEN_FILE": str(credential)}.items():
        monkeypatch.setenv(name, value)
    posted, messages = [], []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"code": 200, "message": "success", "data": {"elder_id": 1, "enabled": False}})
        body = json.loads(request.content)
        valid_auth = request.headers["Authorization"] == f"Bearer {secret}"
        leaked = secret in request.url.__str__() + request.content.decode()
        assert valid_auth and not leaked
        posted.append(body)
        return httpx.Response(200, json=receipt({"eventId": body["event_id"]}))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real_client = main.EdgeClient
    monkeypatch.setattr(main, "EdgeClient", lambda **kwargs: real_client(client=http, **kwargs))
    agent = main.create_agent()
    agent.tts.speak = AsyncMock(side_effect=lambda message: messages.append(message))
    seen = []
    async def observe(item):
        seen.append(item)
    await agent.event_bus.subscribe("help", observe)
    try:
        await agent.start()
        original_task = agent.reporter.task
        await agent.start()
        assert agent.reporter.task is original_task
        result = await agent.handle_voice_text("救命")
        event_id = result["alert_result"]["event_id"]
        for _ in range(100):
            if agent.alert_outbox.get(event_id)["state"] == "received":
                break
            await asyncio.sleep(0.01)
        row = agent.alert_outbox.get(event_id)
        assert row["state"] == "received" and row["cloud_alert_id"] == 17
        assert row["cloud_notification"] == "unknown" and row["cloud_confirmation"] == "unconfirmed"
        assert posted[0]["event_id"] == event_id == seen[0]["eventId"]
        assert posted[0]["elder_id"] == 1 and posted[0]["alert_type"] == "help"
        plugin = next(p for p in agent.plugins if isinstance(p, AlertPlugin))
        assert plugin.get_alert(event_id)["storage_status"] == "persisted"
        await agent.event_bus.publish(seen[0])
        await agent.execute({"type": "time", "payload": {}})
        await asyncio.sleep(0.03)
        assert len(posted) == len(agent.alert_outbox.records()) == 1
        assert all("家属已收到" not in message for message in messages)
        leaked = secret in agent.alert_outbox.path.read_text(encoding="utf-8")
        assert not leaked
    finally:
        await agent.stop()
        assert agent.reporter.task is None
        assert not http.is_closed  # Injected transport remains caller-owned.
        await http.aclose()


@pytest.mark.parametrize("status,state,error", [(401,"blocked","authentication"), (403,"blocked","authentication"),
    (409,"failed","conflict"), (422,"failed","invalid_payload"), (400,"failed","request_rejected"),
    (500,"pending","server_error"), (503,"pending","server_error")])
async def test_status_classification_is_persistent(tmp_path, status, state, error):
    item = event()
    with queue(tmp_path) as outbox:
        outbox.enqueue(item)
        reporter, http = setup_reporter(outbox, lambda request: httpx.Response(status))
        try:
            assert await reporter.run_once() is False
            row = outbox.get(item["eventId"])
            assert row["state"] == state and row["last_error_code"] == error
            assert row["http_status"] == status and row["attempts"] == 1
            assert row["cloud_alert_id"] is None
        finally:
            await http.aclose()


@pytest.mark.parametrize("malformed", [None, {}, [], "text", {"event_id": "wrong"},
    {"device_id": "another-device"}, {"alert_id": True}, {"receipt_status": "ok"},
    {"confirmation": "confirmed"}, {"notification_attempts": "1"}])
async def test_invalid_receipt_never_marks_received(tmp_path, malformed):
    item = event()
    body = receipt(item)
    if isinstance(malformed, dict) and malformed:
        body["data"].update(malformed)
    else:
        body = malformed
    with queue(tmp_path) as outbox:
        outbox.enqueue(item)
        reporter, http = setup_reporter(outbox, lambda request: httpx.Response(200, json=body))
        try:
            assert await reporter.run_once() is False
            assert outbox.get(item["eventId"])["state"] == "pending"
            assert outbox.get(item["eventId"])["last_error_code"] == "invalid_response"
        finally:
            await http.aclose()


async def test_lost_response_retries_same_identity_until_exhaustion(tmp_path):
    now = [datetime(2026, 10, 18, tzinfo=timezone.utc)]
    sent = []
    def unavailable(request):
        sent.append(json.loads(request.content)["event_id"])
        raise httpx.ReadError("sensitive synthetic exception")
    item = event()
    with queue(tmp_path, now=lambda: now[0]) as outbox:
        outbox.enqueue(item)
        reporter, http = setup_reporter(outbox, unavailable)
        try:
            for _ in range(4):
                assert await reporter.run_once() is False
                now[0] += timedelta(seconds=120)
            assert outbox.get(item["eventId"])["state"] == "exhausted"
            assert outbox.get(item["eventId"])["attempts"] == 4
            assert await reporter.run_once() is False
            assert sent == [item["eventId"]] * 4
        finally:
            await http.aclose()


async def test_response_lost_after_commit_recovers_same_event_after_restart(tmp_path):
    item = event()
    committed = {}
    def handler(request):
        event_id = json.loads(request.content)["event_id"]
        if event_id not in committed:
            committed[event_id] = 17
            raise httpx.ReadTimeout("response lost")
        return httpx.Response(200, json=receipt(item))
    with queue(tmp_path) as outbox:
        outbox.enqueue(item)
        first, http = setup_reporter(outbox, handler)
        try:
            assert await first.run_once() is False
        finally:
            await http.aclose()
    future = lambda: datetime.now(timezone.utc) + timedelta(minutes=5)
    with queue(tmp_path, now=future) as outbox:
        second, http = setup_reporter(outbox, handler)
        try:
            assert await second.run_once() is True
            assert outbox.get(item["eventId"])["cloud_alert_id"] == committed[item["eventId"]]
            assert len(committed) == 1
        finally:
            await http.aclose()


async def test_cancel_during_http_preserves_sending_for_restart(tmp_path):
    entered = asyncio.Event()
    async def handler(request):
        entered.set()
        await asyncio.Event().wait()
    item = event()
    with queue(tmp_path) as outbox:
        outbox.enqueue(item)
        reporter, http = setup_reporter(outbox, handler)
        task = asyncio.create_task(reporter.run_once())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert outbox.get(item["eventId"])["state"] == "sending"
        await http.aclose()
    with queue(tmp_path) as outbox:
        assert outbox.get(item["eventId"])["state"] == "pending"


async def test_receipt_write_failure_is_not_completion(tmp_path, monkeypatch):
    item = event()
    with queue(tmp_path) as outbox:
        outbox.enqueue(item)
        reporter, http = setup_reporter(outbox, lambda request: httpx.Response(200, json=receipt(item)))
        monkeypatch.setattr(outbox, "mark_received", Mock(side_effect=OutboxError("write_failed")))
        try:
            with pytest.raises(OutboxError):
                await reporter.run_once()
            assert outbox.get(item["eventId"])["state"] == "sending"
            assert outbox.get(item["eventId"])["cloud_alert_id"] is None
        finally:
            await http.aclose()


async def test_stop_timeout_keeps_task_and_does_not_release_dependencies(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return httpx.Response(503)
    with queue(tmp_path) as outbox:
        outbox.enqueue(event())
        reporter, http = setup_reporter(outbox, handler)
        await reporter.start()
        await entered.wait()
        original = reporter.task
        try:
            with pytest.raises(RuntimeError, match="reporter_shutdown_timeout"):
                await reporter.stop()
            assert reporter.task is original and not original.done()
            await reporter.start()
            assert reporter.task is original
            assert not http.is_closed
        finally:
            release.set()
            await reporter.stop()
            await http.aclose()


async def test_agent_disk_rejection_never_reaches_http(tmp_path, monkeypatch):
    from src.core.agent import ElderAgent
    from src.core.memory import MemoryManager
    called = []
    with queue(tmp_path) as outbox:
        reporter, http = setup_reporter(outbox, lambda request: called.append(request))
        agent = ElderAgent(memory=MemoryManager(str(tmp_path)), alert_outbox=outbox, reporter=reporter,
                           tts=Mock(speak=AsyncMock()))
        await agent.start()
        try:
            monkeypatch.setattr(outbox, "enqueue", Mock(side_effect=OutboxError("write_failed")))
            with pytest.raises(OutboxError):
                await agent.execute(event())
            await asyncio.sleep(0.03)
            assert called == [] and outbox.records() == []
        finally:
            await agent.stop()
            await http.aclose()


async def test_tts_failure_and_repeated_wake_do_not_prevent_send(tmp_path):
    from src.core.agent import ElderAgent
    from src.core.memory import MemoryManager
    item = event("help")
    with queue(tmp_path) as outbox:
        reporter, http = setup_reporter(outbox, lambda request: httpx.Response(200, json=receipt(item)))
        agent = ElderAgent(memory=MemoryManager(str(tmp_path)), alert_outbox=outbox, reporter=reporter,
                           tts=Mock(speak=AsyncMock(side_effect=RuntimeError("synthetic tts failure"))))
        await agent.start()
        try:
            result = await agent.execute(item)
            assert result["persisted"] is True
            for _ in range(100):
                reporter.wake()
                if outbox.get(item["eventId"])["state"] == "received":
                    break
                await asyncio.sleep(0.01)
            assert outbox.get(item["eventId"])["state"] == "received"
            assert outbox.get(item["eventId"])["attempts"] == 1
        finally:
            await agent.stop()
            await http.aclose()


async def test_cancel_before_claim_does_not_start_http(tmp_path):
    calls = []
    with queue(tmp_path) as outbox:
        item = event()
        outbox.enqueue(item)
        reporter, http = setup_reporter(outbox, lambda request: calls.append(request))
        task = asyncio.create_task(reporter.run_once())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert outbox.get(item["eventId"])["attempts"] == 0
        assert calls == []
        await http.aclose()


def test_endpoint_mismatch_fails_before_network(tmp_path):
    with queue(tmp_path) as outbox:
        client = EdgeClient(base_url="https://other.invalid", device_token=token())
        with pytest.raises(OutboxError, match="binding_mismatch"):
            EdgeReporter(client, outbox=outbox)


async def test_real_receipt_disk_failure_survives_reopen(tmp_path, monkeypatch):
    from src.core import alert_outbox as module
    item = event()
    with queue(tmp_path) as outbox:
        outbox.enqueue(item)
        def handler(request):
            monkeypatch.setattr(module.os, "replace", Mock(side_effect=OSError("synthetic disk failure")))
            return httpx.Response(200, json=receipt(item))
        reporter, http = setup_reporter(outbox, handler)
        try:
            with pytest.raises(OutboxError, match="write_failed"):
                await reporter.run_once()
        finally:
            await http.aclose()
    monkeypatch.undo()
    with queue(tmp_path) as restored:
        row = restored.get(item["eventId"])
        assert row["state"] == "pending" and row["attempts"] == 1
        assert row["cloud_alert_id"] is None


async def test_reporter_start_failure_releases_agent_owned_queue(tmp_path):
    from src.core.agent import ElderAgent
    from src.core.memory import MemoryManager
    outbox = queue(tmp_path)
    reporter = Mock(start=AsyncMock(side_effect=RuntimeError("reporter_start_failed")), stop=AsyncMock(), task=None, outbox=outbox)
    agent = ElderAgent(memory=MemoryManager(str(tmp_path)), alert_outbox=outbox, owns_alert_outbox=True,
                       reporter=reporter)
    with pytest.raises(RuntimeError, match="reporter_start_failed"):
        await agent.start()
    with queue(tmp_path) as recovered:
        assert recovered.records() == []
    assert all(not agent.event_bus.get_subscribers(kind) for kind in ("help", "fall", "abnormal"))


async def test_agent_shutdown_timeout_preserves_queue_lock_and_http(tmp_path):
    from src.core.agent import ElderAgent
    from src.core.memory import MemoryManager
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return httpx.Response(503)
    outbox = queue(tmp_path)
    reporter, http = setup_reporter(outbox, handler)
    agent = ElderAgent(memory=MemoryManager(str(tmp_path)), alert_outbox=outbox, owns_alert_outbox=True, reporter=reporter)
    outbox.enqueue(event())
    await agent.start()
    await entered.wait()
    try:
        with pytest.raises(RuntimeError, match="reporter_shutdown_timeout"):
            await agent.stop()
        assert not agent._resources_closed and not http.is_closed
        with pytest.raises(OutboxError, match="owner_busy"):
            queue(tmp_path)
    finally:
        release.set()
        await agent.stop()
        await http.aclose()
    with queue(tmp_path) as recovered:
        assert len(recovered.records()) == 1


async def test_errors_and_object_representations_are_redacted(tmp_path, caplog):
    with queue(tmp_path) as outbox:
        item = event()
        outbox.enqueue(item)
        secret = token()
        async def handler(request):
            raise httpx.ReadError(secret + " raw synthetic details")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = EdgeClient(base_url="https://example.invalid", device_token=secret, client=http)
            reporter = EdgeReporter(client, outbox=outbox)
            await reporter.run_once()
            leaked = secret in caplog.text + repr(client) + repr(reporter) + outbox.path.read_text(encoding="utf-8")
            assert not leaked


async def test_agent_rejects_a_reporter_using_another_outbox(tmp_path):
    from src.core.agent import ElderAgent
    from src.core.memory import MemoryManager
    other = tmp_path / "other"
    other.mkdir()
    with queue(tmp_path) as first, queue(other) as second:
        reporter, http = setup_reporter(second, lambda request: httpx.Response(503))
        try:
            with pytest.raises(OutboxError, match="binding_mismatch"):
                ElderAgent(memory=MemoryManager(str(tmp_path)), alert_outbox=first, reporter=reporter)
        finally:
            await http.aclose()


async def test_previous_queue_version_loads_and_received_state_survives_restart(tmp_path):
    item = event()
    with queue(tmp_path) as outbox:
        outbox.enqueue(item)
        path = outbox.path
    # An old on-disk snapshot in this test's directory, with no live owner.
    old = json.loads(path.read_text(encoding="utf-8"))
    old["version"] = 1
    old.pop("audit", None)
    for row in old["records"]:
        row.pop("cloud_notification")
        row.pop("cloud_confirmation")
        for key in ("revision", "terminal_at", "retry_grants"):
            row.pop(key, None)
    path.write_text(json.dumps(old), encoding="utf-8")
    with queue(tmp_path) as outbox:
        reporter, http = setup_reporter(outbox, lambda request: httpx.Response(200, json=receipt(item)))
        try:
            assert await reporter.run_once() is True
        finally:
            await http.aclose()
    with queue(tmp_path) as restored:
        assert restored.get(item["eventId"])["state"] == "received"
        assert restored.get(item["eventId"])["cloud_confirmation"] == "unconfirmed"
