"""Trusted local administration; no sending, engines, or credential output."""
import re
from uuid import UUID

from sqlalchemy import func, or_, select, update

from app.models.models import AlertLog, Device, EdgeAlertReceipt as Receipt, Elder, NotificationRecovery as Audit
from app.services.alert_delivery import DeliveryError, DeliveryService


class RecoveryService:
    """CAS decision and immutable audit in one transaction on the existing DB."""

    REASONS = {"reviewed_unknown", "awaiting_evidence", "duplicate_risk_accepted"}

    def __init__(self, *, session_scope=None):
        """Production uses the checked application factory; tests the P0-12 scope."""
        self.delivery = DeliveryService(session_scope=session_scope)
        self.scope = self.delivery.session_scope

    @staticmethod
    def _audit(row) -> dict:
        """No payload, raw exceptions, claim token or credentials."""
        return {name: getattr(row, name) for name in (
            "operation_id", "receipt_id", "device_id", "elder_id", "operator", "reason",
            "action", "result", "expected_revision", "attempts")} | {"created_at": row.created_at.isoformat()}

    def query(self, receipt_id=None, *, after_id=0, limit=100) -> list:
        """Read-only paged unknown listing, or a specified record in any state."""
        try:
            with self.scope() as session:
                query = session.query(Receipt)
                query = query.filter(Receipt.id == receipt_id) if receipt_id is not None else query.filter(
                    Receipt.notification_state == "unknown", Receipt.id > after_id)
                rows = query.order_by(Receipt.id).limit(max(1, min(limit, 1000))).all()
                result = []
                for row in rows:
                    error = row.notification_last_error
                    safe_errors = self.delivery.ERRORS | {"lease_expired", "alert_cancelled"}
                    result.append({"receipt_id": row.id, "alert_id": row.alert_id, "device_id": row.device_id,
                                   "elder_id": row.elder_id, "event_id": row.event_id,
                                   "notification_state": row.notification_state,
                                   "attempts": row.notification_attempts, "revision": row.notification_revision,
                                   "last_error": error if error in safe_errors else "unclassified",
                                   "updated_at": row.notification_updated_at.isoformat() if row.notification_updated_at else None})
            return result
        except Exception:
            raise DeliveryError("notification_query_failed") from None

    def audit(self, receipt_id) -> list:
        """Inspect durable decisions without changing the notification."""
        try:
            with self.scope() as session:
                result = [self._audit(row) for row in session.query(Audit).filter_by(receipt_id=receipt_id)
                          .order_by(Audit.created_at, Audit.operation_id).all()]
            return result
        except Exception:
            raise DeliveryError("notification_audit_query_failed") from None

    def backlog(self) -> dict:
        """Aggregate persisted work only; this is not another process's health."""
        now = self.delivery._now()
        try:
            with self.scope() as session:
                counts = dict(session.query(Receipt.notification_state, func.count(Receipt.id))
                              .group_by(Receipt.notification_state).all())
                oldest = session.query(func.min(Receipt.received_at)).filter(
                    Receipt.notification_state.in_(("pending", "retry_wait"))).scalar()
                expired = session.query(func.count(Receipt.id)).filter(Receipt.notification_state == "sending",
                    or_(Receipt.notification_lease_until.is_(None), Receipt.notification_lease_until <= now)).scalar()
                result = {state: counts.get(state, 0) for state in (
                    "pending", "retry_wait", "sending", "unknown", "attempt_returned", "attempt_failed", "cancelled")}
                result.update(expired_sending=expired, oldest_wait_seconds=max(0, (now - oldest).total_seconds()) if oldest else None,
                              worker={"state": "unavailable", "source": "separate_cli_process"})
            return result
        except Exception:
            raise DeliveryError("notification_backlog_query_failed") from None

    def decide(self, *, receipt_id, device_id, elder_id, expected_revision, operation_id,
               operator, reason, action, authorize_duplicate=False) -> dict:
        """One explicit unknown decision; an authorization grants one extra claim."""
        try:
            valid_id = str(UUID(operation_id)) == operation_id
        except (ValueError, TypeError, AttributeError):
            valid_id = False
        if (not valid_id or not isinstance(operator, str)
                or re.fullmatch(r"[A-Za-z0-9_@.\\-]{1,64}", operator) is None
                or reason not in self.REASONS or action not in ("keep", "retry")
                or type(expected_revision) is not int or expected_revision < 0
                or (action == "retry" and authorize_duplicate is not True)):
            raise DeliveryError("invalid_recovery_request")
        now = self.delivery._now()
        try:
            with self.scope() as session:
                existing = session.get(Audit, operation_id)
                expected = dict(receipt_id=receipt_id, device_id=device_id, elder_id=elder_id,
                                expected_revision=expected_revision, operator=operator, reason=reason, action=action)
                if existing is not None:
                    if any(getattr(existing, name) != value for name, value in expected.items()):
                        raise DeliveryError("recovery_operation_conflict")
                    result = self._audit(existing)
                else:
                    row = session.get(Receipt, receipt_id)
                    if row is None:
                        raise DeliveryError("recovery_target_unavailable")
                    # Recheck live binding IN the CAS, not merely a preceding read.
                    bound = select(Device.device_id).join(Elder, Elder.id == Device.elder_id).where(
                        Device.device_id == device_id, Device.elder_id == elder_id,
                        Device.is_active.is_(True), Device.owner_id == Elder.owner_id)
                    active_alert = select(AlertLog.id).where(AlertLog.id == row.alert_id,
                        AlertLog.elder_id == elder_id, AlertLog.status != "cancelled")
                    values = {"notification_revision": Receipt.notification_revision + 1,
                              "notification_updated_at": now}
                    if action == "retry":
                        values.update(notification_state="retry_wait", notification_next_at=now)
                    changed = session.execute(update(Receipt).where(
                        Receipt.id == receipt_id, Receipt.device_id == device_id, Receipt.elder_id == elder_id,
                        Receipt.notification_state == "unknown", Receipt.notification_revision == expected_revision,
                        Receipt.notification_claim_token.is_(None),
                        or_(Receipt.notification_lease_until.is_(None), Receipt.notification_lease_until <= now),
                        Receipt.device_id.in_(bound), Receipt.alert_id.in_(active_alert),
                    ).values(**values)).rowcount
                    if changed != 1:
                        raise DeliveryError("recovery_state_or_binding_conflict")
                    audit = Audit(operation_id=operation_id, **expected,
                                  result="retry_authorized" if action == "retry" else "kept_unknown",
                                  attempts=row.notification_attempts, created_at=now)
                    session.add(audit)
                    session.commit()
                    result = self._audit(audit)
            return result  # Session close must also complete before success.
        except DeliveryError:
            raise
        except Exception:
            raise DeliveryError("recovery_commit_unconfirmed") from None
