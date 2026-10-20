"""Agent → awaited event bus → real plugin → local durable outbox, with no sender."""
from unittest.mock import AsyncMock, Mock
from uuid import UUID
import asyncio
import json

import pytest

from src.core.agent import ElderAgent
from src.core.memory import MemoryManager
from src.core.event_bus import EventBus
from src.plugins.alert import AlertPlugin
from test_alert_outbox import event, queue


async def test_agent_bus_plugin_and_disk_keep_one_uuid(tmp_path):
    with queue(tmp_path) as outbox:
        http = Mock(report_alert=Mock(side_effect=AssertionError("production sends forbidden")),
                    fetch_privacy=AsyncMock(return_value=False))
        agent = ElderAgent(memory=MemoryManager(str(tmp_path)), edge_client=http,
                           elder_id=1, alert_outbox=outbox)
        observed = []

        async def observe(item):
            observed.append(item)

        await agent.event_bus.subscribe("help", observe)
        try:
            await agent.start()
            await agent.handle_voice_text("救命")
            assert len(observed) == 1
            identity = observed[0]["eventId"]
            assert UUID(identity).version == 4
            assert outbox.records()[0]["event_id"] == identity
            plugin = next(p for p in agent.plugins if isinstance(p, AlertPlugin))
            assert plugin.get_alert(identity)["storage_status"] == "persisted"
            await agent.event_bus.publish(observed[0])
            assert len(outbox.records()) == 1
            await agent.handle_voice_text("救命")
            assert len(outbox.records()) == 2
            assert outbox.records()[0]["event_id"] != outbox.records()[1]["event_id"]
            http.report_alert.assert_not_called()
        finally:
            await agent.stop()


async def test_direct_plugin_generates_id_once_and_deduplicates(tmp_path):
    with queue(tmp_path) as outbox:
        plugin = AlertPlugin(EventBus(), tts=Mock(speak=AsyncMock()), outbox=outbox)
        item = {"type": "fall", "priority": "P0", "payload": {"content": "synthetic"}}
        assert await plugin.handle_event(item) is True
        identity = item["eventId"]
        assert UUID(identity).version == 4
        assert await plugin.handle_event(item) is True
        assert item["eventId"] == identity
        assert len(outbox.records()) == 1


async def test_persistence_failure_has_local_feedback_and_no_success(tmp_path, monkeypatch):
    from src.core import alert_outbox
    with queue(tmp_path) as outbox:
        tts = Mock(speak=AsyncMock())
        plugin = AlertPlugin(EventBus(), tts=tts, outbox=outbox)
        item = event()
        monkeypatch.setattr(alert_outbox.os, "replace", Mock(side_effect=OSError("controlled disk failure")))
        assert await plugin.handle_event(item) is False
        assert plugin.get_alert(item["eventId"])["storage_status"] == "write_failed"
        assert tts.speak.await_count == 1
        assert "未能保存" in tts.speak.call_args.args[0]


async def test_main_configures_only_local_queue(tmp_path, monkeypatch):
    from src import main
    monkeypatch.setenv("ELDER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EDGE_DEVICE_ID", "device-a")
    monkeypatch.setenv("EDGE_ELDER_ID", "1")
    monkeypatch.setenv("EDGE_BASE_URL", "https://example.invalid")
    fake = Mock(fetch_privacy=AsyncMock(return_value=False), aclose=AsyncMock(),
                report_alert=Mock(side_effect=AssertionError("network send forbidden")))
    monkeypatch.setattr(main, "EdgeClient", Mock(return_value=fake))
    # Isolate the original local-only assertion; the real default Reporter
    # chain is independently exercised by test_reporter_production.
    monkeypatch.setattr(main, "EdgeReporter", lambda client, outbox: Mock(
        start=AsyncMock(), stop=AsyncMock(), wake=Mock(), task=None, outbox=outbox))
    agent = main.create_agent()
    try:
        await agent.start()
        await agent.handle_voice_text("救命")
        assert agent.alert_outbox.records()[0]["state"] == "pending"
        fake.report_alert.assert_not_called()
    finally:
        await agent.stop()
    with queue(tmp_path) as restored:
        assert len(restored.records()) == 1


