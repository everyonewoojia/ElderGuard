"""Durable alert outbox. HTTP and the production sender stay outside this module.

One OS-locked owner per data directory. Atomic replacement protects the previous
snapshot from ordinary write failures; it is not protection against a malicious
process running as the same OS user or a guarantee against sudden power loss.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading
from urllib.parse import urlsplit
from uuid import UUID, uuid4


class OutboxError(ValueError):
    """A bounded diagnostic category, never raw event or exception contents."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Alert outbox: {code}")


def payload_hash(record: dict) -> str:
    """Exactly the existing cloud admission JSON/SHA-256 contract, no auth data."""
    normalized = {key: record[key] for key in ("elder_id", "alert_type", "priority", "content")}
    return hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _identity(path: Path, directory: bool = False) -> tuple:
    """Reject links/reparse points and require a reliable filesystem identity."""
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or not info.st_ino
            or (not directory and info.st_nlink != 1)
            or (directory and not stat.S_ISDIR(info.st_mode))
            or (not directory and not stat.S_ISREG(info.st_mode))):
        raise OutboxError("identity")
    return info.st_dev, info.st_ino


def _owned(path: Path) -> None:
    """Fail closed unless the existing object belongs to this OS account."""
    if os.name == "nt":
        executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        script = ("$ErrorActionPreference='Stop'; "
                  "$actual=(Get-Acl -LiteralPath $env:ELDER_OUTBOX_OWNER_PATH).GetOwner([System.Security.Principal.SecurityIdentifier]).Value; "
                  "$expected=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
                  "if ($actual -ne $expected) { exit 3 }")
        environment = os.environ.copy()
        environment["ELDER_OUTBOX_OWNER_PATH"] = str(path)
        result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-Command", script],
                                env=environment, capture_output=True, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise OutboxError("ownership")
    elif hasattr(os, "getuid"):
        if path.lstat().st_uid != os.getuid():
            raise OutboxError("ownership")
    else:
        raise OutboxError("ownership_unsupported")


def _cloud_origin(url: str) -> str:
    """Normalize a non-secret endpoint, including an optional API base path."""
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or (parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"))
            or re.search(r"[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}", url)):
        raise OutboxError("binding")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    suffix = f":{port}" if port and port != (443 if parsed.scheme == "https" else 80) else ""
    return f"{parsed.scheme}://{host}{suffix}{parsed.path.rstrip('/')}"


