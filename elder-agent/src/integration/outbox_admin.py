"""Trusted local operator, exclusive existing Outbox; never directly POST alerts."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

from src.core.alert_outbox import AlertOutbox, OutboxError, payload_hash
from src.integration.edge import EdgeClient, ReceiptQueryError, EdgeCredentialError, _cloud_origin


class OutboxAdmin:
    """Separate cloud reception evidence from cloud notification administration."""

    def __init__(self, outbox, client=None):
        """Reuse the owned queue and its exact normalized cloud endpoint."""
        self.outbox, self.client = outbox, client
        if client is not None and _cloud_origin(client.base_url) != outbox.binding["cloud_origin"]:
            raise OutboxError("binding_mismatch")

    async def check(self, event_id) -> dict:
        """Read-only authenticated lookup plus canonical payload comparison."""
        row = self.outbox.get(event_id)
        if row is None or self.client is None:
            raise OutboxError("cloud_check_required")
        evidence = await self.client.fetch_receipt(row["elder_id"], event_id, device_id=row["device_id"])
        if evidence["receipt_status"] == "received" and evidence["payload_hash"] != payload_hash(row):
            raise OutboxError("payload_conflict")
        return evidence

    async def apply(self, **arguments) -> dict:
        """The caller's reviewed revision is never refreshed after network I/O."""
        previous = [a for a in self.outbox.audit() if a["operation_id"] == arguments["operation_id"]]
        if previous or arguments["action"] == "resolve":
            return self.outbox.decide(**arguments)
        evidence = await self.check(arguments["event_id"])
        if evidence["receipt_status"] == "not_found" and arguments["action"] == "reconcile":
            return {"result": "not_found", "event_id": arguments["event_id"]}
        return self.outbox.decide(**arguments, evidence=evidence)


class SafeParser(argparse.ArgumentParser):
    """Refuse without echoing arbitrary CLI values into logs."""

    def error(self, message) -> None:
        """Refuse parse errors without echoing arbitrary values."""
        raise OutboxError("invalid_arguments")


async def run(argv=None, *, client_factory=EdgeClient) -> int:
    """Explicit path/binding, no creation/recovery, and no hidden queue scan."""
    parser = SafeParser(description=__doc__)
    parser.add_argument("--outbox", required=True, type=Path)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--elder-id", required=True, type=int)
    parser.add_argument("--cloud-url", required=True)
    parser.add_argument("--token-file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    for command in ("audit-export", "audit-archive", "audit-status"):
        child = sub.add_parser(command)
        child.add_argument("--destination", required=True, type=Path)
        if command == "audit-archive":
            child.add_argument("--expected-digest", required=True)
            child.add_argument("--operation-id", required=True)
            child.add_argument("--operator", required=True)
            child.add_argument("--reason", choices=sorted(AlertOutbox.REASONS), required=True)
    for command in ("show", "check", "reconcile", "retry", "resolve"):
        child = sub.add_parser(command)
        child.add_argument("--event-id", required=True)
        if command in ("reconcile", "retry", "resolve"):
            child.add_argument("--confirm-event-id", required=True)
            child.add_argument("--expected-revision", type=int, required=True)
            child.add_argument("--operation-id", required=True)
            child.add_argument("--operator", required=True)
            child.add_argument("--reason", choices=sorted(AlertOutbox.REASONS - {"retention_expired"}), required=True)
    prune = sub.add_parser("prune")
    prune.add_argument("--confirm", action="store_true", required=True)
    prune.add_argument("--expected-digest", required=True)
    prune.add_argument("--operation-id", required=True)
    prune.add_argument("--operator", required=True)
    prune.add_argument("--reason", choices=sorted(AlertOutbox.REASONS), required=True)
    client = None
    try:
        args = parser.parse_args(argv)
        path = args.outbox
        if not path.is_absolute() or path.name != "alerts_outbox.json" or path.resolve() != path:
            raise OutboxError("identity")
        with AlertOutbox(path.parent, device_id=args.device_id, elder_id=args.elder_id, cloud_url=args.cloud_url,
                         create=False, recover_sending=False) as outbox:
            if args.command in ("check", "retry", "reconcile"):
                client = client_factory(base_url=args.cloud_url, token_file=args.token_file)
            admin = OutboxAdmin(outbox, client)
            if args.command == "list":
                result = outbox.summary()
            elif args.command == "audit-export":
                result = outbox.audit_export(args.destination)
            elif args.command == "audit-status":
                result = outbox.audit_status(args.destination)
            elif args.command == "audit-archive":
                result = outbox.audit_archive(destination=args.destination, expected_digest=args.expected_digest,
                    operation_id=args.operation_id, operator=args.operator, reason=args.reason)
            elif args.command == "show":
                row = outbox.get(args.event_id)
                if row is None:
                    raise OutboxError("not_found")
                result = {"record": {key: row[key] for key in ("event_id", "device_id", "elder_id", "state", "revision", "attempts",
                          "last_error_code", "http_status", "cloud_alert_id", "cloud_notification", "cloud_confirmation")},
                          "audit": [a for a in outbox.audit() if a["event_id"] == args.event_id]}
            elif args.command == "check":
                result = await admin.check(args.event_id)
            elif args.command == "prune":
                print(json.dumps({"prune_targets": outbox.prune_plan()}))
                replayed = any(a["operation_id"] == args.operation_id for a in outbox.audit())
                result = {"recorded_pruned": outbox.prune(expected_digest=args.expected_digest, operation_id=args.operation_id,
                                                operator=args.operator, reason=args.reason), "replayed": replayed}
            else:
                result = await admin.apply(event_id=args.event_id, action=args.command, confirm_event_id=args.confirm_event_id,
                    expected_revision=args.expected_revision, operation_id=args.operation_id, operator=args.operator, reason=args.reason)
        if client is not None:
            closing, client = client, None
            await closing.aclose()
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OutboxError, ReceiptQueryError, EdgeCredentialError) as error:
        allowed = {"owner_busy", "identity", "binding_mismatch", "not_found", "invalid_file", "audit_capacity",
                   "capacity", "revision_conflict", "operation_conflict", "state_conflict", "invalid_decision",
                   "cloud_check_required", "payload_conflict", "invalid_arguments", "write_failed",
                   "query_authentication", "query_unavailable", "query_invalid", "query_unavailable_or_invalid"}
        allowed.update({"archive_identity", "archive_missing", "archive_invalid", "archive_permissions", "archive_exists",
                        "archive_write_failed", "archive_capacity", "archive_index_invalid", "archive_empty",
                        "archive_target_conflict", "archive_marker_capacity", "archive_commit_unconfirmed",
                        "archive_sync_failed", "ownership", "ownership_unsupported"})
        category = error.code if isinstance(error, OutboxError) else str(error)
        category = category if category in allowed else "credentials_or_configuration_unavailable"
        print(f"outbox_admin_refused:{category}; stop Agent if it owns the queue", file=sys.stderr)
        return 1
    except Exception:
        print("outbox_admin_refused_or_unconfirmed; stop Agent and verify configuration before retrying", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                print("outbox_admin_client_close_failed", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
