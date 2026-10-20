"""Local administration on the registered database, never a real network send."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.models import AlertLog, Device, EdgeAlertReceipt, Elder
from app.services.alert_delivery import DeliveryError
from test_alert_delivery import pending, service, record  # noqa: F401
from test_device_auth import identity, device_key  # noqa: F401


@pytest.fixture()
def unknown(pending, test_database):
    """A synthetic exhausted unknown outcome, retaining the original identifiers."""
    with test_database.session() as session:
        row = session.get(EdgeAlertReceipt, pending[0])
        row.notification_state = "unknown"
        row.notification_attempts = 3
        session.commit()
        return {"receipt_id": row.id, "device_id": row.device_id, "elder_id": row.elder_id,
                "event_id": row.event_id, "alert_id": row.alert_id}


def administration(owner):
    """Use the same protected scope as delivery and REST."""
    from app.services.notification_recovery import RecoveryService
    return RecoveryService(session_scope=owner.session)


def decision(target, **changes):
    """Declared local operator metadata, with an explicit reviewed revision."""
    result = {"receipt_id": target["receipt_id"], "device_id": target["device_id"],
              "elder_id": target["elder_id"], "expected_revision": 0,
              "operation_id": str(uuid4()), "operator": "local-admin", "reason": "reviewed_unknown",
              "action": "keep", "authorize_duplicate": False}
    result.update(changes)
    return result


async def test_query_is_read_only_and_unknown_remains_unsent(unknown, test_database):
    admin = administration(test_database)
    rows = admin.query()
    assert [row["receipt_id"] for row in rows] == [unknown["receipt_id"]]
    assert rows[0]["event_id"] == unknown["event_id"]
    assert "content" not in rows[0] and "claim_token" not in rows[0]
    assert admin.audit(unknown["receipt_id"]) == []
    assert await service(test_database).run_once() is False
    assert await service(test_database).run_once() is False
    assert record(test_database, unknown["receipt_id"])["state"] == "unknown"


def test_keep_is_durable_idempotent_and_does_not_change_send_evidence(unknown, test_database):
    args = decision(unknown)
    first = administration(test_database).decide(**args)
    assert first["result"] == "kept_unknown"
    assert administration(test_database).decide(**args) == first
    assert len(administration(test_database).audit(unknown["receipt_id"])) == 1
    assert record(test_database, unknown["receipt_id"])["state"] == "unknown"
    assert record(test_database, unknown["receipt_id"])["attempts"] == 3


@pytest.mark.parametrize("changes", [{"operator": ""}, {"reason": ""},
    {"reason": "Bearer private-token"}, {"action": "retry"},
    {"device_id": "other-device"}, {"elder_id": 999999}, {"expected_revision": 99}])
def test_invalid_or_mismatched_decision_has_no_side_effect(unknown, test_database, changes):
    with pytest.raises(DeliveryError):
        administration(test_database).decide(**decision(unknown, **changes))
    assert administration(test_database).audit(unknown["receipt_id"]) == []
    assert record(test_database, unknown["receipt_id"])["state"] == "unknown"


async def test_authorized_retry_keeps_ids_and_attempt_history_and_grants_one_attempt(unknown, test_database):
    admin = administration(test_database)
    result = admin.decide(**decision(unknown, action="retry", authorize_duplicate=True))
    assert result["result"] == "retry_authorized"
    assert record(test_database, unknown["receipt_id"])["attempts"] == 3
    sender = AsyncMock(return_value=1)
    assert await service(test_database, sender=sender).run_once() is True
    assert record(test_database, unknown["receipt_id"])["attempts"] == 4
    assert sender.call_args.args[1]["data"]["id"] == unknown["alert_id"]
    row = admin.query(unknown["receipt_id"])[0]
    assert row["event_id"] == unknown["event_id"]
    assert row["notification_state"] == "attempt_returned"
    assert len(admin.audit(unknown["receipt_id"])) == 1


@pytest.mark.parametrize("problem", ["disabled", "rebound", "owner", "cancelled", "lease", "state"])
def test_invalid_live_binding_or_state_refuses_retry(unknown, test_database, problem):
    with test_database.session() as session:
        row = session.get(EdgeAlertReceipt, unknown["receipt_id"])
        device = session.get(Device, row.device_id)
        if problem == "disabled":
            device.is_active = False
        elif problem == "rebound":
            device.elder_id = 999999
        elif problem == "owner":
            session.get(Elder, row.elder_id).owner_id = 999999
        elif problem == "cancelled":
            session.get(AlertLog, row.alert_id).status = "cancelled"
        elif problem == "lease":
            row.notification_lease_until = datetime.now() + timedelta(hours=1)
        else:
            row.notification_state = "sending"
        session.commit()
    with pytest.raises(DeliveryError):
        administration(test_database).decide(**decision(unknown, action="retry", authorize_duplicate=True))
    assert administration(test_database).audit(unknown["receipt_id"]) == []


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_commit_is_not_reported_success_and_operation_id_resolves_it(unknown, test_database, monkeypatch, committed):
    original = Session.commit

    def uncertain(session):
        if committed:
            original(session)
        raise SQLAlchemyError("private-details")

    args = decision(unknown, action="retry", authorize_duplicate=True)
    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", uncertain)
        with pytest.raises(DeliveryError, match="recovery_commit_unconfirmed"):
            administration(test_database).decide(**args)
    rows = administration(test_database).audit(unknown["receipt_id"])
    assert len(rows) == int(committed)
    assert record(test_database, unknown["receipt_id"])["state"] == ("retry_wait" if committed else "unknown")
    assert administration(test_database).decide(**args)["result"] == "retry_authorized"
    assert len(administration(test_database).audit(unknown["receipt_id"])) == 1


def test_audit_insert_failure_rolls_back_recovery(unknown, test_database, monkeypatch):
    original = Session.add

    def refused(session, instance, *args, **kwargs):
        if instance.__class__.__name__ == "NotificationRecovery":
            raise SQLAlchemyError("audit failure")
        return original(session, instance, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Session, "add", refused)
        with pytest.raises(DeliveryError):
            administration(test_database).decide(**decision(unknown, action="retry", authorize_duplicate=True))
    assert record(test_database, unknown["receipt_id"])["state"] == "unknown"
    assert administration(test_database).audit(unknown["receipt_id"]) == []


def test_two_administrators_only_one_revision_transition(unknown, test_database):
    def decide():
        try:
            return administration(test_database).decide(**decision(unknown, action="retry", authorize_duplicate=True))
        except DeliveryError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: decide(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(administration(test_database).audit(unknown["receipt_id"])) == 1


def test_backlog_is_read_only_and_reports_expired_sending(unknown, test_database):
    metrics = administration(test_database).backlog()
    assert metrics["unknown"] == 1 and metrics["pending"] == 0
    assert metrics["expired_sending"] == 0
    with test_database.session() as session:
        row = session.get(EdgeAlertReceipt, unknown["receipt_id"])
        row.notification_state = "sending"
        row.notification_lease_until = datetime(2020, 1, 1)
        session.commit()
    assert administration(test_database).backlog()["expired_sending"] == 1
    assert record(test_database, unknown["receipt_id"])["state"] == "sending"


async def test_worker_and_authorization_competition_preserves_one_logical_task(unknown, test_database):
    """A worker cannot take unknown work before the explicit CAS commit."""
    import asyncio
    sender = AsyncMock(return_value=1)
    worker = service(test_database, sender=sender)
    args = decision(unknown, action="retry", authorize_duplicate=True)
    results = await asyncio.gather(worker.run_once(), asyncio.to_thread(administration(test_database).decide, **args))
    assert results[1]["result"] == "retry_authorized"
    await worker.run_once()
    assert sender.await_count == 1
    assert record(test_database, unknown["receipt_id"])["attempts"] == 4
    assert len(administration(test_database).audit(unknown["receipt_id"])) == 1
    with test_database.session() as session:
        assert session.query(AlertLog).count() == 1
        assert session.query(EdgeAlertReceipt).count() == 1


async def test_recreated_worker_does_not_take_an_unexpired_claim(pending, test_database):
    import asyncio
    from app.services.alert_delivery import DeliveryWorker
    first = service(test_database)
    claim = first.claim()
    sender = AsyncMock(return_value=1)
    restarted = DeliveryWorker(service(test_database, sender=sender), poll_interval=0.01)
    await restarted.start()
    try:
        await asyncio.sleep(0.03)
        assert record(test_database, pending[0])["state"] == "sending"
        assert record(test_database, pending[0])["token"] == claim.token
        sender.assert_not_awaited()
    finally:
        await restarted.stop()
