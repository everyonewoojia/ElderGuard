"""Stage A: real local admission, synthetic events, no production networking."""
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.agent import ElderAgent
from src.core.memory import MemoryManager
from src.core.alert_outbox import AlertOutbox, OutboxError
from src.plugins.alert import AlertPlugin
from test_alert_outbox import event, queue


@pytest.mark.parametrize("kind", ["help", "fall", "abnormal"])
async def test_alert_admission_and_local_feedback_once(tmp_path, kind):
    with queue(tmp_path) as outbox:
        tts = Mock(speak=AsyncMock(return_value=True))
        agent = ElderAgent(memory=MemoryManager(str(tmp_path)), alert_outbox=outbox, tts=tts)
        plugin = next(p for p in agent.plugins if isinstance(p, AlertPlugin))
        await plugin.start()
        item = event(kind)
        identity = item["eventId"]
        commit = Mock(wraps=outbox._commit)
        outbox._commit = commit
        try:
            result = await agent.execute(item)
            assert result["persisted"] is True
            assert result["event_id"] == identity
            assert result["cloud_received"] is None
            tts.speak.assert_awaited_once()
            assert "未能保存" not in tts.speak.call_args.args[0]
            await agent.execute(item)
            assert item["eventId"] == identity
            assert len(outbox.records()) == 1
            assert commit.call_count == 1
        finally:
            await plugin.stop()
            await agent.stop()


@pytest.mark.parametrize("kind", ["help", "fall"])
@pytest.mark.parametrize("speech", ["ok", "false", "exception"])
async def test_disk_failure_uses_independent_feedback_and_reports_speech_failure(tmp_path, monkeypatch, caplog, kind, speech):
    with queue(tmp_path) as outbox:
        tts = Mock(speak=AsyncMock(return_value=speech != "false"))
        if speech == "exception":
            tts.speak.side_effect = OSError("controlled speech failure")
        agent = ElderAgent(memory=MemoryManager(str(tmp_path)), alert_outbox=outbox, tts=tts,
                           edge_client=Mock(report_alert=Mock()))
        agent.event_bus.publish = AsyncMock()
        item = event(kind)
        identity = item["eventId"]
        monkeypatch.setattr(outbox, "enqueue", Mock(side_effect=OutboxError("write_failed")))
        try:
            with caplog.at_level("WARNING"):
                with pytest.raises(OutboxError, match="write_failed"):
                    await agent.execute(item)
            agent.event_bus.publish.assert_not_awaited()
            agent.edge_client.report_alert.assert_not_called()
            tts.speak.assert_awaited_once()
            assert "未能保存" in tts.speak.call_args.args[0]
            assert "已在本机保存" not in tts.speak.call_args.args[0]
            assert item["eventId"] == identity
            assert outbox.records() == []
            if speech != "ok":
                assert "Local alert failure feedback unavailable" in caplog.text
        finally:
            await agent.stop()


@pytest.mark.parametrize("kind", ["reminder", "voice", "button", "time", "unknown"])
async def test_non_alerts_dispatch_without_outbox_admission(tmp_path, kind):
    with queue(tmp_path) as outbox:
        agent = ElderAgent(memory=MemoryManager(str(tmp_path)), alert_outbox=outbox)
        received = []

        async def observe(item):
            received.append(item)

        await agent.event_bus.subscribe(kind, observe)
        item = event(kind)
        try:
            result = await agent.execute(item)
            assert result["published"] is True
            assert result["persisted"] is False
            assert received == [item]
            assert item["type"] == kind
            assert outbox.records() == []
        finally:
            await agent.stop()


@pytest.mark.parametrize("field,value", [("priority", "P9"), ("payload", []), ("timestamp", "invalid")])
async def test_invalid_alert_never_dispatches_or_enters_queue(tmp_path, field, value):
    with queue(tmp_path) as outbox:
        agent = ElderAgent(memory=MemoryManager(str(tmp_path)), alert_outbox=outbox,
                           tts=Mock(speak=AsyncMock(return_value=True)))
        agent.event_bus.publish = AsyncMock()
        item = event()
        item[field] = value
        try:
            with pytest.raises(OutboxError):
                await agent.execute(item)
            agent.event_bus.publish.assert_not_awaited()
            assert outbox.records() == []
        finally:
            await agent.stop()


def test_invalid_elder_binding_cannot_create_outbox(tmp_path):
    with pytest.raises(OutboxError):
        AlertOutbox(tmp_path, device_id="synthetic-device", elder_id=0,
                    cloud_url="https://example.invalid")
    assert not (tmp_path / "alerts_outbox.json").exists()
