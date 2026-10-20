"""Audit capacity recovery uses only owned pytest files and synthetic evidence."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock
from uuid import uuid4

import pytest

from src.core.alert_outbox import OutboxError
from src.integration.outbox_admin import run
from test_alert_outbox import event, queue
from test_alert_outbox_process import environment
from test_outbox_lifecycle import arguments, terminal


def archive_args(outbox, target, **changes):
    """Pin the exact reviewed source; never silently refresh a stale digest."""
    result = dict(destination=target, expected_digest=outbox.summary()["snapshot_digest"],
                  operation_id=str(uuid4()), operator="local-admin", reason="reviewed")
    result.update(changes)
    return result


def audited(outbox):
    """A pending high risk record and a separately audited disposition."""
    pending = outbox.enqueue(event(kind="help"))
    row = terminal(outbox, received=False)
    args = arguments(row, "resolve")
    entry = outbox.decide(**args)
    return pending, args, entry


@pytest.mark.parametrize("empty", [False, True])
def test_export_is_verified_and_does_not_change_source(tmp_path, empty):
    with queue(tmp_path) as outbox:
        if not empty:
            audited(outbox)
        before, rows, audits = outbox.path.read_bytes(), outbox.records(), outbox.audit()
        target = tmp_path / "export.json"
        result = outbox.audit_export(target)
        body = json.loads(target.read_bytes())
        assert result["phase"] == "export_verified"
        assert body["audit"] == audits and body["count"] == len(audits)
        assert body["binding"] == outbox.binding and body["version"] == 1
        assert body["source_digest"] == hashlib.sha256(before).hexdigest()
        assert body["audit_digest"] == hashlib.sha256(json.dumps(audits, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        assert "content" not in body and "Authorization" not in target.read_text()
        assert outbox.path.read_bytes() == before and outbox.records() == rows


@pytest.mark.parametrize("name", ["export.json", "alerts_outbox.json", ".alerts_outbox.lock"])
def test_export_never_overwrites_existing_or_protected_target(tmp_path, name):
    with queue(tmp_path) as outbox:
        target = tmp_path / name
        if not target.exists():
            target.write_bytes(b"existing")
        previous = target.stat() if name == ".alerts_outbox.lock" else target.read_bytes()
        before = outbox.path.read_bytes()
        with pytest.raises(OutboxError):
            outbox.audit_export(target)
        if name == ".alerts_outbox.lock":
            assert target.stat().st_ino == previous.st_ino
        else:
            assert target.read_bytes() == previous
        assert outbox.path.read_bytes() == before


def test_archive_recovers_full_audit_preserving_all_business_fields_and_replay(tmp_path):
    with queue(tmp_path, max_audit_records=1) as outbox:
        pending, args, entry = audited(outbox)
        before = outbox.records()
        with pytest.raises(OutboxError, match="audit_capacity"):
            outbox.decide(**arguments(pending, "resolve"))
        options = archive_args(outbox, tmp_path / "archive.json")
        result = outbox.audit_archive(**options)
        assert result["phase"] == "archive_committed" and not result["replayed"]
        assert outbox.records() == before and outbox.summary()["audit_records"] == 0
        assert outbox.audit() == [entry]
        digest = outbox.summary()["snapshot_digest"]
        assert outbox.audit_archive(**options)["replayed"]
        assert outbox.summary()["snapshot_digest"] == digest
        assert outbox.decide(**args) == entry
        with pytest.raises(OutboxError, match="operation_conflict"):
            outbox.decide(**{**args, "operator": "other"})
        outbox.decide(**arguments(pending, "resolve"))
    with queue(tmp_path, max_audit_records=1) as restored:
        assert restored.get(pending["event_id"])["state"] == "resolved"
        assert restored.decide(**args) == entry
        assert restored.audit_archive(**options)["replayed"]


@pytest.mark.parametrize("change", [{"operator": "other"}, {"reason": "cannot_repair"}, {"destination": "other.json"}, {"expected_digest": "0" * 64}])
def test_archive_uuid_parameter_conflict(tmp_path, change):
    with queue(tmp_path) as outbox:
        audited(outbox)
        options = archive_args(outbox, tmp_path / "archive.json")
        outbox.audit_archive(**options)
        if "destination" in change:
            change = {"destination": tmp_path / change["destination"]}
        before = outbox.path.read_bytes()
        with pytest.raises(OutboxError, match="operation_conflict"):
            outbox.audit_archive(**{**options, **change})
        assert outbox.path.read_bytes() == before


@pytest.mark.parametrize("damage", ["missing", "modified", "replaced"])
def test_archived_evidence_damage_blocks_mutations_without_source_changes(tmp_path, damage):
    with queue(tmp_path) as outbox:
        pending, _, _ = audited(outbox)
        target = tmp_path / "archive.json"
        outbox.audit_archive(**archive_args(outbox, target))
        before = outbox.path.read_bytes()
        if damage == "missing":
            target.rename(tmp_path / "preserved.json")
        elif damage == "modified":
            target.write_bytes(b"{}")
        else:
            content = target.read_bytes()
            target.rename(tmp_path / "preserved.json")
            target.write_bytes(content)
        assert outbox.summary()["archive_status"] != "verified"
        for action in (lambda: outbox.decide(**arguments(pending, "resolve")),
                       lambda: outbox.claim(pending["event_id"]), lambda: outbox.enqueue(event())):
            with pytest.raises(OutboxError, match="archive_"):
                action()
        assert outbox.path.read_bytes() == before
    with pytest.raises(OutboxError, match="archive_"):
        queue(tmp_path)


def test_prepared_archive_survives_commit_failure_and_can_resume(tmp_path, monkeypatch):
    from src.core import alert_outbox as module
    with queue(tmp_path) as outbox:
        audited(outbox)
        target = tmp_path / "archive.json"
        options = archive_args(outbox, target)
        before = outbox.path.read_bytes()
        with monkeypatch.context() as patch:
            patch.setattr(module.os, "replace", Mock(side_effect=OSError("synthetic")))
            with pytest.raises(OutboxError, match="archive_commit_unconfirmed"):
                outbox.audit_archive(**options)
        assert target.exists() and outbox.path.read_bytes() == before
    with queue(tmp_path) as restored:
        assert restored.audit_status(target)["phase"] == "archive_prepared"
        assert restored.audit_archive(**options)["phase"] == "archive_committed"
        assert restored.audit_status(target)["phase"] == "archive_committed"


def test_history_chain_and_retry_grants_are_valid_after_reopen(tmp_path):
    with queue(tmp_path) as outbox:
        row = terminal(outbox, received=False)
        args = arguments(row, "retry")
        entry = outbox.decide(**args, evidence={"receipt_status": "not_found", "event_id": row["event_id"], "device_id": "device-a"})
        outbox.audit_archive(**archive_args(outbox, tmp_path / "one.json"))
        outbox.decide(**arguments(outbox.get(row["event_id"]), "resolve"))
        outbox.audit_archive(**archive_args(outbox, tmp_path / "two.json"))
        before = outbox.records()
    with queue(tmp_path) as restored:
        assert restored.records() == before
        assert restored.decide(**args) == entry
        assert restored.summary()["archive_count"] == 2
        (tmp_path / "one.json").rename(tmp_path / "missing-one.json")
        with pytest.raises(OutboxError):
            restored.prune()


@pytest.mark.parametrize("failure", ["permissions", "write", "verification", "exclusive_race"])
def test_artifact_failure_keeps_source_and_never_overwrites(tmp_path, monkeypatch, failure):
    from src.core import audit_archive as files
    with queue(tmp_path) as outbox:
        audited(outbox)
        before = outbox.path.read_bytes()
        target = tmp_path / "archive.json"
        if failure == "permissions":
            monkeypatch.setattr(files, "secure_output", Mock(side_effect=OSError("synthetic")))
        elif failure == "write":
            monkeypatch.setattr(files.os, "fsync", Mock(side_effect=OSError("synthetic")))
        elif failure == "verification":
            monkeypatch.setattr(files, "read_artifact", Mock(side_effect=OutboxError("archive_invalid")))
        else:
            original = files.os.open
            def race(path, flags, *args, **kwargs):
                if Path(path) == target and flags & os.O_EXCL:
                    target.write_bytes(b"other owner")
                return original(path, flags, *args, **kwargs)
            monkeypatch.setattr(files.os, "open", race)
        with pytest.raises(OutboxError):
            outbox.audit_archive(**archive_args(outbox, target))
        assert outbox.path.read_bytes() == before
        if failure == "exclusive_race":
            assert target.read_bytes() == b"other owner"


def test_diagnostics_remain_read_only_when_audit_full(tmp_path):
    with queue(tmp_path, max_audit_records=1) as outbox:
        audited(outbox)
        before = outbox.path.read_bytes()
        summary = outbox.summary()
        assert "audit_count_full" in summary["capacity_blocks"]
        assert summary["audit_bytes"] > 0 and summary["transition_reserve_bytes"] > 0
        assert summary["archive_status"] == "verified"
        assert outbox.path.read_bytes() == before


@pytest.mark.asyncio
async def test_cli_export_status_archive_do_not_create_network_client(tmp_path, capsys):
    with queue(tmp_path) as outbox:
        audited(outbox)
        digest = outbox.summary()["snapshot_digest"]
    base = ["--outbox", str(tmp_path / "alerts_outbox.json"), "--device-id", "device-a",
            "--elder-id", "1", "--cloud-url", "https://example.invalid"]
    client = Mock(side_effect=AssertionError("no network"))
    target = tmp_path / "archive.json"
    assert await run(base + ["audit-export", "--destination", str(tmp_path / "export.json")], client_factory=client) == 0
    assert await run(base + ["audit-archive", "--destination", str(target), "--expected-digest", digest,
                            "--operation-id", str(uuid4()), "--operator", "local-admin", "--reason", "reviewed"], client_factory=client) == 0
    assert await run(base + ["audit-status", "--destination", str(target)], client_factory=client) == 0
    client.assert_not_called()
    assert "archive_committed" in capsys.readouterr().out


@pytest.mark.parametrize("phase", ["prepared", "committed"])
def test_abrupt_child_exit_preserves_phase_and_releases_lock(tmp_path, phase):
    with queue(tmp_path) as outbox:
        audited(outbox)
        options = archive_args(outbox, tmp_path / "archive.json")
    driver = '''
import json, os, sys
from src.core.alert_outbox import AlertOutbox
q = AlertOutbox(sys.argv[1], device_id="device-a", elder_id=1, cloud_url="https://example.invalid")
options = json.loads(sys.argv[2])
if sys.argv[3] == "prepared":
    q._commit = lambda *a, **kw: os._exit(23)
q.audit_archive(**options)
os._exit(23)
'''
    options["destination"] = str(options["destination"])
    child = subprocess.run([sys.executable, "-B", "-c", driver, str(tmp_path), json.dumps(options), phase],
                           cwd=tmp_path, env=environment(tmp_path), capture_output=True, text=True, timeout=45)
    assert child.returncode == 23, child.stderr
    with queue(tmp_path) as restored:
        assert restored.audit_status(Path(options["destination"]))["phase"] == "archive_" + phase
        assert restored.audit_archive(**options)["phase"] == "archive_committed"


def test_archived_prune_uuid_and_tombstone_survive_restart(tmp_path):
    from datetime import datetime, timedelta, timezone
    now = [datetime(2026, 10, 18, tzinfo=timezone.utc)]
    with queue(tmp_path, now=lambda: now[0]) as outbox:
        item = event()
        outbox.enqueue(item)
        outbox.decide(**arguments(outbox.get(item["eventId"]), "resolve"))
        now[0] += timedelta(days=31)
        args = dict(expected_digest=outbox.summary()["snapshot_digest"], operation_id=str(uuid4()),
                    operator="local-admin", reason="retention_expired")
        assert outbox.prune(**args) == 1
        outbox.audit_archive(**archive_args(outbox, tmp_path / "archive.json"))
    with queue(tmp_path) as outbox:
        assert outbox.prune(**args) == 1
        with pytest.raises(OutboxError, match="event_retired"):
            outbox.enqueue(item)
        with pytest.raises(OutboxError, match="operation_conflict"):
            outbox.prune(**{**args, "operator": "other"})


@pytest.mark.parametrize("damage", ["replace", "modify", "binding", "corrupt", "alias"])
def test_export_refuses_untrusted_source_and_path_alias(tmp_path, damage):
    from src.core.alert_outbox import AlertOutbox
    with queue(tmp_path) as outbox:
        audited(outbox)
        target = tmp_path / "export.json"
        if damage == "replace":
            data = outbox.path.read_bytes()
            outbox.path.rename(tmp_path / "old.json")
            outbox.path.write_bytes(data)
        elif damage == "modify":
            outbox.path.write_bytes(outbox.path.read_bytes() + b" ")
        elif damage == "alias":
            os.link(outbox.path, target)
        elif damage == "corrupt":
            outbox.path.write_bytes(b"{")
        else:
            outbox.close()
            with pytest.raises(OutboxError, match="binding_mismatch"):
                AlertOutbox(tmp_path, device_id="other", elder_id=1, cloud_url="https://example.invalid")
            assert not target.exists()
            return
        before = outbox.path.read_bytes()
        with pytest.raises(OutboxError):
            outbox.audit_export(target)
        assert outbox.path.read_bytes() == before


def test_byte_full_audit_can_archive_without_changing_unresolved_records(tmp_path):
    with queue(tmp_path, max_records=10, reserve_records=1, max_bytes=8192, reserve_bytes=1024) as outbox:
        rows = [terminal(outbox, received=False) for _ in range(6)]
        for row in rows:
            try:
                outbox.decide(**arguments(row, "resolve"))
            except OutboxError as error:
                assert error.code == "audit_capacity"
                break
        else:
            pytest.fail("byte capacity must be reached")
        before = outbox.records()
        outbox.audit_archive(**archive_args(outbox, tmp_path / "archive.json"))
        assert outbox.records() == before and outbox.summary()["audit_records"] == 0
        assert outbox.path.stat().st_size <= outbox.max_bytes
        outbox.decide(**arguments(row, "resolve"))


def test_marker_and_transition_space_are_not_bypassed(tmp_path):
    with queue(tmp_path) as outbox:
        audited(outbox)
        before = outbox.path.read_bytes()
        outbox.max_bytes = len(before) + 64  # Synthetic tight configuration, no altered evidence.
        with pytest.raises(OutboxError, match="archive_marker_capacity"):
            outbox.audit_archive(**archive_args(outbox, tmp_path / "archive.json"))
        assert outbox.path.read_bytes() == before
        assert (tmp_path / "archive.json").exists()


def test_bounded_archive_index_does_not_become_unbounded_journal(tmp_path, monkeypatch):
    from src.core import audit_archive as files
    with queue(tmp_path) as outbox:
        pending, _, _ = audited(outbox)
        outbox.audit_archive(**archive_args(outbox, tmp_path / "one.json"))
        outbox.decide(**arguments(pending, "resolve"))
        before = outbox.path.read_bytes()
        monkeypatch.setattr(files, "MAX_ARCHIVES", 1)
        with pytest.raises(OutboxError, match="archive_capacity"):
            outbox.audit_archive(**archive_args(outbox, tmp_path / "two.json"))
        assert outbox.path.read_bytes() == before and not (tmp_path / "two.json").exists()


def test_archive_does_not_free_record_slots_or_reset_attempts(tmp_path):
    with queue(tmp_path, max_records=3, reserve_records=1) as outbox:
        audited(outbox)
        outbox.enqueue(event())
        outbox.audit_archive(**archive_args(outbox, tmp_path / "archive.json"))
        before = outbox.records()
        with pytest.raises(OutboxError, match="capacity"):
            outbox.enqueue(event())
        assert outbox.records() == before and len(before) == 3


def test_competing_process_cannot_export_or_archive_owned_queue(tmp_path):
    with queue(tmp_path) as outbox:
        audited(outbox)
        before = outbox.path.read_bytes()
        for command in ("audit-export", "audit-archive"):
            argv = [sys.executable, "-B", "-m", "src.integration.outbox_admin", "--outbox", str(outbox.path),
                    "--device-id", "device-a", "--elder-id", "1", "--cloud-url", "https://example.invalid",
                    command, "--destination", str(tmp_path / "archive.json")]
            if command == "audit-archive":
                argv += ["--expected-digest", outbox.summary()["snapshot_digest"], "--operation-id", str(uuid4()),
                         "--operator", "local-admin", "--reason", "reviewed"]
            child = subprocess.run(argv, cwd=tmp_path, env=environment(tmp_path), capture_output=True, text=True, timeout=45)
            assert child.returncode == 1 and "owner_busy" in child.stderr
        assert outbox.path.read_bytes() == before and not (tmp_path / "archive.json").exists()


def test_commit_completed_but_error_reported_is_replayable(tmp_path, monkeypatch):
    from src.core import alert_outbox as module
    with queue(tmp_path) as outbox:
        audited(outbox)
        options = archive_args(outbox, tmp_path / "archive.json")
        replace = module.os.replace
        def interrupted(source, target):
            replace(source, target)
            raise OSError("synthetic after commit")
        with monkeypatch.context() as patch:
            patch.setattr(module.os, "replace", interrupted)
            with pytest.raises(OutboxError, match="archive_commit_unconfirmed"):
                outbox.audit_archive(**options)
    with queue(tmp_path) as restored:
        assert restored.audit_status(options["destination"])["phase"] == "archive_committed"
        assert restored.audit_archive(**options)["replayed"]


@pytest.mark.parametrize("version", [1, 2, 3])
def test_legacy_snapshot_export_preserves_exact_source_bytes(tmp_path, version):
    with queue(tmp_path) as outbox:
        row = outbox.enqueue(event())
        path = outbox.path
    body = json.loads(path.read_bytes())
    body["version"] = version
    if version < 3:
        body.pop("audit")
        for record in body["records"]:
            for key in ("revision", "terminal_at", "retry_grants"):
                record.pop(key)
            if version == 1:
                record.pop("cloud_notification")
                record.pop("cloud_confirmation")
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    before = path.read_bytes()
    with queue(tmp_path, create=False, recover_sending=False) as outbox:
        outbox.audit_export(tmp_path / "export.json")
        assert outbox.get(row["event_id"])["event_id"] == row["event_id"]
        assert path.read_bytes() == before


def test_partial_export_from_abrupt_exit_keeps_all_source_evidence(tmp_path):
    with queue(tmp_path) as outbox:
        audited(outbox)
        before = outbox.path.read_bytes()
    driver = '''
import os, sys
from src.core.alert_outbox import AlertOutbox
from src.core import audit_archive as files
q = AlertOutbox(sys.argv[1], device_id="device-a", elder_id=1, cloud_url="https://example.invalid")
files.os.fsync = lambda *args: os._exit(24)
q.audit_export(q.directory / "partial.json")
'''
    child = subprocess.run([sys.executable, "-B", "-c", driver, str(tmp_path)], cwd=tmp_path,
                           env=environment(tmp_path), capture_output=True, text=True, timeout=45)
    assert child.returncode == 24, child.stderr
    with queue(tmp_path) as restored:
        assert restored.path.read_bytes() == before
        assert len(restored.audit()) == 1
        with pytest.raises(OutboxError, match="archive_exists"):
            restored.audit_export(tmp_path / "partial.json")


@pytest.mark.parametrize("field,value", [("source_digest", "invalid"), ("exported_at", "unknown"),
                                        ("binding", {}), ("operation_id", "unexpected"),
                                        ("previous_digest", "broken")])
def test_status_rejects_malformed_export_metadata(tmp_path, field, value):
    with queue(tmp_path) as outbox:
        target = tmp_path / "export.json"
        outbox.audit_export(target)
        before = outbox.path.read_bytes()
        body = json.loads(target.read_bytes())
        body[field] = value
        target.write_text(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        with pytest.raises(OutboxError):
            outbox.audit_status(target)
        assert outbox.path.read_bytes() == before


def test_token_shaped_operator_is_never_exported(tmp_path):
    with queue(tmp_path) as outbox:
        row = terminal(outbox, received=False)
        synthetic = ".".join(["A" * 16, "B" * 16, "C" * 16])
        before = outbox.path.read_bytes()
        with pytest.raises(OutboxError):
            outbox.decide(**arguments(row, "resolve", operator=synthetic))
        assert outbox.path.read_bytes() == before


def test_archive_rejects_windows_alternate_stream_before_file_operations(tmp_path):
    from src.core import audit_archive as files
    with pytest.raises(OutboxError, match="archive_identity"):
        files.parents(tmp_path / "alerts_outbox.json:audit")


def test_prepared_retry_flushes_artifact_before_releasing_audit_capacity(tmp_path, monkeypatch):
    from src.core import audit_archive as files
    with queue(tmp_path) as outbox:
        audited(outbox)
        target = tmp_path / "archive.json"
        options = archive_args(outbox, target)
        before = outbox.path.read_bytes()
        with monkeypatch.context() as patch:
            patch.setattr(files.os, "fsync", Mock(side_effect=OSError("synthetic failed flush")))
            with pytest.raises(OutboxError):
                outbox.audit_archive(**options)
        assert outbox.path.read_bytes() == before
        original = files.os.fsync
        with monkeypatch.context() as patch:
            patch.setattr(files.os, "fsync", Mock(side_effect=OSError("synthetic retry flush failure")))
            with pytest.raises(OutboxError, match="archive_sync_failed"):
                outbox.audit_archive(**options)
        assert outbox.path.read_bytes() == before and len(outbox.audit()) == 1
        identities = []
        def observed(descriptor):
            info = os.fstat(descriptor)
            identities.append((info.st_dev, info.st_ino))
            return original(descriptor)
        with monkeypatch.context() as patch:
            patch.setattr(files.os, "fsync", observed)
            assert outbox.audit_archive(**options)["phase"] == "archive_committed"
        info = target.stat()
        assert (info.st_dev, info.st_ino) in identities
