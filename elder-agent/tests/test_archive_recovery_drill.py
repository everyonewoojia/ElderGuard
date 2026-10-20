"""Recovery acceptance using registered, synthetic files in fresh pytest roots."""
import errno
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
from unittest.mock import AsyncMock, Mock

import pytest

from src.core import alert_outbox as queue_module
from src.core import audit_archive as files
from src.core.alert_outbox import OutboxError
from src.integration.outbox_admin import run
from test_alert_outbox import event, queue
from test_alert_outbox_process import environment
from test_audit_archive import archive_args, audited


def fingerprint(path):
    """Register identity and digest without recording synthetic event contents."""
    if not path.exists():
        return {"path": str(path), "exists": False}
    info = path.stat()
    return {"path": str(path), "exists": True, "identity": [info.st_dev, info.st_ino],
            "bytes": info.st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture
def registered(tmp_path):
    """Record intended targets before creation and initial/final file identities."""
    paths = [tmp_path / name for name in ("alerts_outbox.json", "archive.json", "export.json")]
    manifest = {"root": str(tmp_path), "synthetic_only": True,
                "before": [fingerprint(p) for p in paths]}
    registry = tmp_path / "drill-manifest.json"
    registry.write_text(json.dumps(manifest), encoding="utf-8")

    def checkpoint():
        """Pin all existing evidence before fault injection or CLI mutation."""
        manifest.setdefault("checkpoints", []).append([fingerprint(p) for p in paths])
        registry.write_text(json.dumps(manifest), encoding="utf-8")

    yield tmp_path, checkpoint
    manifest["after"] = [fingerprint(p) for p in paths]
    registry.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("boundary", ["before_create", "after_create", "fsync", "after_fsync",
                                      "after_verify", "replace", "after_replace"])
def test_each_interruption_reopens_before_retry(registered, monkeypatch, boundary):
    """An exception never decides whether a durable transaction committed."""
    root, checkpoint = registered
    target = root / "archive.json"
    with queue(root, max_audit_records=1) as outbox:
        audited(outbox)
        options = archive_args(outbox, target)
        before, rows, audits = fingerprint(outbox.path), outbox.records(), outbox.audit()
        checkpoint()
        with monkeypatch.context() as patch:
            def fail(*args, **kwargs):
                """Controlled storage exhaustion, never an actual full disk."""
                raise OSError(errno.ENOSPC, "synthetic drill")

            if boundary == "before_create":
                patch.setattr(files, "write_artifact", fail)
            elif boundary == "after_create":
                patch.setattr(files, "secure_output", fail)
            elif boundary == "fsync":
                patch.setattr(files.os, "fsync", fail)
            elif boundary == "after_fsync":
                patch.setattr(files, "read_artifact", fail)
            elif boundary == "after_verify":
                patch.setattr(outbox, "_commit", fail)
            else:
                replace = queue_module.os.replace

                def interrupted(source, destination):
                    """Optionally replace first, then lose the caller's result."""
                    if boundary == "after_replace":
                        replace(source, destination)
                    fail()

                patch.setattr(queue_module.os, "replace", interrupted)
            with pytest.raises((OutboxError, OSError)):
                outbox.audit_archive(**options)
        if boundary != "after_replace":
            assert fingerprint(outbox.path) == before
        assert not list(root.glob(".alerts-outbox-*.tmp"))
    # Closing the uncertain instance is mandatory; the same real OS lock reopens.
    with queue(root, max_audit_records=1, create=False, recover_sending=False) as reopened:
        assert reopened.records() == rows and reopened.audit() == audits
        assert reopened.binding == {"device_id": "device-a", "elder_id": 1,
                                    "cloud_origin": "https://example.invalid"}
        summary = reopened.summary()
        assert summary["audit_records"] == (0 if boundary == "after_replace" else 1)
        assert summary["archive_count"] == (1 if boundary == "after_replace" else 0)
        if boundary == "before_create":
            assert not target.exists()
        elif boundary == "after_create":
            assert target.exists()
            with pytest.raises(OutboxError):
                reopened.audit_status(target)
            assert fingerprint(reopened.path) == before
            return  # Preserve incomplete evidence; never overwrite it to retry.
        else:
            checkpoint()
            artifact_before = fingerprint(target)
            phase = "archive_committed" if boundary == "after_replace" else "archive_prepared"
            assert reopened.audit_status(target)["phase"] == phase
            assert fingerprint(target) == artifact_before
        result = reopened.audit_archive(**options)
        assert result["replayed"] == (boundary == "after_replace")
        assert reopened.summary()["audit_records"] == 0
        assert reopened.summary()["archive_count"] == 1
        assert reopened.audit() == audits and reopened.records() == rows
        committed = fingerprint(reopened.path)
        assert reopened.audit_archive(**options)["replayed"]
        assert fingerprint(reopened.path) == committed
        assert json.loads(reopened.path.read_bytes())["version"] == 4


@pytest.mark.parametrize("damage", ["missing_field", "wrong_type", "extra_field", "digest", "unreadable"])
def test_prepared_damage_refuses_commit_and_preserves_active_audit(registered, monkeypatch, damage):
    """Readable JSON and a well-shaped digest do not establish trusted evidence."""
    root, checkpoint = registered
    with queue(root) as outbox:
        audited(outbox)
        target = root / "archive.json"
        options = archive_args(outbox, target)
        files.write_artifact(target, outbox._artifact(operation_id=options["operation_id"],
                             operator=options["operator"], reason=options["reason"]), source_check=outbox._check)
        before = fingerprint(outbox.path)
        checkpoint()
        if damage == "unreadable":
            original = Path.open

            def denied(path, *args, **kwargs):
                """Simulate file access denial without changing real account ACLs."""
                if path == target:
                    raise PermissionError("synthetic denial")
                return original(path, *args, **kwargs)

            monkeypatch.setattr(Path, "open", denied)
        else:
            body = json.loads(target.read_bytes())
            if damage == "missing_field":
                body.pop("source_digest")
            elif damage == "wrong_type":
                body["count"] = True
            elif damage == "extra_field":
                body["Authorization"] = "synthetic forbidden field"
            else:
                body["audit_digest"] = "0" * 64
            target.write_bytes(files.encode(body))
        with pytest.raises(OutboxError):
            outbox.audit_status(target)
        with pytest.raises(OutboxError):
            outbox.audit_archive(**options)
        assert fingerprint(outbox.path) == before
        assert outbox.summary()["audit_records"] == 1
        assert outbox.summary()["archive_count"] == 0
        monkeypatch.undo()


def test_export_write_failure_and_success_keep_source_identity_and_redact(registered, monkeypatch):
    """No source replacement or secret-bearing event payload enters an export."""
    root, checkpoint = registered
    with queue(root) as outbox:
        audited(outbox)
        outbox.enqueue(event(content="synthetic-private-content", priority="P0"))
        before = fingerprint(outbox.path)
        checkpoint()
        with monkeypatch.context() as patch:
            patch.setattr(files.os, "fsync", lambda descriptor: (_ for _ in ()).throw(OSError(errno.ENOSPC, "synthetic")))
            with pytest.raises(OutboxError, match="archive_write_failed"):
                outbox.audit_export(root / "export.json")
        assert fingerprint(outbox.path) == before
        with pytest.raises(OutboxError, match="archive_exists"):
            outbox.audit_export(root / "export.json")
        outbox.audit_export(root / "archive.json")
        raw = (root / "archive.json").read_text(encoding="utf-8")
        assert all(value not in raw for value in ("synthetic-private-content", "Authorization", "JWT", "password", "Traceback"))
        assert fingerprint(outbox.path) == before


def test_cli_complete_flow_replay_conflict_interruption_and_lock_release(registered, monkeypatch):
    """Run the actual module entry point in independent Python processes."""
    root, checkpoint = registered
    with queue(root) as outbox:
        audited(outbox)
        digest = outbox.summary()["snapshot_digest"]
        rows, audits = outbox.records(), outbox.audit()
        before = fingerprint(outbox.path)
    target = root / "archive.json"
    operation = str(uuid4())
    base = [sys.executable, "-B", "-m", "src.integration.outbox_admin", "--outbox",
            str(root / "alerts_outbox.json"), "--device-id", "device-a", "--elder-id", "1",
            "--cloud-url", "https://example.invalid"]
    journal = []

    def cli(command, code=0):
        """Pin inputs and register exit/output as synthetic drill evidence."""
        checkpoint()
        child = subprocess.run(base + command, cwd=root, env=environment(root),
                               capture_output=True, text=True, timeout=60)
        journal.append({"arguments": command, "exit": child.returncode,
                        "stdout": child.stdout, "stderr": child.stderr})
        (root / "cli-results.json").write_text(json.dumps(journal), encoding="utf-8")
        assert child.returncode == code, child.stderr
        # Every exit (including refusal) must release ownership.
        with queue(root, create=False, recover_sending=False) as probe:
            assert probe.records() == rows
        return json.loads(child.stdout) if code == 0 else child.stderr

    assert cli(["list"])["audit_records"] == 1
    assert "archive_missing" in cli(["audit-status", "--destination", str(target)], 1)
    assert cli(["audit-export", "--destination", str(root / "export.json")])["phase"] == "export_verified"
    assert fingerprint(root / "alerts_outbox.json") == before
    options = ["audit-archive", "--destination", str(target), "--expected-digest", digest,
               "--operation-id", operation, "--operator", "local-admin", "--reason", "reviewed"]
    # Prepare in a separately owned session, interrupt before snapshot commit.
    with queue(root) as outbox:
        with monkeypatch.context() as patch:
            patch.setattr(outbox, "_commit", lambda *a, **kw: (_ for _ in ()).throw(OSError("synthetic")))
            with pytest.raises(OutboxError, match="archive_commit_unconfirmed"):
                outbox.audit_archive(destination=target, expected_digest=digest,
                                     operation_id=operation, operator="local-admin", reason="reviewed")
    assert cli(["audit-status", "--destination", str(target)])["phase"] == "archive_prepared"
    assert cli(options)["phase"] == "archive_committed"
    assert cli(["audit-status", "--destination", str(target)])["phase"] == "archive_committed"
    assert cli(options)["replayed"]
    conflict = options.copy()
    conflict[conflict.index("local-admin")] = "other-admin"
    assert "operation_conflict" in cli(conflict, 1)
    final = cli(["list"])
    assert final["audit_records"] == 0 and final["archive_count"] == 1
    with queue(root) as outbox:
        assert outbox.audit() == audits and outbox.records() == rows
        body = json.loads(outbox.path.read_bytes())
        assert body["version"] == 4 and body["archives"][0]["digest"] == files.read_artifact(target)[1]["digest"]


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_exact_single_artifact_byte_boundary(registered, offset):
    """Exercise the real 128 KiB writer bound, independently of audit semantics."""
    root, checkpoint = registered
    # The outer artifact schema is valid; the padded row is deliberately not a
    # domain audit. Storage verification must never substitute for queue trust.
    with queue(root) as outbox:
        body = outbox._artifact()
        body.update(audit=[{"synthetic_padding": ""}], count=1)
        body["audit_digest"] = files.digest(body["audit"])
        body["audit"][0]["synthetic_padding"] = "x" * (files.MAX_ARTIFACT_BYTES + offset - len(files.encode(body)))
        body["audit_digest"] = files.digest(body["audit"])
        target = root / "archive.json"
        checkpoint()
        if offset > 0:
            with pytest.raises(OutboxError, match="archive_capacity"):
                files.write_artifact(target, body, source_check=outbox._check)
            assert not target.exists()
        else:
            ref = files.write_artifact(target, body, source_check=outbox._check)
            assert ref["bytes"] == target.stat().st_size == files.MAX_ARTIFACT_BYTES + offset
            with pytest.raises(OutboxError):
                outbox.audit_status(target)


@pytest.mark.parametrize("limit", ["index", "history"])
def test_real_capacity_constants_with_controlled_artifact_io(registered, monkeypatch, limit):
    """Real 32/2 MiB arithmetic; mock artifact IO to avoid quadratic ACL launches."""
    root, checkpoint = registered
    with queue(root) as outbox:
        audited(outbox)
        options = archive_args(outbox, root / "archive.json")
        before = fingerprint(outbox.path)
        audits = outbox.audit()
        checkpoint()
        # Real history/identity validation is covered by other tests; this case
        # isolates capacity arithmetic and is not evidence of a valid disk chain.
        refs = [{"operation_id": str(uuid4()), "digest": "0" * 64, "bytes": 65536,
                 "path": str(root / f"synthetic-ref-{index}.json")}
                for index in range(files.MAX_ARCHIVES if limit == "index" else 31)]
        monkeypatch.setattr(outbox, "_archives", refs)
        monkeypatch.setattr(outbox, "_all_audit", lambda: audits)
        if limit == "history":
            candidate = {"bytes": 65537}
            monkeypatch.setattr(files, "write_artifact", lambda *a, **kw: candidate)
        with pytest.raises(OutboxError, match="archive_capacity"):
            outbox.audit_archive(**options)
        assert fingerprint(outbox.path) == before
        assert outbox._audit == audits and outbox._archives == refs
        assert not (root / "archive.json").exists()


def test_sync_handle_identity_failure_closes_descriptor(registered, monkeypatch):
    """Even an identity refusal inside the open-handle scope closes the file."""
    root, checkpoint = registered
    with queue(root) as outbox:
        audited(outbox)
        target = root / "archive.json"
        ref = files.write_artifact(target, outbox._artifact(), source_check=outbox._check)
        before = fingerprint(target)
        checkpoint()
        original, handles = Path.open, []

        def tracked(path, *args, **kwargs):
            """Track only the synthetic artifact's writable sync handle."""
            stream = original(path, *args, **kwargs)
            if path == target and args and args[0] == "r+b":
                handles.append(stream)
            return stream

        original_stat = files.os.fstat

        def wrong_identity(descriptor):
            """Keep real stat values except for the controlled inode mismatch."""
            info = original_stat(descriptor)
            return type("SyntheticStat", (), {"st_dev": info.st_dev, "st_ino": info.st_ino + 1})()

        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", tracked)
            patch.setattr(files.os, "fstat", wrong_identity)
            with pytest.raises(OutboxError, match="archive_identity"):
                files.sync_artifact(ref)
        assert handles and all(handle.closed for handle in handles)
        assert fingerprint(target) == before


@pytest.mark.parametrize("limit", ["index", "history"])
def test_real_files_reach_bounded_history_limits(registered, monkeypatch, limit):
    """Real bytes, identities, digests and schemas; ACL checks are isolated out."""
    root, checkpoint = registered
    # Only this arithmetic/chain stress case skips expensive platform ACL tools.
    # Other drill cases run the actual Windows DACL creation and verification.
    monkeypatch.setattr(files, "secure_output", lambda *args: None)
    monkeypatch.setattr(files, "_permissions", lambda *args: None)
    with queue(root) as outbox:
        audited(outbox)
        template = outbox._artifact(operation_id=str(uuid4()), operator="local-admin", reason="reviewed")
        entry = outbox.audit()[0]
        references, total = [], 0
        for index in range(33):
            target = root / f"chain-{index:02}.json"
            # Register every intended file before creation, including absent identity.
            registration = root / f"chain-{index:02}-manifest.json"
            registration.write_text(json.dumps({"before": fingerprint(target)}), encoding="utf-8")
            body = dict(template, operation_id=str(uuid4()),
                        previous_digest=references[-1]["digest"] if references else None)
            body["audit"] = [dict(entry, operation_id=str(uuid4()), event_id=str(uuid4()), operator="a" * 64)
                             for _ in range(1 if limit == "index" else 240)]
            body["count"] = len(body["audit"])
            body["audit_digest"] = files.digest(body["audit"])
            ref = files.write_artifact(target, body, source_check=outbox._check)
            assert ref["bytes"] <= 128 * 1024
            registration.write_text(json.dumps({"before": {"path": str(target), "exists": False},
                                                "after": fingerprint(target)}), encoding="utf-8")
            references.append(ref)
            total += ref["bytes"]
            if (limit == "index" and len(references) == 33) or (limit == "history" and total > 2 * 1024 * 1024):
                break
        checkpoint()
        kwargs = dict(binding=outbox.binding,
                      validate_audit=lambda rows: outbox._validate_audit(rows, capacity=False),
                      timestamp=outbox._timestamp)
        prior = references[:-1]
        verified = files.history(prior, **kwargs)
        assert len(verified) == sum(ref["count"] for ref in prior)
        before = [fingerprint(Path(ref["path"])) for ref in references]
        with pytest.raises(OutboxError, match="archive_index_invalid" if limit == "index" else "archive_capacity"):
            files.history(references, **kwargs)
        assert [fingerprint(Path(ref["path"])) for ref in references] == before
        (root / "capacity-results.json").write_text(json.dumps({
            "limit": limit, "accepted_count": len(prior), "accepted_bytes": sum(ref["bytes"] for ref in prior),
            "rejected_count": len(references), "rejected_bytes": total,
            "largest_file": max(ref["bytes"] for ref in references), "acl_mocked": True}), encoding="utf-8")


@pytest.mark.parametrize("closing", ["client", "queue"])
async def test_cli_close_errors_release_ownership_without_leaking_details(registered, monkeypatch, capsys, closing):
    """Probe the real lock after client failure or a post-release queue error."""
    root, checkpoint = registered
    with queue(root) as outbox:
        pending, _, _ = audited(outbox)
        before = fingerprint(outbox.path)
    checkpoint()
    base = ["--outbox", str(root / "alerts_outbox.json"), "--device-id", "device-a",
            "--elder-id", "1", "--cloud-url", "https://example.invalid"]
    fake = Mock(base_url="https://example.invalid", fetch_receipt=AsyncMock(return_value={"receipt_status": "not_found"}),
                aclose=AsyncMock(side_effect=RuntimeError("synthetic-private-close-detail")))
    with monkeypatch.context() as patch:
        if closing == "client":
            code = await run(base + ["check", "--event-id", pending["event_id"]], client_factory=lambda **kw: fake)
        else:
            close = queue_module.AlertOutbox.close

            def after_release(instance):
                """The release completes but its caller observes an error."""
                close(instance)
                raise OSError("synthetic-private-close-detail")

            patch.setattr(queue_module.AlertOutbox, "close", after_release)
            code = await run(base + ["list"])
    assert code == 1
    output = capsys.readouterr()
    assert "synthetic-private-close-detail" not in output.out + output.err
    with queue(root, create=False, recover_sending=False) as probe:
        assert fingerprint(probe.path) == before