async def test_plan_and_execute_preserve_uuid_before_dispatch(tmp_path):
    agent = ElderAgent(memory=MemoryManager(str(tmp_path)), tts=Mock(speak=AsyncMock()))
    observed = []

    async def observe(item):
        observed.append(item["eventId"])

    await agent.event_bus.subscribe("help", observe)
    await agent._plan({"type": "help", "priority": "P0", "content": "synthetic"})
    action = {"type": "help", "priority": "P0", "payload": {"content": "synthetic"}}
    await agent.execute(action)
    await agent.execute(action)
    assert UUID(observed[0]).version == 4
    assert observed[1:] == [action["eventId"], action["eventId"]]
    assert observed[0] != observed[1]
    await agent.stop()


async def test_owned_queue_released_even_if_asr_close_fails(tmp_path):
    outbox = queue(tmp_path)
    asr = Mock(close=AsyncMock(side_effect=OSError("controlled shutdown failure")))
    agent = ElderAgent(memory=MemoryManager(str(tmp_path)), asr=asr,
                       alert_outbox=outbox, owns_alert_outbox=True)
    with pytest.raises(OSError, match="controlled shutdown failure"):
        await agent.stop()
    await agent.stop()
    with queue(tmp_path) as replacement:
        assert replacement.records() == []


async def test_plugin_logs_and_feedback_never_include_payload_or_token(tmp_path, caplog):
    import secrets
    secret = ".".join(secrets.token_urlsafe(24) for _ in range(3))
    with queue(tmp_path) as outbox:
        tts = Mock(speak=AsyncMock())
        plugin = AlertPlugin(EventBus(), tts=tts, outbox=outbox)
        item = event()
        item["authToken"] = secret
        item["payload"]["unrelated"] = secret
        with caplog.at_level("INFO"):
            assert await plugin.handle_event(item) is True
        leaked = secret in caplog.text or secret in str(plugin.active_alerts()) or secret in str(tts.speak.call_args)
        assert not leaked
        assert "本机保存" in tts.speak.call_args.args[0]
        assert "正在通知" not in tts.speak.call_args.args[0]


