"""Health is derived from the retained task, never a second watchdog task."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.services.alert_delivery import DeliveryError, DeliveryWorker
from test_delivery_lifecycle import fake_service


async def test_health_tracks_successful_loop_and_lifecycle():
    fake = fake_service()
    fake.run_once.return_value = True
    worker = DeliveryWorker(fake)
    await worker.start()
    try:
        await asyncio.sleep(0)
        status = worker.health()
        assert status["state"] == "running"
        assert status["last_loop_at"] is not None
        assert status["last_processed_at"] is not None
        assert status["task_done"] is False
    finally:
        await worker.stop()
    assert worker.health()["state"] == "stopped"
    await worker.stop()
    assert worker.health()["state"] == "stopped"


async def test_fatal_task_is_observed_without_waiting_for_shutdown(caplog):
    fake = fake_service()
    fake.run_once.side_effect = RuntimeError("secret-details-not-for-output")
    worker = DeliveryWorker(fake)
    await worker.start()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    status = worker.health()
    assert status["state"] == "failed"
    assert status["last_error"] == "worker_unhandled_error"
    assert status["task_observed"] is True
    assert "secret-details-not-for-output" not in str(status) + caplog.text
    with pytest.raises(RuntimeError):
        await worker.stop()
    assert worker.health()["state"] == "failed"


async def test_unexpected_cancellation_is_failure():
    worker = DeliveryWorker(fake_service())
    await worker.start()
    worker.task.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert worker.health()["state"] == "failed"
    assert worker.health()["last_error"] == "worker_cancelled"
    await worker.stop()


async def test_startup_failure_is_visible():
    fake = fake_service()
    fake.check_schema.side_effect = DeliveryError("notification_schema_unavailable")
    worker = DeliveryWorker(fake)
    with pytest.raises(DeliveryError):
        await worker.start()
    assert worker.health()["state"] == "failed"
    assert worker.health()["last_error"] == "notification_schema_unavailable"
    await worker.stop()
    assert worker.health()["state"] == "failed"


async def test_blocked_loop_does_not_refresh_heartbeat():
    entered = asyncio.Event()
    fake = fake_service()

    async def blocking():
        entered.set()
        await asyncio.Event().wait()

    fake.run_once.side_effect = blocking
    worker = DeliveryWorker(fake, shutdown_timeout=0.01)
    await worker.start()
    await entered.wait()
    assert worker.health()["last_loop_at"] is None
    await asyncio.sleep(0.02)
    assert worker.health()["last_loop_at"] is None
    await worker.stop()


def test_public_health_degrades_without_exposing_details(client):
    worker = client.app.state.delivery_worker
    assert client.get("/health").json() == {"status": "healthy"}
    worker.state = "failed"
    worker.last_error = "worker_unhandled_error"
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded"}


async def test_session_close_failure_is_sanitized_and_detected(db_state, test_database, caplog):
    from contextlib import contextmanager
    from app.services.alert_delivery import DeliveryService

    @contextmanager
    def broken_close():
        with test_database.session() as session:
            yield session
        raise RuntimeError("sensitive-close-details")

    worker = DeliveryWorker(DeliveryService(session_scope=broken_close))
    with pytest.raises(DeliveryError) as captured:
        await worker.start()
    assert "sensitive-close-details" not in str(captured.value) + caplog.text
    assert worker.health()["state"] == "failed"


async def test_failed_worker_can_be_explicitly_restarted_without_duplicate_task():
    fake = fake_service()
    fake.run_once.side_effect = DeliveryError("notification_result_write_failed")
    worker = DeliveryWorker(fake)
    await worker.start()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert worker.health()["state"] == "failed"
    old = worker.task
    fake.run_once.side_effect = None
    await worker.restart()
    try:
        assert worker.task is not old and old.done()
        await asyncio.sleep(0)
        assert worker.health()["state"] == "running"
        with pytest.raises(DeliveryError, match="notification_worker_still_owned"):
            await worker.restart()
    finally:
        await worker.stop()


async def test_unexpected_normal_exit_is_failed_without_shutdown():
    worker = DeliveryWorker(fake_service())
    worker._run = AsyncMock(return_value=None)
    await worker.start()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert worker.health()["state"] == "failed"
    assert worker.health()["last_error"] == "worker_exited"
    await worker.stop()


async def test_timeout_retains_owned_task_and_reports_failure():
    entered, release = asyncio.Event(), asyncio.Event()

    async def delay_cancel():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    fake = fake_service()
    fake.run_once.side_effect = delay_cancel
    worker = DeliveryWorker(fake, shutdown_timeout=0.01)
    await worker.start()
    await entered.wait()
    task = worker.task
    try:
        with pytest.raises(DeliveryError, match="notification_shutdown_timeout"):
            await worker.stop()
        assert worker.task is task and not task.done()
        assert worker.health()["state"] == "failed"
        assert worker.health()["last_error"] == "notification_shutdown_timeout"
        with pytest.raises(DeliveryError):
            await worker.restart()
    finally:
        release.set()
        await worker.stop()


async def test_stale_heartbeat_makes_public_readiness_degraded(monkeypatch):
    from app.main import app, health_check
    worker = DeliveryWorker(fake_service())
    worker.state = "running"
    worker.last_loop_at = "2020-01-01T00:00:00+00:00"
    monkeypatch.setattr(app.state, "delivery_worker", worker, raising=False)
    response = await health_check()
    assert response.status_code == 503