class AlertOutbox:
    """Bounded, exclusively owned JSON snapshot; Reporter owns automatic sending.

    Only ``simulate_attempt`` accepts an injected fake sender. It can never mark
    an alert received. The production worker uses claim and checked transitions.
    """

    MAX_ATTEMPTS = 4  # Original Reporter: initial attempt plus three retries.
    RETENTION_DAYS = 30
    META_KEYS = {"revision", "terminal_at", "retry_grants"}
    RECORD_KEYS = frozenset(("event_id", "device_id", "elder_id", "cloud_origin",
                             "alert_type", "priority", "content", "occurred_at",
                             "state", "attempts", "next_retry_at", "last_error_code",
                             "http_status", "cloud_alert_id", "cloud_notification", "cloud_confirmation",
                             "revision", "terminal_at", "retry_grants"))
    AUDIT_KEYS = {"operation_id", "event_id", "action", "before_state", "after_state", "operator", "reason",
                  "at", "expected_revision", "before_attempts", "before_error", "before_http_status", "cloud_alert_id", "snapshot_digest"}
    REASONS = {"reviewed", "credentials_repaired", "cannot_repair", "retention_expired"}
    ERROR_CODES = {None, "interrupted", "simulation_only", "simulation_failure", "authentication",
                   "transport", "server_error", "invalid_response", "conflict", "invalid_payload", "request_rejected"}
    NOTIFICATIONS = {"pending", "sending", "retry_wait", "attempt_returned", "attempt_failed", "unknown", "cancelled"}

    def __init__(self, data_dir, *, device_id: str, elder_id: int, cloud_url: str,
                 max_records: int = 1000, reserve_records: int = 100,
                 max_bytes: int = 2 * 1024 * 1024, reserve_bytes: int = 256 * 1024,
                 now=None, create=True, recover_sending=True, max_audit_records=256):
        """Acquire ownership before loading; never recreate a damaged snapshot."""
        self._fd = None
        self._locked = False
        self._closed = False
        self._failed = False
        self._mutex = threading.RLock()
        self._simulation_lock = asyncio.Lock()
        self._records = []
        self._audit = []
        self._archives = []
        self._queue_identity = None
        self._queue_digest = None
        self._now = now or (lambda: datetime.now(timezone.utc))
        try:
            if (not isinstance(device_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", device_id)
                    or type(elder_id) is not int or elder_id <= 0):
                raise OutboxError("binding")
            self._binding = {"device_id": device_id, "elder_id": elder_id,
                             "cloud_origin": _cloud_origin(cloud_url)}
            if (any(type(value) is not int for value in (max_records, reserve_records, max_bytes, reserve_bytes))
                    or not 0 < reserve_records < max_records
                    or not 0 < reserve_bytes < max_bytes):
                raise OutboxError("capacity_configuration")
            self.max_records, self.reserve_records = max_records, reserve_records
            self.max_bytes, self.reserve_bytes = max_bytes, reserve_bytes
            if type(max_audit_records) is not int or not 1 <= max_audit_records <= 256:
                raise OutboxError("capacity_configuration")
            self.max_audit_records = max_audit_records
            self.directory = Path(os.path.abspath(data_dir))
            self._directory_identity = _identity(self.directory, True)
            self._ancestors = [(p, _identity(p, True)) for p in self.directory.parents]
            if self.directory.resolve() != self.directory:
                raise OutboxError("identity")
            _owned(self.directory)
            self.path = self.directory / "alerts_outbox.json"
            self._lock_path = self.directory / ".alerts_outbox.lock"
            if not create and not os.path.lexists(self.path):
                raise OutboxError("not_found")
            self._acquire()
            self._check()
            if os.path.lexists(self.path):
                self._queue_identity = _identity(self.path)
                _owned(self.path)
                self._load()
                recovered = deepcopy(self._records)
                for record in recovered:
                    if recover_sending and record["state"] == "sending":
                        record.update(state="exhausted" if record["attempts"] >= self.MAX_ATTEMPTS + record["retry_grants"] else "pending",
                                      next_retry_at=None, last_error_code="interrupted")
                if recovered != self._records:
                    self._commit(recovered)
            else:
                self._commit([])
        except BaseException as exc:
            self.close()
            if isinstance(exc, (OutboxError, KeyboardInterrupt, SystemExit)):
                raise
            raise OutboxError("initialization") from None

    @property
    def binding(self) -> dict:
        """Return public configuration without exposing the authoritative binding."""
        return dict(self._binding)

    def _acquire(self) -> None:
        """Use a kernel lock, never lockfile existence or stale-file deletion."""
        try:
            self._fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(self._fd, b"1")
        except FileExistsError:
            _identity(self._lock_path)
            _owned(self._lock_path)
            self._fd = os.open(self._lock_path, os.O_RDWR)
        self._lock_identity = _identity(self._lock_path)
        opened = os.fstat(self._fd)
        if (opened.st_dev, opened.st_ino) != self._lock_identity:
            raise OutboxError("identity")
        os.lseek(self._fd, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise OutboxError("owner_busy") from None
        self._locked = True

    def _check(self) -> None:
        """Recheck owned objects, refusing moved/replaced objects without cleanup."""
        if self._closed or not self._locked:
            raise OutboxError("closed")
        if self._failed:
            raise OutboxError("write_failed")
        try:
            if (_identity(self.directory, True) != self._directory_identity
                    or any(_identity(p, True) != identity for p, identity in self._ancestors)
                    or self.directory.resolve() != self.directory
                    or _identity(self._lock_path) != self._lock_identity):
                raise OutboxError("identity")
            info = os.fstat(self._fd)
            if (info.st_dev, info.st_ino) != self._lock_identity:
                raise OutboxError("identity")
            if self._queue_identity is None:
                # Before first load an existing file is inspected by _load.
                return
            if _identity(self.path) != self._queue_identity:
                raise OutboxError("identity")
            if self._queue_digest is not None:
                with self.path.open("rb") as stream:
                    current = os.fstat(stream.fileno())
                    raw = stream.read(self.max_bytes + 1)
                if ((current.st_dev, current.st_ino) != self._queue_identity
                        or len(raw) > self.max_bytes
                        or hashlib.sha256(raw).digest() != self._queue_digest):
                    raise OutboxError("identity")
        except OSError:
            raise OutboxError("identity") from None

    @staticmethod
    def _timestamp(value) -> datetime:
        """Require an explicit timezone and bounded ISO8601 text."""
        if not isinstance(value, str) or len(value) > 64:
            raise OutboxError("invalid_event")
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                raise ValueError
            return parsed
        except ValueError:
            raise OutboxError("invalid_event") from None

    @staticmethod
    def _content(value) -> str:
        """Never serialize arbitrary payloads, bearer tokens or JWT-shaped text."""
        if (not isinstance(value, str) or len(value.encode("utf-8")) > 4096
                or re.search(r"(?i)\bbearer\s|[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}", value)):
            raise OutboxError("invalid_event")
        return value

    def _from_event(self, event: dict) -> dict:
        """Extract only the minimal business whitelist from an existing event."""
        try:
            identity = event["eventId"]
            if not isinstance(identity, str) or str(UUID(identity)) != identity:
                raise ValueError
            kind, priority = event["type"], event["priority"]
            if kind not in ("fall", "help", "abnormal") or priority not in ("P0", "P1", "P2"):
                raise ValueError
            occurred_at = event["timestamp"]
            self._timestamp(occurred_at)
            payload = event.get("payload", {})
            if not isinstance(payload, dict):
                raise ValueError
            content = self._content(payload.get("content", {"help": "主动求助", "fall": "疑似跌倒", "abnormal": "异常情况"}[kind]))
            return {"event_id": identity, **self.binding, "alert_type": kind,
                    "priority": priority, "content": content, "occurred_at": occurred_at,
                    "state": "pending", "attempts": 0, "next_retry_at": None,
                    "last_error_code": None, "http_status": None, "cloud_alert_id": None,
                    "cloud_notification": None, "cloud_confirmation": None,
                    "revision": 0, "terminal_at": None, "retry_grants": 0}
        except (KeyError, TypeError, ValueError, AttributeError):
            raise OutboxError("invalid_event") from None

    def _encode(self, records: list, audit=None, archives=None) -> bytes:
        """Count actual UTF-8 bytes, not a character-count approximation."""
        references = self._archives if archives is None else archives
        body = {"version": 4 if references else 3, "binding": self.binding, "records": records,
                "audit": self._audit if audit is None else audit}
        if references:
            body["archives"] = references
        return json.dumps(body,
                          ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")

    def _load(self) -> None:
        """Validate the entire bounded snapshot before permitting mutations."""
        try:
            self._check()
            if self.path.stat().st_size > self.max_bytes:
                raise ValueError
            with self.path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != self._queue_identity:
                    raise OutboxError("identity")
                raw = stream.read(self.max_bytes + 1)
                body = json.loads(raw)
            self._queue_digest = hashlib.sha256(raw).digest()
            self._check()
            if (not isinstance(body, dict) or type(body.get("version")) is not int or body["version"] not in (1, 2, 3, 4)
                    or set(body) != ({"version", "binding", "records", "audit", "archives"} if body["version"] == 4 else
                                     {"version", "binding", "records", "audit"} if body["version"] == 3 else {"version", "binding", "records"})):
                raise ValueError
            if body["binding"] != self.binding:
                raise OutboxError("binding_mismatch")
            records = body["records"]
            if not isinstance(records, list) or len(records) > self.max_records:
                raise ValueError
            seen = set()
            audits = body.get("audit", [])
            self._validate_audit(audits)
            self._audit = audits
            self._archives = body.get("archives", [])
            audits = self._all_audit()
            for record in records:
                if body["version"] == 1:
                    if not isinstance(record, dict) or set(record) != self.RECORD_KEYS - self.META_KEYS - {"cloud_notification", "cloud_confirmation"}:
                        raise ValueError
                    record.update(cloud_notification=None, cloud_confirmation=None)
                if body["version"] in (1, 2):
                    if not isinstance(record, dict) or set(record) != self.RECORD_KEYS - self.META_KEYS:
                        raise ValueError
                    record.update(revision=0, terminal_at=None, retry_grants=0)
                if not isinstance(record, dict) or set(record) != self.RECORD_KEYS:
                    raise ValueError
                if any(record[k] != v or type(record[k]) is not type(v) for k, v in self.binding.items()):
                    raise ValueError
                self._from_event({"eventId": record["event_id"], "type": record["alert_type"],
                                  "priority": record["priority"], "timestamp": record["occurred_at"],
                                  "payload": {"content": record["content"]}})
                if (record["event_id"] in seen or record["state"] not in ("pending", "sending", "blocked", "exhausted", "received", "failed", "resolved")
                        or type(record["revision"]) is not int or record["revision"] < 0
                        or type(record["retry_grants"]) is not int or not 0 <= record["retry_grants"] <= self.max_audit_records
                        or type(record["attempts"]) is not int or not 0 <= record["attempts"] <= self.MAX_ATTEMPTS + record["retry_grants"]
                        or record["last_error_code"] not in self.ERROR_CODES
                        or (record["http_status"] is not None and (type(record["http_status"]) is not int or not 100 <= record["http_status"] <= 599))
                        or (record["state"] == "exhausted" and record["attempts"] != self.MAX_ATTEMPTS + record["retry_grants"])
                        or (record["state"] == "sending" and record["attempts"] == 0)):
                    raise ValueError
                if record["state"] == "received":
                    if (type(record["cloud_alert_id"]) is not int or record["cloud_alert_id"] <= 0
                            or record["cloud_notification"] not in self.NOTIFICATIONS
                            or record["cloud_confirmation"] != "unconfirmed"
                            or record["http_status"] not in (200, 201)
                            or (record["attempts"] == 0 and not any(a["event_id"] == record["event_id"] and a["after_state"] == "received" for a in audits))):
                        raise ValueError
                elif any(record[key] is not None for key in ("cloud_alert_id", "cloud_notification", "cloud_confirmation")):
                    raise ValueError
                if record["next_retry_at"] is not None:
                    self._timestamp(record["next_retry_at"])
                if record["terminal_at"] is not None:
                    self._timestamp(record["terminal_at"])
                    if record["state"] not in ("received", "resolved"):
                        raise ValueError
                if record["retry_grants"] != sum(a["event_id"] == record["event_id"] and a["action"] == "retry"
                                                 and a["after_state"] == "pending" for a in audits):
                    raise ValueError
                seen.add(record["event_id"])
            self._records = records
        except (OutboxError, ValueError, TypeError, KeyError, OSError) as error:
            # Binding/identity diagnostics are distinct; contents never escape.
            if isinstance(error, OutboxError) and (error.code in ("identity", "binding_mismatch") or error.code.startswith("archive_")):
                raise
            raise OutboxError("invalid_file") from None

    def _commit(self, records: list, audit=None, archives=None) -> None:
        """Atomic snapshot replacement, updating memory only after success."""
        self._check()
        self._all_audit()
        old = {r["event_id"]: r for r in self._records}
        for record in records:
            previous = old.get(record["event_id"])
            if previous is not None and previous != record:
                record["revision"] = previous["revision"] + 1
        audit = self._audit if audit is None else audit
        self._validate_audit(audit)
        references = self._archives if archives is None else archives
        if archives is not None:
            self._history(references)
        encoded = self._encode(records, audit, references)
        if len(records) > self.max_records or len(encoded) > self.max_bytes:
            raise OutboxError("capacity")
        temporary = None
        identity = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", prefix=".alerts-outbox-", suffix=".tmp",
                                             dir=self.directory, delete=False) as stream:
                temporary = Path(stream.name)
                identity = _identity(temporary)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            self._check()
            if _identity(temporary) != identity:
                raise OutboxError("identity")
            if self._queue_identity is None and os.path.lexists(self.path):
                raise OutboxError("identity")
            # Recheck evidence at the final replacement boundary as well.
            self._history(references)
            self._check()
            os.replace(temporary, self.path)
            self._queue_identity = identity
            self._queue_digest = hashlib.sha256(encoded).digest()
            if _identity(self.path) != identity:
                raise OutboxError("identity")
            self._records = records
            self._audit = audit
            self._archives = references
        except OSError:
            self._failed = True
            raise OutboxError("write_failed") from None
        finally:
            if temporary is not None:
                try:
                    if (_identity(self.directory, True) == self._directory_identity
                            and temporary.exists() and _identity(temporary) == identity):
                        temporary.unlink()  # Only our just-created, verified file.
                except (OSError, OutboxError):
                    pass  # Never broaden cleanup after identity uncertainty.

    def enqueue(self, event: dict) -> dict:
        """Persist once; duplicate IDs must have identical immutable fields."""
        with self._mutex:
            self._check()
            record = self._from_event(event)
            for old in self._records:
                if old["event_id"] == record["event_id"]:
                    immutable = ("event_id", "device_id", "elder_id", "cloud_origin", "alert_type", "priority", "content", "occurred_at")
                    if any(old[k] != record[k] for k in immutable):
                        raise OutboxError("event_conflict")
                    return deepcopy(old)
            if any(a["event_id"] == record["event_id"] and a["action"] == "prune" for a in self._all_audit()):
                raise OutboxError("event_retired")
            if self.prune_plan():
                self.prune()
            urgent = record["priority"] == "P0" or record["alert_type"] == "help"
            proposed = self._records + [record]
            count_limit = self.max_records if urgent else self.max_records - self.reserve_records
            byte_limit = self.max_bytes if urgent else self.max_bytes - self.reserve_bytes
            # Reserve state-transition space too, so full queues can still retry.
            if len(proposed) > count_limit or len(self._encode(proposed)) + 128 * len(proposed) > byte_limit:
                raise OutboxError("capacity")
            self._commit(proposed)
            return deepcopy(record)

    def records(self) -> list:
        """Return detached records; callers cannot mutate the live snapshot."""
        with self._mutex:
            self._check()
            return deepcopy(self._records)

    def get(self, event_id: str):
        """Find a detached local record by stable identity."""
        return next((r for r in self.records() if r["event_id"] == event_id), None)

    def claim(self, event_id: str):
        """Persist one attempt before the caller can send; keep the stable ID."""
        with self._mutex:
            self._check()
            records = deepcopy(self._records)
            for record in records:
                if record["event_id"] != event_id:
                    continue
                if (record["state"] != "pending" or record["attempts"] >= self.MAX_ATTEMPTS + record["retry_grants"]
                        or (record["next_retry_at"] and self._timestamp(record["next_retry_at"]) > self._now())):
                    return None
                record.update(state="sending", attempts=record["attempts"] + 1)
                self._commit(records)
                return deepcopy(record)
            return None

    def _validate_audit(self, audits: list, *, capacity=True) -> None:
        """Bound audit size and whitelist every field before encoding or loading."""
        if (type(audits) is not list or (capacity and (len(audits) > self.max_audit_records
                or len(json.dumps(audits, ensure_ascii=False).encode("utf-8")) > min(65536, self.max_bytes // 4)))):
            raise OutboxError("audit_capacity")
        seen = set()
        for row in audits:
            if type(row) is not dict or set(row) != self.AUDIT_KEYS:
                raise OutboxError("invalid_file")
            try:
                self._content(row["operator"])
                if (str(UUID(row["operation_id"])) != row["operation_id"] or str(UUID(row["event_id"])) != row["event_id"]
                        or row["action"] not in ("retry", "resolve", "reconcile", "prune")
                        or row["reason"] not in self.REASONS
                        or not isinstance(row["operator"], str) or re.fullmatch(r"[A-Za-z0-9_@.\\-]{1,64}", row["operator"]) is None
                        or row["before_state"] not in ("pending", "sending", "received", "blocked", "exhausted", "failed", "resolved")
                        or row["after_state"] not in ("pending", "received", "resolved", "pruned")
                        or type(row["expected_revision"]) is not int or row["expected_revision"] < 0
                        or type(row["before_attempts"]) is not int or row["before_attempts"] < 0
                        or row["before_error"] not in self.ERROR_CODES
                        or (row["before_http_status"] is not None and (type(row["before_http_status"]) is not int or not 100 <= row["before_http_status"] <= 599))
                        or (row["cloud_alert_id"] is not None and (type(row["cloud_alert_id"]) is not int or row["cloud_alert_id"] <= 0))
                        or re.fullmatch(r"[0-9a-f]{64}", row["snapshot_digest"]) is None
                        or (row["operation_id"], row["event_id"]) in seen):
                    raise ValueError
                self._timestamp(row["at"])
            except (ValueError, TypeError, KeyError, AttributeError):
                raise OutboxError("invalid_file") from None
            seen.add((row["operation_id"], row["event_id"]))

    def audit(self) -> list:
        """Read verified active and bounded archived evidence for UUID replay."""
        with self._mutex:
            self._check()
            return deepcopy(self._all_audit())

    def _history(self, references=None) -> list:
        """All historical artifacts are required, including their original identity."""
        from src.core import audit_archive as files
        try:
            return files.history(self._archives if references is None else references, binding=self.binding,
                                 validate_audit=lambda rows: self._validate_audit(rows, capacity=False), timestamp=self._timestamp)
        except OutboxError as error:
            if error.code.startswith("archive_"):
                raise
            raise OutboxError("archive_invalid") from None

    def _all_audit(self) -> list:
        """Retain historical grant, tombstone and idempotence semantics."""
        rows = self._history() + self._audit
        self._validate_audit(rows, capacity=False)
        if {r["operation_id"] for r in self._archives}.intersection(r["operation_id"] for r in rows):
            raise OutboxError("archive_index_invalid")
        return rows

    def _artifact_target(self, destination) -> Path:
        """Only an explicit canonical path; never overwrite a protected object."""
        from src.core import audit_archive as files
        path = Path(destination)
        files.parents(path)
        if path in (self.path, self._lock_path) or any(path == Path(r["path"]) for r in self._archives):
            raise OutboxError("archive_target_conflict")
        return path

    def _artifact(self, *, operation_id=None, operator=None, reason=None) -> dict:
        """Export only whitelisted audit, with source and chain evidence."""
        from src.core import audit_archive as files
        return {"version": 1, "kind": "archive" if operation_id else "export", "binding": self.binding,
                "source_digest": self._queue_digest.hex(), "exported_at": self._now().isoformat(),
                "count": len(self._audit), "audit_digest": files.digest(self._audit), "audit": deepcopy(self._audit),
                "operation_id": operation_id, "operator": operator, "reason": reason,
                "previous_digest": self._archives[-1]["digest"] if self._archives else None}

    def audit_export(self, destination) -> dict:
        """Verify an exclusive export without changing source bytes or capacity."""
        from src.core import audit_archive as files
        with self._mutex:
            self._check()
            self._all_audit()
            path = self._artifact_target(destination)
            ref = files.write_artifact(path, self._artifact(), source_check=self._check)
            return {"phase": "export_verified", "count": ref["count"], "digest": ref["digest"], "path": str(path)}

    def audit_status(self, destination) -> dict:
        """Explicit artifact diagnosis: prepared evidence is never a source commit."""
        from src.core import audit_archive as files
        with self._mutex:
            self._check()
            self._all_audit()
            body, ref = files.read_artifact(destination)
            self._validate_audit(body["audit"], capacity=False)
            if body["binding"] != self.binding:
                raise OutboxError("binding_mismatch")
            committed = next((r for r in self._archives if r["operation_id"] == body["operation_id"]), None)
            if committed is not None and committed != ref:
                raise OutboxError("archive_invalid")
            prepared = (body["kind"] == "archive" and body["source_digest"] == self._queue_digest.hex()
                        and body["audit"] == self._audit
                        and body["previous_digest"] == (self._archives[-1]["digest"] if self._archives else None))
            phase = "archive_committed" if committed else "archive_prepared" if prepared else "export_verified" if body["kind"] == "export" else "archive_stale"
            return {"phase": phase, "count": body["count"], "digest": ref["digest"], "path": str(destination)}

    def audit_archive(self, *, destination, expected_digest, operation_id, operator, reason) -> dict:
        """Prepare/verify evidence, then one atomic reference-and-capacity commit."""
        from src.core import audit_archive as files
        with self._mutex:
            self._check()
            history = self._all_audit()
            if not files.valid_operation(operation_id, operator, reason):
                raise OutboxError("invalid_decision")
            path = Path(destination)
            files.parents(path)
            previous = next((r for r in self._archives if r["operation_id"] == operation_id), None)
            if previous:
                if any(previous[k] != v for k, v in {"path": str(path), "source_digest": expected_digest,
                                                       "operator": operator, "reason": reason}.items()):
                    raise OutboxError("operation_conflict")
                return {"phase": "archive_committed", "replayed": True, "count": previous["count"], "digest": previous["digest"]}
            if any(a["operation_id"] == operation_id for a in history):
                raise OutboxError("operation_conflict")
            if expected_digest != self._queue_digest.hex():
                raise OutboxError("revision_conflict")
            if not self._audit:
                raise OutboxError("archive_empty")
            if len(self._archives) >= files.MAX_ARCHIVES:
                raise OutboxError("archive_capacity")
            path = self._artifact_target(path)
            desired = self._artifact(operation_id=operation_id, operator=operator, reason=reason)
            if os.path.lexists(path):
                actual, ref = files.read_artifact(path)
                comparison = dict(actual, exported_at=desired["exported_at"])
                if comparison != desired:
                    raise OutboxError("operation_conflict")
                self._timestamp(actual["exported_at"])
                files.sync_artifact(ref)
            else:
                ref = files.write_artifact(path, desired, source_check=self._check)
            references = self._archives + [ref]
            if sum(r["bytes"] for r in references) > files.MAX_HISTORY_BYTES:
                raise OutboxError("archive_capacity")
            # Includes the new index and the original per-record transition reserve.
            if len(self._encode(self._records, [], references)) + 128 * len(self._records) > self.max_bytes:
                raise OutboxError("archive_marker_capacity")
            files.read_artifact(path, expected=ref)
            try:
                self._commit(deepcopy(self._records), [], archives=references)
            except (OutboxError, OSError):
                raise OutboxError("archive_commit_unconfirmed") from None
            return {"phase": "archive_committed", "replayed": False, "count": ref["count"], "digest": ref["digest"]}

    def summary(self) -> dict:
        """Capacity counts include audit bytes; identity digest pins prune plans."""
        with self._mutex:
            self._check()
            audit_bytes = len(json.dumps(self._audit, ensure_ascii=False).encode("utf-8"))
            audit_limit = min(65536, self.max_bytes // 4)
            next_bytes = len(self._encode(self._records))
            blocks = []
            for condition, code in ((len(self._audit) >= self.max_audit_records, "audit_count_full"),
                                    (audit_bytes >= audit_limit, "audit_bytes_full"),
                                    (next_bytes >= self.max_bytes, "queue_bytes_full"),
                                    (next_bytes + 128 * len(self._records) > self.max_bytes, "transition_reserve_insufficient"),
                                    (next_bytes + 128 * len(self._records) > self.max_bytes - self.reserve_bytes
                                     or len(self._records) >= self.max_records - self.reserve_records, "urgent_reserve_in_use")):
                if condition:
                    blocks.append(code)
            try:
                self._all_audit()
                archive_status = "verified"
            except OutboxError as error:
                archive_status = error.code
            return {"counts": {state: sum(r["state"] == state for r in self._records) for state in
                    ("pending", "sending", "received", "blocked", "exhausted", "failed", "resolved")},
                    "records": len(self._records), "bytes": self.path.stat().st_size,
                    "next_write_bytes": len(self._encode(self._records)),
                    "max_records": self.max_records, "max_bytes": self.max_bytes,
                    "reserve_records": self.reserve_records, "reserve_bytes": self.reserve_bytes,
                    "audit_records": len(self._audit), "max_audit_records": self.max_audit_records,
                    "audit_bytes": audit_bytes, "max_audit_bytes": audit_limit,
                    "audit_byte_headroom": audit_limit - audit_bytes,
                    "transition_reserve_bytes": 128 * len(self._records), "capacity_blocks": blocks,
                    "archive_count": len(self._archives), "archive_status": archive_status,
                    "history_allows_mutation": archive_status == "verified",
                    "snapshot_digest": self._queue_digest.hex()}

    def prune_plan(self, limit=32) -> list:
        """Only proven received/explicit resolved, aged from terminal evidence."""
        with self._mutex:
            self._check()
            cutoff = self._now() - timedelta(days=self.RETENTION_DAYS)
            return [r["event_id"] for r in self._records if r["state"] in ("received", "resolved")
                    and r["terminal_at"] is not None and self._timestamp(r["terminal_at"]) <= cutoff][:max(1, min(limit, 32))]

    def _entry(self, row, action, after, operation_id, operator, reason) -> dict:
        """Retain pre-decision failure evidence without copying event content."""
        return {"operation_id": operation_id, "event_id": row["event_id"], "action": action,
                "before_state": row["state"], "after_state": after, "operator": operator, "reason": reason,
                "at": self._now().isoformat(), "expected_revision": row["revision"],
                "before_attempts": row["attempts"], "before_error": row["last_error_code"],
                "before_http_status": row["http_status"], "cloud_alert_id": row["cloud_alert_id"],
                "snapshot_digest": self._queue_digest.hex()}

    def prune(self, *, expected_digest=None, operation_id=None, operator="system", reason="retention_expired") -> int:
        """Delete records and append evidence in the SAME atomic replacement."""
        with self._mutex:
            self._check()
            operation_id = operation_id or str(uuid4())
            if any(r["operation_id"] == operation_id for r in self._archives):
                raise OutboxError("operation_conflict")
            previous = [a for a in self._all_audit() if a["operation_id"] == operation_id]
            if previous:
                if any(a["action"] != "prune" or a["operator"] != operator or a["reason"] != reason
                       or a["snapshot_digest"] != expected_digest for a in previous):
                    raise OutboxError("operation_conflict")
                return len(previous)
            if expected_digest is not None and expected_digest != self._queue_digest.hex():
                raise OutboxError("revision_conflict")
            targets = set(self.prune_plan())
            if not targets:
                return 0
            audits = self._audit + [self._entry(r, "prune", "pruned", operation_id, operator, reason)
                                    for r in self._records if r["event_id"] in targets]
            self._commit([deepcopy(r) for r in self._records if r["event_id"] not in targets], audits)
            return len(targets)

    def decide(self, *, event_id, action, expected_revision, confirm_event_id, operation_id,
               operator, reason, evidence=None) -> dict:
        """Audited CAS decision; caller must obtain cloud evidence via device JWT."""
        with self._mutex:
            self._check()
            try:
                valid_op = str(UUID(operation_id)) == operation_id
            except (ValueError, TypeError, AttributeError):
                valid_op = False
            if (not valid_op or confirm_event_id != event_id or reason not in self.REASONS
                    or not isinstance(operator, str) or re.fullmatch(r"[A-Za-z0-9_@.\\-]{1,64}", operator) is None
                    or type(expected_revision) is not int or expected_revision < 0
                    or action not in ("resolve", "retry", "reconcile")):
                raise OutboxError("invalid_decision")
            if any(r["operation_id"] == operation_id for r in self._archives):
                raise OutboxError("operation_conflict")
            previous = [a for a in self._all_audit() if a["operation_id"] == operation_id]
            if previous:
                if len(previous) != 1 or any(previous[0][k] != v for k, v in {
                    "event_id": event_id, "action": action, "operator": operator, "reason": reason,
                    "expected_revision": expected_revision}.items()):
                    raise OutboxError("operation_conflict")
                return deepcopy(previous[0])
            records = deepcopy(self._records)
            row = next((r for r in records if r["event_id"] == event_id), None)
            if row is None or row["revision"] != expected_revision:
                raise OutboxError("revision_conflict")
            before = deepcopy(row)
            if action == "resolve":
                if row["state"] not in ("pending", "blocked", "exhausted", "failed"):
                    raise OutboxError("state_conflict")
                row.update(state="resolved", terminal_at=self._now().isoformat(), next_retry_at=None)
            else:
                if row["state"] == "resolved" or type(evidence) is not dict:
                    raise OutboxError("cloud_check_required")
                if evidence.get("receipt_status") == "received":
                    values = self.validate_receipt(evidence, event_id=event_id, device_id=row["device_id"])
                    if evidence.get("payload_hash") != payload_hash(row):
                        raise OutboxError("payload_conflict")
                    row.update(**values, state="received", terminal_at=self._now().isoformat(), http_status=200,
                               next_retry_at=None, last_error_code=None)
                elif (action == "retry" and row["state"] in ("blocked", "exhausted")
                      and evidence == {"event_id": event_id, "device_id": row["device_id"], "receipt_status": "not_found"}):
                    row.update(state="pending", retry_grants=row["retry_grants"] + 1, next_retry_at=None)
                else:
                    raise OutboxError("cloud_check_required")
            entry = self._entry(before, action, row["state"], operation_id, operator, reason)
            entry["cloud_alert_id"] = row["cloud_alert_id"]
            self._commit(records, self._audit + [entry])
            return deepcopy(entry)

    @classmethod
    def validate_receipt(cls, data, *, event_id: str, device_id: str) -> dict:
        """Accept only the cloud contract's received fact, never infer delivery."""
        if (type(data) is not dict or data.get("event_id") != event_id or data.get("device_id") != device_id
                or type(data.get("alert_id")) is not int or data["alert_id"] <= 0
                or data.get("receipt_status") != "received"
                or data.get("status") not in ("pending", "notified", "cancelled")
                or data.get("notification") not in cls.NOTIFICATIONS
                or data.get("confirmation") != "unconfirmed"
                or type(data.get("notification_attempts")) is not int or data["notification_attempts"] < 0
                or "notification_send_count" not in data or "notification_last_result" not in data
                or (data["notification_send_count"] is not None and
                    (type(data["notification_send_count"]) is not int or data["notification_send_count"] < 0))
                or data["notification_last_result"] not in (None, "claimed", "returned", "not_started", "unknown")):
            raise OutboxError("invalid_response")
        return {"cloud_alert_id": data["alert_id"], "cloud_notification": data["notification"],
                "cloud_confirmation": data["confirmation"]}

    def mark_received(self, event_id: str, data: dict, *, expected_attempts: int, http_status: int) -> None:
        """Update only the current claim after an authenticated, matched receipt."""
        with self._mutex:
            self._check()
            values = self.validate_receipt(data, event_id=event_id, device_id=self._binding["device_id"])
            if type(http_status) is not int or http_status not in (200, 201):
                raise OutboxError("invalid_response")
            records = deepcopy(self._records)
            current = next((r for r in records if r["event_id"] == event_id), None)
            if current is None or current["state"] != "sending" or current["attempts"] != expected_attempts:
                raise OutboxError("claim_conflict")
            current.update(**values, state="received", next_retry_at=None, last_error_code=None, http_status=http_status,
                           terminal_at=self._now().isoformat())
            self._commit(records)

    def record_failure(self, event_id: str, *, expected_attempts: int, error: str, http_status=None) -> None:
        """Bounded classifications and durable backoff, without deleting an alert."""
        if error not in self.ERROR_CODES - {None} or (http_status is not None and
                (type(http_status) is not int or not 100 <= http_status <= 599)):
            raise OutboxError("invalid_response")
        with self._mutex:
            self._check()
            records = deepcopy(self._records)
            current = next((r for r in records if r["event_id"] == event_id), None)
            if current is None or current["state"] != "sending" or current["attempts"] != expected_attempts:
                raise OutboxError("claim_conflict")
            state = ("blocked" if error == "authentication" else "failed" if error in
                     ("conflict", "invalid_payload", "request_rejected") else
                     "exhausted" if current["attempts"] >= self.MAX_ATTEMPTS + current["retry_grants"] else "pending")
            current.update(state=state, last_error_code=error, http_status=http_status,
                           next_retry_at=(self._now() + timedelta(seconds=min(60, 2 ** current["attempts"]))).isoformat() if state == "pending" else None)
            self._commit(records)

    async def simulate_attempt(self, event_id: str, fake_sender) -> None:
        """TEST PATH ONLY: no receipt is accepted and no real sender is wired."""
        async with self._simulation_lock:
            record = self.claim(event_id)
            if record is None:
                return
            error, status = "simulation_only", None
            try:
                result = await fake_sender(deepcopy(record))
                if type(result) is int and result in (401, 403):
                    error, status = "authentication", result
            except Exception:
                error = "simulation_failure"
            # Cancellation/abrupt exit leaves durable sending for recovery.
            with self._mutex:
                self._check()
                records = deepcopy(self._records)
                current = next(r for r in records if r["event_id"] == event_id)
                current.update(state="blocked" if status else "exhausted" if current["attempts"] >= self.MAX_ATTEMPTS + current["retry_grants"] else "pending",
                               last_error_code=error, http_status=status,
                               next_retry_at=(self._now() + timedelta(seconds=min(60, 2 ** current["attempts"]))).isoformat())
                self._commit(records)

    def close(self) -> None:
        """Release only this instance's OS descriptor; never delete lock/queue."""
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            if self._fd is not None:
                try:
                    if self._locked:
                        os.lseek(self._fd, 0, os.SEEK_SET)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(self._fd, fcntl.LOCK_UN)
                finally:
                    os.close(self._fd)
                    self._fd = None
                    self._locked = False

    def __enter__(self):
        """Hold exclusive ownership for the context lifetime."""
        return self

    def __exit__(self, *args):
        """Release ownership on both normal and exceptional context exit."""
        self.close()
