"""Durable notification attempts, with SQLite CAS claims and no delivery claims.

The WS connection registry is process-local. Only a worker with locally online
owners claims production work; database CAS still arbitrates competing workers.
No database transaction remains open while awaiting an external send.
"""
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import secrets
import sqlite3

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.core import database
from app.core.config import settings
from app.models.models import AlertLog, EdgeAlertReceipt, Elder, NotificationRecovery
from app.services.ws_manager import manager

logger = logging.getLogger(__name__)


class DeliveryError(RuntimeError):
    """Only controlled diagnostic categories, never SQL/credential exceptions."""


class NotSent(RuntimeError):
    """Trusted pre-send evidence: no external send was attempted."""

    def __init__(self, code="no_connections"):
        self.code = "no_connections"
        super().__init__(self.code)


@dataclass(frozen=True)
class Claim:
    """Detached business snapshot; claim token is not a client credential."""

    receipt_id: int
    token: str
    owner_id: int
    payload: dict


class DeliveryService:
    """One claim, one external invocation, then an independently committed result."""

    MAX_ATTEMPTS = 3
    ERRORS = {None, "no_connections", "send_exception", "send_timeout", "invalid_send_result"}

    def __init__(self, *, session_scope=None, sender=None, online_owners=None,
                 now=None, lease_seconds=30, send_timeout=5):
        """Reuse the application factory, or an explicitly checked test scope."""
        self._engine, self._factory = database.engine, database.SessionLocal
        self._url = settings.DATABASE_URL
        self.session_scope = session_scope or self._session
        self.sender = sender if sender is not None else self._send
        self.online_owners = online_owners if online_owners is not None else manager.online_users
        self.clock = now or (lambda: datetime.now(timezone.utc))
        if not 0 < send_timeout < lease_seconds:
            raise DeliveryError("invalid_lease_configuration")
        self.lease_seconds = lease_seconds
        self.send_timeout = send_timeout
        self.last_error = None

    @classmethod
    def _attempt_limit(cls):
        """Each immutable manual authorization grants one claim; never reset history."""
        extra = select(func.count(NotificationRecovery.operation_id)).where(
            NotificationRecovery.receipt_id == EdgeAlertReceipt.id,
            NotificationRecovery.result == "retry_authorized").correlate(EdgeAlertReceipt).scalar_subquery()
        return cls.MAX_ATTEMPTS + extra

    def _now(self):
        """SQLite timestamps use UTC without a stored offset."""
        value = self.clock()
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value

    @contextmanager
    def _session(self):
        """Refuse changed factories/configuration before any connection is used."""
        if (database.engine is not self._engine or database.SessionLocal is not self._factory
                or settings.DATABASE_URL != self._url
                or self._factory.kw.get("bind") is not self._engine or self._factory.kw.get("binds")):
            raise DeliveryError("database_binding_changed")
        with self._factory() as session:
            if session.get_bind() is not self._engine:
                raise DeliveryError("session_binding_changed")
            yield session

    @staticmethod
    def _busy(error):
        """Only actual SQLite BUSY/LOCKED codes are transient competition."""
        return (isinstance(error, OperationalError)
                and getattr(error.orig, "sqlite_errorcode", -1) & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))

    def check_schema(self):
        """Read-only startup check; missing migration fails startup, never runs DDL."""
        try:
            with self.session_scope() as session:
                session.query(EdgeAlertReceipt).limit(0).all()
                session.query(NotificationRecovery).limit(0).all()
        except SQLAlchemyError:
            raise DeliveryError("notification_schema_unavailable") from None

    def recover_expired(self):
        """Expired sending is unknown, even if a process died before calling send."""
        now = self._now()
        try:
            with self.session_scope() as session:
                expired = session.execute(update(EdgeAlertReceipt).where(
                    EdgeAlertReceipt.notification_state == "sending",
                    or_(EdgeAlertReceipt.notification_lease_until.is_(None),
                        EdgeAlertReceipt.notification_lease_until <= now),
                ).values(notification_state="unknown", notification_last_result="unknown",
                         notification_last_error="lease_expired", notification_updated_at=now,
                         notification_claim_token=None, notification_lease_until=None,
                         notification_next_at=None)).rowcount
                # Preserve the existing family cancellation semantics without
                # relabeling a never-attempted task as a failed send.
                session.execute(update(EdgeAlertReceipt).where(
                    EdgeAlertReceipt.notification_state.in_(("pending", "retry_wait")),
                    EdgeAlertReceipt.alert_id.in_(select(AlertLog.id).where(AlertLog.status == "cancelled")),
                ).values(notification_state="cancelled", notification_last_result="not_started",
                         notification_last_error="alert_cancelled", notification_updated_at=now,
                         notification_next_at=None))
                session.commit()
                return expired
        except SQLAlchemyError as exc:
            if self._busy(exc):
                self.last_error = "database_busy"
                logger.warning("Notification recovery deferred: database_busy")
                return 0
            raise DeliveryError("notification_recovery_failed") from None

    def claim(self):
        """Conditional UPDATE is the cross-worker arbiter, not a Python lock."""
        owners = self.online_owners()
        if owners is not None and not owners:
            return None
        now = self._now()
        eligible = (
            EdgeAlertReceipt.notification_state.in_(("pending", "retry_wait")),
            EdgeAlertReceipt.notification_attempts < self._attempt_limit(),
            or_(EdgeAlertReceipt.notification_next_at.is_(None), EdgeAlertReceipt.notification_next_at <= now),
            EdgeAlertReceipt.alert_id.in_(select(AlertLog.id).where(AlertLog.status != "cancelled")),
        )
        try:
            with self.session_scope() as session:
                query = session.query(EdgeAlertReceipt, AlertLog, Elder.owner_id).join(
                    AlertLog, AlertLog.id == EdgeAlertReceipt.alert_id).join(Elder, Elder.id == AlertLog.elder_id).filter(*eligible)
                if owners is not None:
                    query = query.filter(Elder.owner_id.in_(owners))
                row = query.order_by(EdgeAlertReceipt.id).first()
                if row is None:
                    return None
                receipt, alert, owner_id = row
                token = secrets.token_urlsafe(24)
                claim = Claim(receipt.id, token, owner_id, {"type": "alert", "data": {
                    "id": alert.id, "elder_id": alert.elder_id, "alert_type": alert.alert_type,
                    "priority": alert.priority, "content": alert.content, "status": alert.status,
                    "created_at": alert.created_at.isoformat() if alert.created_at else None}})
                changed = session.execute(update(EdgeAlertReceipt).where(
                    EdgeAlertReceipt.id == receipt.id, *eligible,
                ).values(notification_state="sending", notification_claim_token=token,
                         notification_lease_until=now + timedelta(seconds=self.lease_seconds),
                         notification_attempts=EdgeAlertReceipt.notification_attempts + 1,
                         notification_last_attempt_at=now, notification_last_result="claimed",
                         notification_updated_at=now, notification_last_error=None,
                         notification_next_at=None, notification_send_count=None)).rowcount
                if changed != 1:
                    session.rollback()
                    return None
                session.commit()
                return claim  # Session/transaction has closed before send awaits.
        except SQLAlchemyError as exc:
            if self._busy(exc):
                self.last_error = "database_busy"
                logger.warning("Notification claim deferred: database_busy")
                return None
            raise DeliveryError("notification_claim_failed") from None

    def record_result(self, claim, state, *, count=None, error=None):
        """Only the unexpired claim owner may persist an observed result."""
        if state not in ("attempt_returned", "retry_wait", "attempt_failed", "unknown") or error not in self.ERRORS:
            raise DeliveryError("invalid_notification_result")
        if state == "attempt_returned" and (type(count) is not int or count < 0):
            raise DeliveryError("invalid_send_count")
        now = self._now()
        try:
            with self.session_scope() as session:
                row = session.get(EdgeAlertReceipt, claim.receipt_id)
                if row is None:
                    raise DeliveryError("notification_receipt_missing")
                retry_at = (now + timedelta(seconds=min(30, 2 ** row.notification_attempts))) if state == "retry_wait" else None
                changed = session.execute(update(EdgeAlertReceipt).where(
                    EdgeAlertReceipt.id == claim.receipt_id,
                    EdgeAlertReceipt.notification_state == "sending",
                    EdgeAlertReceipt.notification_claim_token == claim.token,
                    EdgeAlertReceipt.notification_lease_until > now,
                ).values(notification_state=state, notification_send_count=count,
                         notification_last_result="returned" if state == "attempt_returned" else "not_started" if state in ("retry_wait", "attempt_failed") else "unknown",
                         notification_last_error=error, notification_updated_at=now,
                         notification_next_at=retry_at, notification_claim_token=None,
                         notification_lease_until=None)).rowcount
                session.commit()
                return changed == 1
        except SQLAlchemyError:
            # Sending may already have happened. Keep the durable sending claim;
            # recovery will mark it unknown, never turn it into ordinary retry.
            raise DeliveryError("notification_result_write_failed") from None

    @staticmethod
    async def _send(owner_id, payload):
        """No in-memory blind retry after a possibly side-effecting WS call."""
        if owner_id not in manager.online_users():
            raise NotSent()
        return await manager.push_to_user(owner_id, payload)

    async def run_once(self):
        """Recover, claim, send once outside SQL, then persist actual evidence."""
        self.recover_expired()
        claim = self.claim()
        if claim is None:
            return False
        try:
            count = await asyncio.wait_for(self.sender(claim.owner_id, claim.payload), self.send_timeout)
            if type(count) is not int or count < 0:
                recorded = self.record_result(claim, "unknown", error="invalid_send_result")
            else:
                recorded = self.record_result(claim, "attempt_returned", count=count)
        except NotSent:
            with self.session_scope() as session:
                attempts = session.get(EdgeAlertReceipt, claim.receipt_id).notification_attempts
                limit = self.MAX_ATTEMPTS + session.query(NotificationRecovery).filter_by(
                    receipt_id=claim.receipt_id, result="retry_authorized").count()
            self.last_error = "no_connections"
            recorded = self.record_result(claim, "retry_wait" if attempts < limit else "attempt_failed", error="no_connections")
        except asyncio.CancelledError:
            # No SQL held across the await. Durable sending survives cancellation.
            raise
        except DeliveryError:
            raise
        except Exception as exc:
            code = "send_timeout" if isinstance(exc, TimeoutError) else "send_exception"
            self.last_error = code
            logger.warning("Notification send outcome unknown: %s", code)
            recorded = self.record_result(claim, "unknown", error=code)
        return recorded


