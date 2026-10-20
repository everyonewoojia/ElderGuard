"""Bounded immutable audit artifacts; no queue mutation or network capability.

Paths and identities detect ordinary replacement, not hostile same-account
TOCTOU. A partial/uncertain output is retained, never overwritten or deleted.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from src.core.alert_outbox import AlertOutbox, OutboxError, _cloud_origin, _identity, _owned

MAX_ARCHIVES = 32
MAX_ARTIFACT_BYTES = 128 * 1024
MAX_HISTORY_BYTES = 2 * 1024 * 1024
KEYS = {"version", "kind", "binding", "source_digest", "exported_at", "count", "audit_digest",
        "audit", "operation_id", "operator", "reason", "previous_digest"}
REF_KEYS = {"path", "identity", "parents", "digest", "bytes", "count", "operation_id",
            "operator", "reason", "source_digest", "previous_digest"}


def encode(value) -> bytes:
    """Canonical JSON is the public digest contract."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value) -> str:
    """Hash exactly the canonical UTF-8 representation."""
    return hashlib.sha256(encode(value)).hexdigest()


def parents(path: Path) -> list:
    """Pin every ancestor, including the immediate output directory."""
    if (not path.is_absolute() or any(":" in part for part in path.parts[1:])
            or (os.name == "nt" and os.path.isreserved(path))
            or path.resolve() != path or len(str(path)) > 2048):
        raise OutboxError("archive_identity")
    result = [[str(p), list(_identity(p, True))] for p in path.parents]
    if not result or len(result) > 64:
        raise OutboxError("archive_identity")
    return result


def secure_output(path: Path, descriptor: int) -> None:
    """Set a private ACL before any sensitive byte, then verify it on reread."""
    if os.name != "nt":
        os.fchmod(descriptor, 0o600)
        return
    script = ("$ErrorActionPreference='Stop'; "
              "$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User; "
              "$acl=New-Object System.Security.AccessControl.FileSecurity; "
              "$acl.SetOwner($sid); $acl.SetAccessRuleProtection($true,$false); "
              "$rule=New-Object System.Security.AccessControl.FileSystemAccessRule($sid,'FullControl','Allow'); "
              "$acl.SetAccessRule($rule); Set-Acl -LiteralPath $env:ELDER_AUDIT_PATH -AclObject $acl")
    _powershell(path, script)


def _powershell(path, script):
    """Invoke platform ACL tooling without a shell, secret text or visible UI."""
    environment = os.environ.copy()
    environment["ELDER_AUDIT_PATH"] = str(path)
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-Command", script],
                                env=environment, capture_output=True, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        raise OutboxError("archive_permissions") from None
    if result.returncode:
        raise OutboxError("archive_permissions")


def _permissions(path):
    """Refuse widened access, even for an otherwise identical archive."""
    if os.name == "nt":
        _powershell(path, "$ErrorActionPreference='Stop'; "
                    "$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
                    "$acl=Get-Acl -LiteralPath $env:ELDER_AUDIT_PATH; "
                    "$parent=Get-Acl -LiteralPath ([System.IO.Path]::GetDirectoryName($env:ELDER_AUDIT_PATH)); "
                    "if ($acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne $sid -or "
                    "$parent.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne $sid) {exit 3}; "
                    "if (!$acl.AreAccessRulesProtected) {exit 3}; "
                    "foreach ($r in $acl.Access) {if ($r.AccessControlType -eq 'Allow' -and "
                    "$r.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -ne $sid) {exit 3}}")
    else:
        _owned(path)
        _owned(path.parent)
        if path.stat().st_mode & 0o077:
            raise OutboxError("archive_permissions")


