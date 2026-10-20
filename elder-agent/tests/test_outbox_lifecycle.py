"""Retention and audited decisions use one owned atomic snapshot, no real cloud."""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock
from uuid import uuid4

import pytest

from src.core.alert_outbox import OutboxError
from test_alert_outbox import event, queue
from test_reporter_production import receipt


def arguments(row, action, **changes):
    result = dict(event_id=row["event_id"], action=action, expected_revision=row["revision"],
                  confirm_event_id=row["event_id"], operation_id=str(uuid4()), operator="local-admin", reason="reviewed")
    result.update(changes)
    return result


def terminal(outbox, *, received=True):
    item = event()
    outbox.enqueue(item)
    claimed = outbox.claim(item["eventId"])
    if received:
        outbox.mark_received(item["eventId"], receipt(item)["data"], expected_attempts=claimed["attempts"], http_status=200)
    else:
        outbox.record_failure(item["eventId"], expected_attempts=claimed["attempts"], error="authentication", http_status=401)
    return outbox.get(item["eventId"])


def test_retention_only_prunes_proven_or_explicitly_resolved_records(tmp_path):
    now = [datetime(2026, 10, 18, tzinfo=timezone.utc)]
    with queue(tmp_path, now=lambda: now[0]) as outbox:
        received = terminal(outbox)
        blocked = terminal(outbox, received=False)
        pending = outbox.enqueue(event())
        resolved = terminal(outbox, received=False)
        outbox.decide(**arguments(resolved, "resolve"))
        assert outbox.prune_plan() == []
        now[0] += timedelta(days=31)
        assert set(outbox.prune_plan()) == {received["event_id"], resolved["event_id"]}
        assert outbox.prune() == 2
        assert {r["event_id"] for r in outbox.records()} == {blocked["event_id"], pending["event_id"]}
        assert len(outbox.audit()) == 3
    with queue(tmp_path) as restored:
        assert len(restored.records()) == 2
        assert len(restored.audit()) == 3


def test_capacity_admission_prunes_old_received_without_overwriting_unresolved(tmp_path):
    now = [datetime(2026, 10, 18, tzinfo=timezone.utc)]
    with queue(tmp_path, max_records=3, reserve_records=1, max_bytes=16384, reserve_bytes=1024, now=lambda: now[0]) as outbox:
        saved = terminal(outbox)
        unresolved = terminal(outbox, received=False)
        outbox.enqueue(event())
        with pytest.raises(OutboxError, match="capacity"):
            outbox.enqueue(event())
        now[0] += timedelta(days=31)
        admitted = outbox.enqueue(event())
        assert outbox.get(saved["event_id"]) is None
        assert outbox.get(unresolved["event_id"]) is not None
        assert outbox.get(admitted["event_id"]) is not None
        assert outbox.path.stat().st_size <= outbox.max_bytes


@pytest.mark.parametrize("change", [{"confirm_event_id": "wrong"}, {"reason": ""}, {"operator": ""}, {"expected_revision": 999}])
def test_unsafe_manual_decision_is_not_written(tmp_path, change):
    with queue(tmp_path) as outbox:
        row = terminal(outbox, received=False)
        before = outbox.path.read_bytes()
        with pytest.raises(OutboxError):
            outbox.decide(**arguments(row, "resolve", **change))
        assert outbox.path.read_bytes() == before


def test_retry_requires_not_found_evidence_and_preserves_attempts(tmp_path):
    with queue(tmp_path) as outbox:
        row = terminal(outbox, received=False)
        args = arguments(row, "retry")
        with pytest.raises(OutboxError):
            outbox.decide(**args)
        evidence = {"receipt_status": "not_found", "event_id": row["event_id"], "device_id": "device-a"}
        first = outbox.decide(**args, evidence=evidence)
        assert outbox.decide(**args, evidence=evidence) == first
        stored = outbox.get(row["event_id"])
        assert stored["state"] == "pending" and stored["attempts"] == row["attempts"]
        assert len(outbox.audit()) == 1
        assert outbox.audit()[0]["before_error"] == "authentication"


def test_permanent_conflict_cannot_be_requeued_but_can_be_resolved(tmp_path):
    with queue(tmp_path) as outbox:
        item = event()
        row = outbox.enqueue(item)
        claim = outbox.claim(row["event_id"])
        outbox.record_failure(row["event_id"], expected_attempts=claim["attempts"], error="conflict", http_status=409)
        row = outbox.get(row["event_id"])
        evidence = {"receipt_status": "not_found", "event_id": row["event_id"], "device_id": "device-a"}
        with pytest.raises(OutboxError):
            outbox.decide(**arguments(row, "retry"), evidence=evidence)
        outbox.decide(**arguments(row, "resolve"))
        assert outbox.get(row["event_id"])["state"] == "resolved"
        assert outbox.get(row["event_id"])["content"] == row["content"]


def test_audit_capacity_refuses_destructive_cleanup(tmp_path):
    now = [datetime(2026, 10, 18, tzinfo=timezone.utc)]
    with queue(tmp_path, now=lambda: now[0], max_audit_records=1) as outbox:
        first = terminal(outbox, received=False)
        outbox.decide(**arguments(first, "resolve"))
        now[0] += timedelta(days=31)
        before = outbox.path.read_bytes()
        with pytest.raises(OutboxError, match="audit_capacity"):
            outbox.prune()
        assert outbox.path.read_bytes() == before