async def test_missing_public_binding_never_creates_sendable_record(tmp_path, monkeypatch):
    from src import main
    monkeypatch.setenv("ELDER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("EDGE_DEVICE_ID", raising=False)
    monkeypatch.delenv("EDGE_DEVICE_TOKEN_FILE", raising=False)
    monkeypatch.setenv("EDGE_ELDER_ID", "1")
    monkeypatch.setenv("EDGE_BASE_URL", "https://example.invalid")
    agent = main.create_agent()
    agent.tts.speak = AsyncMock()
    try:
        await agent.start()
        await agent.handle_voice_text("救命")
        assert agent.alert_outbox is None
        assert not (tmp_path / "alerts_outbox.json").exists()
        plugin = next(p for p in agent.plugins if isinstance(p, AlertPlugin))
        assert list(plugin.active_alerts().values())[0]["storage_status"] == "unconfigured"
    finally:
        await agent.stop()


@pytest.mark.parametrize("constructor", ["EdgeClient", "ElderAgent"])
def test_factory_error_releases_queue_ownership(tmp_path, monkeypatch, constructor):
    from src import main
    monkeypatch.setenv("ELDER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EDGE_DEVICE_ID", "device-a")
    monkeypatch.setenv("EDGE_ELDER_ID", "1")
    monkeypatch.setenv("EDGE_BASE_URL", "https://example.invalid")
    monkeypatch.setattr(main, "EdgeClient", Mock(return_value=Mock(base_url="https://example.invalid")))
    monkeypatch.setattr(main, constructor, Mock(side_effect=RuntimeError("controlled construction failure")))
    with pytest.raises(RuntimeError, match="controlled construction failure"):
        main.create_agent()
    with queue(tmp_path) as recovered:
        assert recovered.records() == []


@pytest.mark.parametrize("entry", ["voice", "plan", "execute"])
async def test_agent_persists_before_first_subscriber_can_suspend(tmp_path, entry):
    """Cancel a real publish task while an earlier subscriber blocks the plugin."""
    with queue(tmp_path) as outbox:
        intent = {"type": "help", "priority": "P0", "content": "synthetic help"}
        agent = ElderAgent(memory=MemoryManager(str(tmp_path)), alert_outbox=outbox,
                           intent=Mock(parse=AsyncMock(return_value=intent)),
                           tts=Mock(speak=AsyncMock()))
        entered = asyncio.Event()
        observed = []

        async def first(item):
            observed.append(item["eventId"])
            entered.set()
            await asyncio.Event().wait()

        await agent.event_bus.subscribe("help", first)
        await agent.start()
        if entry == "voice":
            work = asyncio.create_task(agent.handle_voice_text("synthetic help"))
        elif entry == "plan":
            work = asyncio.create_task(agent._plan(intent))
        else:
            work = asyncio.create_task(agent.execute(event("help")))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            # The event has reached the bus; the plugin is still uncalled.
            assert outbox.get(observed[0]) is not None
            assert outbox.get(observed[0])["state"] == "pending"
            assert outbox.get(observed[0])["cloud_alert_id"] is None
            assert all(not p.active_alerts() for p in agent.plugins if isinstance(p, AlertPlugin))
        finally:
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
            await agent.stop()
    with queue(tmp_path) as recovered:
        assert recovered.records()[0]["event_id"] == observed[0]


async def test_normal_stop_cancels_real_main_loop_after_durable_admission(tmp_path):
    """Use actual start → main loop → sense → parse → publish → stop ordering."""
    outbox = queue(tmp_path)
    asr = Mock(init=AsyncMock(return_value=True), listen=AsyncMock(return_value=b"synthetic"),
               recognize=AsyncMock(return_value="synthetic help"), close=AsyncMock())
    client = Mock(fetch_privacy=AsyncMock(return_value=True),
                  report_alert=Mock(side_effect=AssertionError("HTTP forbidden")))
    agent = ElderAgent(memory=MemoryManager(str(tmp_path)), asr=asr, elder_id=1,
                       edge_client=client, alert_outbox=outbox, owns_alert_outbox=True,
                       tts=Mock(speak=AsyncMock()),
                       intent=Mock(parse=AsyncMock(return_value={"type": "help", "priority": "P0", "content": "synthetic"})))
    entered = asyncio.Event()
    observed = []

    async def first(item):
        observed.append(item["eventId"])
        entered.set()
        await asyncio.Event().wait()

    await agent.event_bus.subscribe("help", first)
    try:
        await agent.start()
        await asyncio.wait_for(entered.wait(), 5)
        await agent.stop()
        with queue(tmp_path) as recovered:
            assert len(recovered.records()) == 1
            assert recovered.get(observed[0])["state"] == "pending"
        client.report_alert.assert_not_called()
        asr.close.assert_awaited_once()
    finally:
        await agent.stop()
        outbox.close()


@pytest.mark.parametrize("configured", [True, False])
async def test_unexpected_plugin_error_reaches_agent_after_safe_admission(tmp_path, monkeypatch, configured):
    """Isolation still runs other subscribers but must not imply successful handling."""
    with queue(tmp_path) as outbox:
        agent = ElderAgent(memory=MemoryManager(str(tmp_path)), alert_outbox=outbox if configured else None,
                           tts=Mock(speak=AsyncMock()))
        plugin = next(p for p in agent.plugins if isinstance(p, AlertPlugin))
        monkeypatch.setattr(plugin, "handle_event", AsyncMock(side_effect=RuntimeError("controlled plugin failure")))
        await agent.start()
        action = event("help")
        try:
            with pytest.raises(RuntimeError, match="event_dispatch_failed"):
                await agent.execute(action)
            if configured:
                assert outbox.get(action["eventId"])["state"] == "pending"
            else:
                assert outbox.records() == []
        finally:
            await agent.stop()


async def test_agent_disk_failure_is_not_a_successful_intent_or_publish(tmp_path, monkeypatch):
    from src.core import alert_outbox
    with queue(tmp_path) as outbox:
        tts = Mock(speak=AsyncMock())
        client = Mock(report_alert=Mock(side_effect=AssertionError("HTTP forbidden")))
        agent = ElderAgent(memory=MemoryManager(str(tmp_path)), alert_outbox=outbox,
                           edge_client=client, tts=tts,
                           intent=Mock(parse=AsyncMock(return_value={"type": "help", "priority": "P0", "content": "synthetic"})))
        agent.event_bus.publish = AsyncMock()
        before = outbox.path.read_bytes()
        monkeypatch.setattr(alert_outbox.os, "replace", Mock(side_effect=OSError("controlled disk failure")))
        try:
            with pytest.raises(alert_outbox.OutboxError, match="write_failed"):
                await agent.handle_voice_text("synthetic")
            agent.event_bus.publish.assert_not_awaited()
            client.report_alert.assert_not_called()
            assert outbox.path.read_bytes() == before
            assert any("未能保存" in call.args[0] for call in tts.speak.call_args_list)
            assert not any("已在本机保存" in call.args[0] or "云端已接收" in call.args[0]
                           for call in tts.speak.call_args_list)
        finally:
            await agent.stop()


async def test_commit_abort_propagates_cancellation_without_saved_claim(tmp_path, monkeypatch):
    """Inject abort before replace; this is distinct from cooperative task.cancel."""
    from src.core import alert_outbox
    with queue(tmp_path) as outbox:
        tts = Mock(speak=AsyncMock())
        agent = ElderAgent(memory=MemoryManager(str(tmp_path)), alert_outbox=outbox, tts=tts)
        action = event("help")
        before = outbox.path.read_bytes()
        monkeypatch.setattr(alert_outbox.os, "replace", Mock(side_effect=asyncio.CancelledError()))
        try:
            with pytest.raises(asyncio.CancelledError):
                await agent.execute(action)
            assert outbox.path.read_bytes() == before
            assert outbox.records() == []
            assert not any("本机保存" in call.args[0] for call in tts.speak.call_args_list)
        finally:
            await agent.stop()


async def test_retry_after_cancel_uses_original_id_and_single_disk_commit(tmp_path, monkeypatch):
    with queue(tmp_path) as outbox:
        commit = Mock(wraps=outbox._commit)
        monkeypatch.setattr(outbox, "_commit", commit)
        agent = ElderAgent(memory=MemoryManager(str(tmp_path)), alert_outbox=outbox,
                           tts=Mock(speak=AsyncMock()))
        entered = asyncio.Event()
        action = event("help")

        async def first(item):
            entered.set()
            await asyncio.Event().wait()

        await agent.event_bus.subscribe("help", first)
        await agent.start()
        task = asyncio.create_task(agent.execute(action))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await agent.event_bus.unsubscribe("help", first)
            result = await agent.execute(action)
            assert result["event_id"] == action["eventId"]
            assert result["published"] is True
            assert result["persisted"] is True
            assert result["cloud_received"] is None
            assert len(outbox.records()) == 1
            assert commit.call_count == 1
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            await agent.stop()


async def test_stopped_agent_rejects_new_alert_without_reopening_queue(tmp_path):
    with queue(tmp_path) as outbox:
        agent = ElderAgent(memory=MemoryManager(str(tmp_path)), alert_outbox=outbox)
        await agent.stop()
        with pytest.raises(RuntimeError, match="agent_stopping"):
            await agent.execute(event("help"))
        assert outbox.records() == []


async def test_real_cancel_requested_before_replace_is_delivered_after_commit(tmp_path, monkeypatch):
    """Actual task.cancel request, not an artificial await inside synchronous I/O."""
    from src.core import alert_outbox
    with queue(tmp_path) as outbox:
        tts = Mock(speak=AsyncMock())
        client = Mock(report_alert=Mock(side_effect=AssertionError("HTTP forbidden")))
        agent = ElderAgent(memory=MemoryManager(str(tmp_path)), alert_outbox=outbox,
                           edge_client=client, tts=tts)
        action = event("help")
        original_replace = alert_outbox.os.replace
        requested = []

        async def suspend(item):
            await asyncio.Event().wait()

        await agent.event_bus.subscribe("help", suspend)

        def request_cancel(source, destination):
            assert json.loads(outbox.path.read_text(encoding="utf-8"))["records"] == []
            assert not any("本机保存" in call.args[0] for call in tts.speak.call_args_list)
            requested.append(True)
            assert work.cancel() is True
            assert work.cancelling() == 1  # Requested before the real replace.
            return original_replace(source, destination)

        monkeypatch.setattr(alert_outbox.os, "replace", request_cancel)
        work = asyncio.create_task(agent.execute(action))
        try:
            with pytest.raises(asyncio.CancelledError):
                await work
            assert requested == [True]
            assert outbox.get(action["eventId"])["state"] == "pending"
            assert outbox.get(action["eventId"])["cloud_alert_id"] is None
            client.report_alert.assert_not_called()
        finally:
            if not work.done():
                work.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await work
            await agent.stop()
    with queue(tmp_path) as recovered:
        assert recovered.get(action["eventId"])["state"] == "pending"
