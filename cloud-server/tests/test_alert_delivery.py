"""Persistent delivery, true SQLite claim competition, and conservative recovery."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import OperationalError

from app.models.models import AlertLog, EdgeAlertReceipt
from app.routers.edge import EdgeAlertIn, require_bound_elder
from app.services import device_auth, edge_alerts
from app.services.alert_delivery import DeliveryError, DeliveryService, NotSent
from test_device_auth import identity, device_key  # noqa: F401
from test_edge_idempotency import payload


@pytest.fixture()
def pending(identity, test_database):
    """Admission creates the initial task, never a second engine or fake JWT."""
    db, device, elder = identity
    token = device_auth.issue_token(db, device.device_id)
    body = payload(elder.id)
    with test_database.session() as session:
        authenticated = device_auth.authenticate_device(session, token)
        bound = require_bound_elder(session, authenticated, elder.id)
        alert, receipt, created = edge_alerts.accept_alert(session, authenticated, bound, EdgeAlertIn(**body))
        assert created
        result = receipt.id, alert.id, elder.owner_id
        db.rollback()  # Release the fixture's independent authentication read.
        return result


def service(owner, *, sender=None, clock=None, online=None):
    """Every real Session is opened by the registered P0-12 owner."""
    return DeliveryService(session_scope=owner.session,
                           sender=sender or AsyncMock(return_value=1),
                           online_owners=online or (lambda: None), now=clock)


def record(owner, ident):
    with owner.session() as session:
        row = session.get(EdgeAlertReceipt, ident)
        return {"state": row.notification_state, "attempts": row.notification_attempts,
                "count": row.notification_send_count, "result": row.notification_last_result,
                "error": row.notification_last_error, "next": row.notification_next_at,
                "alert_id": row.alert_id, "token": row.notification_claim_token,
                "updated": row.notification_updated_at}


def test_pending_state_is_atomic_with_receipt(pending, test_database):
    state = record(test_database, pending[0])
    assert state["state"] == "pending"
    assert state["attempts"] == 0
    assert state["alert_id"] == pending[1]
    assert state["updated"] is not None


def test_two_workers_cannot_claim_same_task(pending, test_database, monkeypatch):
    from sqlalchemy.orm import Session
    gate = threading.Barrier(2)
    updates = threading.Barrier(2)
    original = Session.execute

    def competing_update(session, statement, *args, **kwargs):
        if getattr(statement, "is_update", False):
            updates.wait(timeout=10)  # Both candidates were read before either CAS.
        return original(session, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", competing_update)

    def claim():
        worker = service(test_database)
        gate.wait(timeout=10)
        return worker.claim()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        claims = [future.result(timeout=20) for future in futures]
    assert sum(item is not None for item in claims) == 1
    assert record(test_database, pending[0])["attempts"] == 1
    assert service(test_database).claim() is None


@pytest.mark.parametrize("count", [0, 1, 2])
async def test_return_count_is_evidence_not_confirmation(pending, test_database, count):
    sender = AsyncMock(return_value=count)
    worker = service(test_database, sender=sender)
    assert await worker.run_once() is True
    stored = record(test_database, pending[0])
    assert stored["state"] == "attempt_returned"
    assert stored["count"] == count
    assert stored["result"] == "returned"
    assert stored["attempts"] == 1
    assert stored["alert_id"] == pending[1]
    assert await service(test_database, sender=sender).run_once() is False
    sender.assert_awaited_once()
    with test_database.session() as session:
        assert session.get(AlertLog, pending[1]).status == "pending"


async def test_offline_worker_does_not_consume_attempts(pending, test_database):
    sender = AsyncMock(return_value=0)
    worker = service(test_database, sender=sender, online=lambda: [])
    assert await worker.run_once() is False
    sender.assert_not_awaited()
    assert record(test_database, pending[0])["state"] == "pending"
    assert record(test_database, pending[0])["attempts"] == 0


async def test_known_unsent_failure_has_bounded_backoff(pending, test_database):
    now = [datetime(2026, 10, 18, tzinfo=timezone.utc)]
    sender = AsyncMock(side_effect=NotSent("no_connections"))
    for index in range(3):
        worker = service(test_database, sender=sender, clock=lambda: now[0])
        assert await worker.run_once() is True
        state = record(test_database, pending[0])
        assert state["attempts"] == index + 1
        assert state["state"] == ("retry_wait" if index < 2 else "attempt_failed")
        assert await worker.run_once() is False
        now[0] += timedelta(seconds=60)
    assert await service(test_database, sender=sender, clock=lambda: now[0]).run_once() is False
    assert sender.await_count == 3


async def test_sender_exception_is_unknown_and_never_auto_retried(pending, test_database, caplog):
    sender = AsyncMock(side_effect=RuntimeError("raw-details-must-not-be-logged"))
    worker = service(test_database, sender=sender)
    with caplog.at_level("WARNING"):
        assert await worker.run_once()
    assert record(test_database, pending[0])["state"] == "unknown"
    assert "raw-details-must-not-be-logged" not in caplog.text
    assert await service(test_database, sender=sender).run_once() is False
    sender.assert_awaited_once()


async def test_expired_sending_recovers_to_unknown_not_pending(pending, test_database):
    now = [datetime(2026, 10, 18, tzinfo=timezone.utc)]
    first = service(test_database, clock=lambda: now[0])
    claim = first.claim()
    assert claim is not None
    assert record(test_database, pending[0])["state"] == "sending"
    assert first.recover_expired() == 0
    now[0] += timedelta(seconds=60)
    restarted = service(test_database, clock=lambda: now[0])
    assert restarted.recover_expired() == 1
    assert record(test_database, pending[0])["state"] == "unknown"
    assert await restarted.run_once() is False
    restarted.sender.assert_not_awaited()
    assert first.record_result(claim, "attempt_returned", count=1) is False


async def test_result_commit_failure_retains_claim_for_conservative_recovery(pending, test_database, monkeypatch):
    worker = service(test_database)
    sender = AsyncMock(return_value=1)
    worker.sender = sender
    monkeypatch.setattr(worker, "record_result", Mock(side_effect=DeliveryError("result_write_failed")))
    with pytest.raises(DeliveryError, match="result_write_failed"):
        await worker.run_once()
    sender.assert_awaited_once()
    state = record(test_database, pending[0])
    assert state["state"] == "sending"
    assert state["alert_id"] == pending[1]
    assert state["attempts"] == 1


async def test_cancelled_send_propagates_and_becomes_unknown_after_lease(pending, test_database):
    entered = asyncio.Event()
    now = [datetime(2026, 10, 18, tzinfo=timezone.utc)]

    async def sender(owner, data):
        entered.set()
        await asyncio.Event().wait()

    worker = service(test_database, sender=sender, clock=lambda: now[0])
    task = asyncio.create_task(worker.run_once())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert record(test_database, pending[0])["state"] == "sending"
    now[0] += timedelta(seconds=60)
    assert service(test_database, clock=lambda: now[0]).recover_expired() == 1
    assert record(test_database, pending[0])["state"] == "unknown"


def test_claim_lock_contention_never_means_success(pending, test_database, monkeypatch):
    from sqlalchemy.orm import Session
    worker = service(test_database)
    original = Session.execute

    def busy(session, statement, *args, **kwargs):
        if getattr(statement, "is_update", False):
            import sqlite3
            error = sqlite3.OperationalError("database is locked")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise OperationalError("synthetic", {}, error)
        return original(session, statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Session, "execute", busy)
        assert worker.claim() is None
    assert record(test_database, pending[0])["state"] == "pending"


async def test_cancelled_business_alert_is_not_sent(pending, test_database):
    with test_database.session() as session:
        session.get(AlertLog, pending[1]).status = "cancelled"
        session.commit()
    worker = service(test_database)
    assert await worker.run_once() is False
    worker.sender.assert_not_awaited()
    assert record(test_database, pending[0])["state"] == "cancelled"


async def test_no_sql_connection_is_held_across_send(pending, test_database):
    from app.core import database

    async def sender(owner, data):
        assert database.engine.pool.checkedout() == 0
        await asyncio.sleep(0)
        assert database.engine.pool.checkedout() == 0
        return 1

    assert await service(test_database, sender=sender).run_once()
    assert database.engine.pool.checkedout() == 0


async def test_lease_expiring_during_send_does_not_report_result_write_success(pending, test_database):
    now = [datetime(2026, 10, 18, tzinfo=timezone.utc)]

    async def slow(owner, payload):
        now[0] += timedelta(seconds=60)
        return 1

    worker = service(test_database, sender=slow, clock=lambda: now[0])
    assert await worker.run_once() is False
    assert record(test_database, pending[0])["state"] == "sending"
    assert worker.recover_expired() == 1
    assert record(test_database, pending[0])["state"] == "unknown"


async def test_actual_result_transaction_failure_keeps_durable_sending(pending, test_database, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.orm import Session

    with monkeypatch.context() as patch:
        async def sender(owner, payload):
            # Admission and claim have committed. Fail the independent result
            # transaction, not the fixture or any unrelated authentication read.
            patch.setattr(Session, "commit", Mock(side_effect=SQLAlchemyError("private sql details")))
            return 1

        worker = service(test_database, sender=sender)
        with pytest.raises(DeliveryError, match="^notification_result_write_failed$"):
            await worker.run_once()
    state = record(test_database, pending[0])
    assert state["state"] == "sending"
    assert state["result"] == "claimed"
    assert state["count"] is None
    assert state["alert_id"] == pending[1]


async def test_send_timeout_is_unknown_and_releases_cooperative_sender(pending, test_database):
    released = asyncio.Event()

    async def sender(owner, payload):
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    worker = service(test_database, sender=sender)
    worker.send_timeout = 0.01
    assert await worker.run_once() is True
    assert released.is_set()
    stored = record(test_database, pending[0])
    assert stored["state"] == "unknown"
    assert stored["error"] == "send_timeout"
    assert await worker.run_once() is False