class DeliveryWorker:
    """One retained task per lifespan, bounded cooperative shutdown."""

    def __init__(self, service=None, *, poll_interval=0.1, shutdown_timeout=1):
        self.service = service if service is not None else DeliveryService()
        self.poll_interval = poll_interval
        self.shutdown_timeout = shutdown_timeout
        self.task = None
        self._stop = asyncio.Event()
        self.state = "stopped"
        self.last_loop_at = None
        self.last_processed_at = None
        self.last_error = None
        self._observed = False

    def _fail(self, error) -> None:
        """Bounded classification: never publish arbitrary exception messages."""
        known = {"notification_schema_unavailable", "notification_recovery_failed", "notification_claim_failed",
                 "notification_result_write_failed", "database_binding_changed", "session_binding_changed",
                 "worker_unhandled_error", "schema_unavailable", "database_unavailable"}
        self.last_error = str(error) if isinstance(error, DeliveryError) and str(error) in known else "worker_unhandled_error"
        self.state = "failed"
        logger.error("Notification worker failed: %s", self.last_error)

    def _observe(self, task) -> None:
        """Done callback discovers failure without waiting for application shutdown."""
        if task is not self.task or not task.done():
            return
        self._observed = True
        if task.cancelled():
            if not self._stop.is_set():
                self.state, self.last_error = "failed", "worker_cancelled"
                logger.error("Notification worker failed: worker_cancelled")
            elif self.state != "failed":
                self.state = "stopped"
        elif task.exception() is not None:
            self._fail(task.exception())
        elif self._stop.is_set():
            if self.state != "failed":
                self.state = "stopped"
        else:
            self.state, self.last_error = "failed", "worker_exited"
            logger.error("Notification worker failed: worker_exited")

    def health(self) -> dict:
        """A snapshot of this process only; no database query or monitor task."""
        if self.task is not None and self.task.done() and not self._observed:
            self._observe(self.task)
        return {"state": self.state, "last_loop_at": self.last_loop_at,
                "last_processed_at": self.last_processed_at, "last_error": self.last_error,
                "task_done": self.task.done() if self.task is not None else True,
                "task_observed": self._observed}

    async def restart(self) -> None:
        """Explicit in-process recovery only after the old task has actually ended."""
        if self.task is not None and not self.task.done():
            raise DeliveryError("notification_worker_still_owned")
        if self.task is not None:
            self._observe(self.task)
            self.task = None
        await self.start()

    async def start(self):
        """Read-only schema validation; repeated starts cannot hide a task error."""
        if self.task is not None:
            if self.task.done():
                self.task.result()
            return
        self.state = "starting"
        try:
            self.service.check_schema()
        except BaseException as error:
            self._fail(error)
            if isinstance(error, asyncio.CancelledError):
                raise
            raise DeliveryError(self.last_error) from None
        self._stop.clear()
        self._observed = False
        self.last_error = None
        self.last_loop_at = self.last_processed_at = None
        self.task = asyncio.create_task(self._run())
        self.task.add_done_callback(self._observe)

    async def _run(self):
        """Do not catch fatal service/database errors and pretend to be healthy."""
        while not self._stop.is_set():
            try:
                processed = await self.service.run_once()
            except Exception as error:
                self._fail(error)
                raise DeliveryError(self.last_error) from None
            self.last_loop_at = datetime.now(timezone.utc).isoformat()
            if processed is True:
                self.last_processed_at = self.last_loop_at
            error = getattr(self.service, "last_error", None)
            if type(error) is str:
                self.last_error = error if error in ("database_busy", "no_connections", "send_exception", "send_timeout") else "operation_uncertain"
            self.state = "running"
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), self.poll_interval)
            except TimeoutError:
                pass

    async def stop(self):
        """Stop new claims, wait briefly, then cancel the cooperative send."""
        self._stop.set()
        if self.state != "failed":
            self.state = "stopping"
        task = self.task
        if task is None:
            if self.state != "failed":
                self.state = "stopped"
            return
        try:
            done, _ = await asyncio.wait({task}, timeout=self.shutdown_timeout)
            if not done:
                task.cancel()
                done, _ = await asyncio.wait({task}, timeout=self.shutdown_timeout)
            if not done:
                # Keep the handle: an uncooperative dependency must not allow a
                # second worker to start or be reported as successfully closed.
                self.state, self.last_error = "failed", "notification_shutdown_timeout"
                raise DeliveryError("notification_shutdown_timeout")
            if not task.cancelled():
                task.result()
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.wait({task}, timeout=self.shutdown_timeout)
            raise
        finally:
            if task.done():
                self._observe(task)
                self.task = None
