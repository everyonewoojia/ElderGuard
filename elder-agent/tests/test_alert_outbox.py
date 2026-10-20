"""Persistent local queue tests: synthetic alerts and freshly owned directories only."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import Mock

import pytest

from src.core.alert_outbox import AlertOutbox, OutboxError


def event(kind="fall", priority="P0", content="synthetic alert"):
    """Create one immutable test event identity."""
    return {"eventId": str(uuid4()), "type": kind, "priority": priority,
            "timestamp": "2026-10-18T00:00:00+00:00", "source": "edge",
            "payload": {"content": content}}


def queue(path, **kwargs):
    """No token is required to store a locally identified alert."""
    return AlertOutbox(path, device_id="device-a", elder_id=1,
                       cloud_url="https://EXAMPLE.invalid:443/", **kwargs)


def test_durable_identity_deduplication_and_restart(tmp_path):
    item = event()
    with queue(tmp_path) as outbox:
        record = outbox.enqueue(item)
        assert record["event_id"] == item["eventId"]
        assert record["device_id"] == "device-a"
        assert record["elder_id"] == 1
        assert record["cloud_origin"] == "https://example.invalid"
        assert record["state"] == "pending"
        assert outbox.enqueue(item) == record
        assert len(outbox.records()) == 1
    with queue(tmp_path) as restored:
        assert restored.records() == [record]
        assert restored.enqueue(item) == record


def test_changed_payload_cannot_replace_same_event(tmp_path):
    item = event()
    with queue(tmp_path) as outbox:
        before = outbox.enqueue(item)
        item["payload"]["content"] = "changed"
        with pytest.raises(OutboxError, match="event_conflict"):
            outbox.enqueue(item)
        assert outbox.records() == [before]


@pytest.mark.parametrize("field,value", [("priority", "P9"), ("eventId", "time-based"), ("type", "unknown")])
def test_invalid_event_never_changes_queue(tmp_path, field, value):
    with queue(tmp_path) as outbox:
        item = event()
        item[field] = value
        before = outbox.path.read_bytes()
        with pytest.raises(OutboxError):
            outbox.enqueue(item)
        assert outbox.path.read_bytes() == before


@pytest.mark.parametrize("binding", [
    {"device_id": "", "elder_id": 1, "cloud_url": "https://example.invalid"},
    {"device_id": "device-a", "elder_id": True, "cloud_url": "https://example.invalid"},
    {"device_id": "device-a", "elder_id": 1, "cloud_url": "https://user:password@example.invalid"},
])
def test_incomplete_or_secret_binding_is_rejected(tmp_path, binding):
    with pytest.raises(OutboxError):
        AlertOutbox(tmp_path, **binding)
    assert not (tmp_path / "alerts_outbox.json").exists()


def test_only_business_whitelist_is_persisted(tmp_path):
    import secrets
    secret = ".".join(secrets.token_urlsafe(24) for _ in range(3))
    item = event()
    item["authToken"] = secret
    item["Authorization"] = f"Bearer {secret}"
    item["payload"]["unrelated"] = secret
    with queue(tmp_path) as outbox:
        outbox.enqueue(item)
        text = outbox.path.read_text(encoding="utf-8")
        leaked = secret in text or "Authorization" in text or "authToken" in text
        assert not leaked
        assert "payload" not in json.loads(text)["records"][0]
        item = event(content=f"Bearer {secret}")
        with pytest.raises(OutboxError):
            outbox.enqueue(item)


def test_reserved_records_are_available_only_to_urgent_alerts(tmp_path):
    with queue(tmp_path, max_records=3, reserve_records=1,
               max_bytes=16384, reserve_bytes=1024) as outbox:
        outbox.enqueue(event("abnormal", "P1"))
        outbox.enqueue(event("abnormal", "P2"))
        with pytest.raises(OutboxError, match="capacity"):
            outbox.enqueue(event("abnormal", "P1"))
        outbox.enqueue(event("help", "P1"))
        before = outbox.records()
        with pytest.raises(OutboxError, match="capacity"):
            outbox.enqueue(event())
        assert outbox.records() == before


def test_byte_limit_and_priority_reserve(tmp_path):
    with queue(tmp_path, max_records=50, reserve_records=2,
               max_bytes=5000, reserve_bytes=2000) as outbox:
        while True:
            try:
                outbox.enqueue(event("abnormal", "P1", "x" * 350))
            except OutboxError as exc:
                assert exc.code == "capacity"
                break
        before = len(outbox.records())
        outbox.enqueue(event("help", "P0", "urgent"))
        assert len(outbox.records()) == before + 1
        assert outbox.path.stat().st_size <= 5000


def test_corrupt_file_is_not_overwritten(tmp_path):
    path = tmp_path / "alerts_outbox.json"
    path.write_bytes(b"{broken synthetic json")
    with pytest.raises(OutboxError, match="invalid_file"):
        queue(tmp_path)
    assert path.read_bytes() == b"{broken synthetic json"
    assert (tmp_path / ".alerts_outbox.lock").exists()


def test_invalid_record_and_binding_change_are_refused(tmp_path):
    with queue(tmp_path) as outbox:
        outbox.enqueue(event())
        path = outbox.path
    with pytest.raises(OutboxError, match="binding_mismatch"):
        AlertOutbox(tmp_path, device_id="device-b", elder_id=1,
                    cloud_url="https://example.invalid")
    body = json.loads(path.read_text(encoding="utf-8"))
    body["records"][0]["state"] = "received"
    path.write_text(json.dumps(body), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(OutboxError, match="invalid_file"):
        queue(tmp_path)
    assert path.read_bytes() == before


async def test_commit_before_simulation_and_no_received_state(tmp_path):
    with queue(tmp_path) as outbox:
        item = event()
        outbox.enqueue(item)
        calls = []

        async def fake_sender(record):
            stored = json.loads(outbox.path.read_text(encoding="utf-8"))["records"][0]
            assert stored["state"] == "sending"
            assert stored["event_id"] == record["event_id"] == item["eventId"]
            calls.append(record["event_id"])
            return "simulated"

        await outbox.simulate_attempt(item["eventId"], fake_sender)
        assert calls == [item["eventId"]]
        record = outbox.records()[0]
        assert record["state"] == "pending"
        assert record["cloud_alert_id"] is None
        assert record["last_error_code"] == "simulation_only"


@pytest.mark.parametrize("operation", ["fsync", "replace"])
async def test_write_failure_prevents_claim_and_sender(tmp_path, monkeypatch, operation):
    from src.core import alert_outbox as module
    with queue(tmp_path) as outbox:
        item = event()
        outbox.enqueue(item)
        before = outbox.path.read_bytes()
        monkeypatch.setattr(module.os, operation, Mock(side_effect=OSError("controlled disk failure")))
        sender = Mock()
        with pytest.raises(OutboxError, match="write_failed"):
            await outbox.simulate_attempt(item["eventId"], sender)
        sender.assert_not_called()
        assert outbox.path.read_bytes() == before
        assert list(tmp_path.glob(".alerts-outbox-*.tmp")) == []


async def test_attempt_limit_survives_restarts(tmp_path):
    now = [datetime(2026, 10, 18, tzinfo=timezone.utc)]
    item = event()
    calls = []

    async def fail(record):
        calls.append(record["event_id"])
        raise OSError("exception details must not be persisted")

    for index in range(4):
        with queue(tmp_path, now=lambda: now[0]) as outbox:
            outbox.enqueue(item)
            await outbox.simulate_attempt(item["eventId"], fail)
        now[0] += timedelta(seconds=60)
    with queue(tmp_path, now=lambda: now[0]) as outbox:
        assert outbox.records()[0]["state"] == "exhausted"
        assert outbox.records()[0]["attempts"] == 4
        await outbox.simulate_attempt(item["eventId"], fail)
        assert len(calls) == 4
        assert "exception details" not in outbox.path.read_text(encoding="utf-8")


async def test_concurrent_simulation_is_single_claim(tmp_path):
    with queue(tmp_path) as outbox:
        item = event()
        outbox.enqueue(item)
        calls = []

        async def sender(record):
            calls.append(record["event_id"])
            await asyncio.sleep(0)
            return "simulated"

        await asyncio.gather(*(outbox.simulate_attempt(item["eventId"], sender) for _ in range(4)))
        assert calls == [item["eventId"]]


def test_same_process_ownership_and_independent_directories(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    with queue(tmp_path):
        with pytest.raises(OutboxError, match="owner_busy"):
            queue(tmp_path)
        with queue(other):
            pass
    with queue(tmp_path):
        pass


def test_replaced_queue_path_is_not_adopted(tmp_path):
    with queue(tmp_path) as outbox:
        original = outbox.path
        moved = tmp_path / "saved-original.json"
        original.rename(moved)
        original.write_text('{"replacement": true}', encoding="utf-8")
        with pytest.raises(OutboxError, match="identity"):
            outbox.enqueue(event())
        assert original.read_text(encoding="utf-8") == '{"replacement": true}'
        assert moved.exists()


def test_in_place_changes_cannot_be_overwritten(tmp_path):
    with queue(tmp_path) as outbox:
        outbox.enqueue(event())
        changed = b'{"changed-in-place":true}'
        outbox.path.write_bytes(changed)
        with pytest.raises(OutboxError, match="identity"):
            outbox.enqueue(event())
        assert outbox.path.read_bytes() == changed


def test_binding_snapshot_cannot_be_changed_by_caller(tmp_path):
    with queue(tmp_path) as outbox:
        binding = outbox.binding
        binding["device_id"] = "another-device"
        assert outbox.enqueue(event())["device_id"] == "device-a"


@pytest.mark.parametrize("status", [401, 403])
async def test_simulated_authentication_failure_stays_blocked(tmp_path, status):
    with queue(tmp_path) as outbox:
        item = event()
        outbox.enqueue(item)
        calls = []

        async def reject(record):
            calls.append(record["event_id"])
            return status

        await outbox.simulate_attempt(item["eventId"], reject)
        await outbox.simulate_attempt(item["eventId"], reject)
        assert calls == [item["eventId"]]
        assert outbox.get(item["eventId"])["state"] == "blocked"
    with queue(tmp_path) as recovered:
        assert recovered.get(item["eventId"])["http_status"] == status
        assert recovered.claim(item["eventId"]) is None


async def test_cancelled_simulation_keeps_durable_claim_for_recovery(tmp_path):
    item = event()
    entered = asyncio.Event()

    async def wait(record):
        entered.set()
        await asyncio.Event().wait()

    with queue(tmp_path) as outbox:
        outbox.enqueue(item)
        task = asyncio.create_task(outbox.simulate_attempt(item["eventId"], wait))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert outbox.get(item["eventId"])["state"] == "sending"
    with queue(tmp_path) as recovered:
        record = recovered.get(item["eventId"])
        assert record["state"] == "pending"
        assert record["attempts"] == 1
        assert record["last_error_code"] == "interrupted"


def test_unconfirmed_directory_ownership_is_refused_without_files(tmp_path, monkeypatch):
    from src.core import alert_outbox
    monkeypatch.setattr(alert_outbox, "_owned", Mock(side_effect=OutboxError("ownership")))
    with pytest.raises(OutboxError, match="ownership"):
        queue(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_initial_write_failure_releases_os_ownership(tmp_path, monkeypatch):
    from src.core import alert_outbox
    with monkeypatch.context() as patch:
        patch.setattr(alert_outbox.os, "replace", Mock(side_effect=PermissionError("controlled file occupation")))
        with pytest.raises(OutboxError, match="write_failed"):
            queue(tmp_path)
        assert not (tmp_path / "alerts_outbox.json").exists()
        assert list(tmp_path.glob(".alerts-outbox-*.tmp")) == []
    with queue(tmp_path) as recovered:
        assert recovered.records() == []