def test_prune_disk_failure_retains_old_snapshot(tmp_path, monkeypatch):
    from src.core import alert_outbox as module
    now = [datetime(2026, 10, 18, tzinfo=timezone.utc)]
    with queue(tmp_path, now=lambda: now[0]) as outbox:
        terminal(outbox)
        now[0] += timedelta(days=31)
        before = outbox.path.read_bytes()
        monkeypatch.setattr(module.os, "replace", Mock(side_effect=OSError("synthetic")))
        with pytest.raises(OutboxError):
            outbox.prune()
        assert outbox.path.read_bytes() == before
    monkeypatch.undo()
    with queue(tmp_path) as restored:
        assert len(restored.records()) == 1 and restored.audit() == []


def test_read_only_open_does_not_recover_or_create_snapshot(tmp_path):
    with pytest.raises(OutboxError):
        queue(tmp_path, create=False, recover_sending=False)
    assert not (tmp_path / "alerts_outbox.json").exists()
    with queue(tmp_path) as outbox:
        row = outbox.enqueue(event())
        outbox.claim(row["event_id"])
        path = outbox.path
    before = path.read_bytes()
    with queue(tmp_path, create=False, recover_sending=False) as reader:
        assert reader.get(row["event_id"])["state"] == "sending"
    assert path.read_bytes() == before


def test_legacy_capacity_summary_reports_actual_bytes_and_does_not_guess_receipt_age(tmp_path):
    with queue(tmp_path) as outbox:
        terminal(outbox)
        path = outbox.path
    body = json.loads(path.read_text(encoding="utf-8"))
    body["version"] = 2
    body.pop("audit")
    for row in body["records"]:
        for key in ("revision", "terminal_at", "retry_grants"):
            row.pop(key)
    path.write_text(json.dumps(body, indent=4), encoding="utf-8")
    far_future = lambda: datetime(2035, 1, 1, tzinfo=timezone.utc)
    with queue(tmp_path, now=far_future, create=False, recover_sending=False) as outbox:
        assert outbox.prune_plan() == []  # Unknown receive time is not event time.
        assert outbox.summary()["bytes"] == path.stat().st_size


def test_interruption_after_replace_has_consistent_record_and_audit(tmp_path, monkeypatch):
    from src.core import alert_outbox as module
    now = [datetime(2026, 10, 18, tzinfo=timezone.utc)]
    with queue(tmp_path, now=lambda: now[0]) as outbox:
        terminal(outbox)
        now[0] += timedelta(days=31)
        original = module.os.replace
        def interrupted(source, target):
            original(source, target)
            raise OSError("synthetic after replacement")
        with monkeypatch.context() as patch:
            patch.setattr(module.os, "replace", interrupted)
            with pytest.raises(OutboxError):
                outbox.prune()
    with queue(tmp_path) as restored:
        assert restored.records() == []
        assert len(restored.audit()) == 1 and restored.audit()[0]["action"] == "prune"


@pytest.mark.parametrize("kind", ["failed", "exhausted"])
def test_unresolved_terminal_states_are_never_aged_out(tmp_path, kind):
    now = [datetime(2026, 10, 18, tzinfo=timezone.utc)]
    with queue(tmp_path, now=lambda: now[0]) as outbox:
        row = outbox.enqueue(event())
        for _ in range(1 if kind == "failed" else 4):
            claim = outbox.claim(row["event_id"])
            outbox.record_failure(row["event_id"], expected_attempts=claim["attempts"],
                                  error="conflict" if kind == "failed" else "transport")
            now[0] += timedelta(minutes=5)
        now[0] += timedelta(days=100)
        assert outbox.prune() == 0
        assert outbox.get(row["event_id"])["state"] == kind


def test_repeated_prune_uuid_is_audit_replay_not_another_deletion(tmp_path):
    now = [datetime(2026, 10, 18, tzinfo=timezone.utc)]
    with queue(tmp_path, now=lambda: now[0]) as outbox:
        terminal(outbox)
        now[0] += timedelta(days=31)
        args = {"expected_digest": outbox.summary()["snapshot_digest"], "operation_id": str(uuid4()),
                "operator": "local-admin", "reason": "retention_expired"}
        assert outbox.prune(**args) == outbox.prune(**args) == 1
        assert len(outbox.audit()) == 1


def test_audit_byte_limit_fails_before_changing_the_reviewed_record(tmp_path):
    with queue(tmp_path, max_records=10, reserve_records=1, max_bytes=8192, reserve_bytes=1024) as outbox:
        rows = [terminal(outbox, received=False) for _ in range(6)]
        refused = False
        for row in rows:
            before = outbox.path.read_bytes()
            try:
                outbox.decide(**arguments(row, "resolve"))
            except OutboxError as error:
                assert error.code == "audit_capacity"
                assert outbox.path.read_bytes() == before
                refused = True
                break
        assert refused
        assert outbox.path.stat().st_size <= outbox.max_bytes


def test_hash_ignores_credentials_and_nonbusiness_fields():
    from src.core.alert_outbox import payload_hash
    row = {"elder_id": 1, "alert_type": "help", "priority": "P0", "content": "中文  "}
    assert payload_hash(row) == payload_hash({**row, "Authorization": "synthetic", "event_id": str(uuid4())})
