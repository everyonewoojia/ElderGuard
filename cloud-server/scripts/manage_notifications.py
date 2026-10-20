"""Local trusted administrator only; inspect/decide, never send or migrate."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sys

from app.core import database
from app.services.alert_delivery import DeliveryError, DeliveryService
from app.services.notification_recovery import RecoveryService
from scripts.migrate_edge_alert_receipts import _validate


class SafeParser(argparse.ArgumentParser):
    """Argument errors must not echo arbitrary input into operator logs."""

    def error(self, message) -> None:
        """Keep help available, but refuse errors with a fixed category."""
        raise DeliveryError("invalid_admin_arguments")


def main(argv=None, *, validate_owner=None) -> int:
    """Pin original application objects and filesystem identity before any SQL."""
    parser = SafeParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list-unknown")
    listing.add_argument("--after-id", type=int, default=0)
    listing.add_argument("--limit", type=int, default=100)
    show = sub.add_parser("show")
    show.add_argument("--receipt-id", required=True, type=int)
    sub.add_parser("status")
    for name in ("keep", "retry"):
        action = sub.add_parser(name)
        action.add_argument("--receipt-id", required=True, type=int)
        action.add_argument("--device-id", required=True)
        action.add_argument("--elder-id", required=True, type=int)
        action.add_argument("--expected-revision", required=True, type=int)
        action.add_argument("--operation-id", required=True)
        action.add_argument("--operator", required=True)
        action.add_argument("--reason", required=True, choices=sorted(RecoveryService.REASONS))
        action.add_argument("--authorize-duplicate", action="store_true")
    try:
        args = parser.parse_args(argv)
    except DeliveryError:
        print("invalid_admin_arguments", file=sys.stderr)
        return 1
    if args.command == "retry":
        print("此前的外部发送可能已成功；授权重试可能造成重复通知。", file=sys.stderr)
    try:
        if validate_owner is not None:
            validate_owner()
        target, identity = _validate(database.engine, args.database)
        delivery = DeliveryService()

        @contextmanager
        def checked_scope():
            """Revalidate every query/decision and report an uncertain close."""
            if validate_owner is not None:
                validate_owner()
            if _validate(database.engine, target) != (target, identity):
                raise DeliveryError("database_identity_changed")
            with delivery._session() as session:
                yield session
            if _validate(database.engine, target) != (target, identity):
                raise DeliveryError("database_identity_changed")

        service = RecoveryService(session_scope=checked_scope)
        if args.command == "list-unknown":
            result = service.query(after_id=args.after_id, limit=args.limit)
        elif args.command == "show":
            result = {"records": service.query(args.receipt_id), "audit": service.audit(args.receipt_id)}
        elif args.command == "status":
            result = service.backlog()
        else:
            result = service.decide(receipt_id=args.receipt_id, device_id=args.device_id,
                elder_id=args.elder_id, expected_revision=args.expected_revision,
                operation_id=args.operation_id, operator=args.operator, reason=args.reason,
                action=args.command, authorize_duplicate=args.authorize_duplicate)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception:
        # Even a commit-success/response-failure must not print success or SQL.
        print("notification_admin_refused_or_unconfirmed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