def read_artifact(path, *, expected=None) -> tuple:
    """Reread bounded bytes, verifying handle identity, parents, ACL and digest."""
    path = Path(path)
    try:
        ancestry = parents(path)
        identity = list(_identity(path))
        _permissions(path)
        if expected is not None and (ancestry != expected["parents"] or identity != expected["identity"]):
            raise OutboxError("archive_identity")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
        if (list((opened.st_dev, opened.st_ino)) != identity or list(_identity(path)) != identity
                or parents(path) != ancestry or len(raw) > MAX_ARTIFACT_BYTES):
            raise OutboxError("archive_identity")
        body = json.loads(raw)
        if (type(body) is not dict or set(body) != KEYS or raw != encode(body)
                or type(body["version"]) is not int or body["version"] != 1
                or body["kind"] not in ("export", "archive") or type(body["audit"]) is not list
                or type(body["count"]) is not int or body["count"] != len(body["audit"])
                or body["audit_digest"] != digest(body["audit"])):
            raise OutboxError("archive_invalid")
        binding = body["binding"]
        if (type(binding) is not dict or set(binding) != {"device_id", "elder_id", "cloud_origin"}
                or not isinstance(binding["device_id"], str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", binding["device_id"]) is None
                or type(binding["elder_id"]) is not int or binding["elder_id"] <= 0
                or not isinstance(binding["cloud_origin"], str) or _cloud_origin(binding["cloud_origin"]) != binding["cloud_origin"]
                or not isinstance(body["source_digest"], str) or re.fullmatch(r"[0-9a-f]{64}", body["source_digest"]) is None
                or (body["previous_digest"] is not None and (not isinstance(body["previous_digest"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", body["previous_digest"]) is None))
                or (body["kind"] == "export" and any(body[k] is not None for k in ("operation_id", "operator", "reason")))
                or (body["kind"] == "archive" and (not 0 < body["count"] <= 256
                    or not valid_operation(body["operation_id"], body["operator"], body["reason"])))):
            raise OutboxError("archive_invalid")
        try:
            AlertOutbox._timestamp(body["exported_at"])
        except OutboxError:
            raise OutboxError("archive_invalid") from None
        fingerprint = hashlib.sha256(raw).hexdigest()
        ref = {"path": str(path), "identity": identity, "parents": ancestry, "digest": fingerprint,
               "bytes": len(raw), **{k: body[k] for k in ("count", "operation_id", "operator", "reason", "source_digest", "previous_digest")}}
        if expected is not None and ref != expected:
            raise OutboxError("archive_invalid")
        return body, ref
    except FileNotFoundError:
        raise OutboxError("archive_missing") from None
    except (OSError, ValueError, TypeError, KeyError) as error:
        if isinstance(error, OutboxError):
            raise
        raise OutboxError("archive_invalid") from None


def write_artifact(path, body, *, source_check) -> dict:
    """Exclusive create; failures keep evidence and leave the source untouched."""
    path = Path(path)
    try:
        ancestry = parents(path)
        _owned(path.parent)
        encoded = encode(body)
        if len(encoded) > MAX_ARTIFACT_BYTES:
            raise OutboxError("archive_capacity")
        source_check()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            identity = list(_identity(path))
            secure_output(path, stream.fileno())
            if (list((os.fstat(stream.fileno()).st_dev, os.fstat(stream.fileno()).st_ino)) != identity
                    or parents(path) != ancestry):
                raise OutboxError("archive_identity")
            source_check()
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        source_check()
        verified, ref = read_artifact(path)
        if verified != body or ref["identity"] != identity or ref["parents"] != ancestry:
            raise OutboxError("archive_invalid")
        return ref
    except FileExistsError:
        raise OutboxError("archive_exists") from None
    except OSError:
        raise OutboxError("archive_write_failed") from None


def sync_artifact(reference: dict) -> None:
    """A prepared file's earlier fsync may have failed; retry it before commit."""
    path = Path(reference["path"])
    try:
        if (parents(path) != reference["parents"]
                or list(_identity(path)) != reference["identity"]):
            raise OutboxError("archive_identity")
        # No truncation or byte writes. Windows flushing needs a writable handle.
        with path.open("r+b") as stream:
            info = os.fstat(stream.fileno())
            if list((info.st_dev, info.st_ino)) != reference["identity"]:
                raise OutboxError("archive_identity")
            os.fsync(stream.fileno())
        read_artifact(path, expected=reference)
    except OSError:
        raise OutboxError("archive_sync_failed") from None


def history(references, *, binding, validate_audit, timestamp) -> list:
    """Bounded ordered index, pinned identities, and hash links to every ancestor."""
    if type(references) is not list or len(references) > MAX_ARCHIVES:
        raise OutboxError("archive_index_invalid")
    result, previous, total = [], None, 0
    operations, paths = set(), set()
    for ref in references:
        if type(ref) is not dict or set(ref) != REF_KEYS:
            raise OutboxError("archive_index_invalid")
        body, _ = read_artifact(ref["path"], expected=ref)
        if (body["kind"] != "archive" or body["binding"] != binding or body["previous_digest"] != previous
                or body["operation_id"] in operations or ref["path"] in paths
                or not 0 < body["count"] <= 256 or not valid_operation(body["operation_id"], body["operator"], body["reason"])
                or not isinstance(body["source_digest"], str) or re.fullmatch(r"[0-9a-f]{64}", body["source_digest"]) is None):
            raise OutboxError("archive_index_invalid")
        timestamp(body["exported_at"])
        validate_audit(body["audit"])
        result.extend(body["audit"])
        operations.add(body["operation_id"])
        paths.add(ref["path"])
        previous = ref["digest"]
        total += ref["bytes"]
        if total > MAX_HISTORY_BYTES:
            raise OutboxError("archive_capacity")
    if operations.intersection(a["operation_id"] for a in result):
        raise OutboxError("archive_index_invalid")
    return result


def valid_operation(identity, operator, reason) -> bool:
    """Controlled audit identity fields, never free-form exception details."""
    from uuid import UUID
    try:
        return (str(UUID(identity)) == identity and isinstance(operator, str)
                and re.fullmatch(r"[A-Za-z0-9_@.\\-]{1,64}", operator) is not None
                and re.search(r"[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}", operator) is None
                and reason in ("reviewed", "credentials_repaired", "cannot_repair", "retention_expired"))
    except (ValueError, TypeError, AttributeError):
        return False
