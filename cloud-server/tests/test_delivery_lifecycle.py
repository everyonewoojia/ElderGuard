"""Worker lifecycle with a fake service: no database fixture or DDL."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.alert_delivery import DeliveryError, DeliveryWorker


def fake_service():
    """Never open a database while exercising task ownership."""
    return Mock(check_schema=Mock(), run_once=AsyncMock(return_value=False))


async def test_repeated_start_stop_owns_one_worker():
    service = fake_service()
    worker = DeliveryWorker(service, poll_interval=0.01, shutdown_timeout=0.1)
    await worker.start()
    task = worker.task
    try:
        await worker.start()
        assert worker.task is task
        await asyncio.sleep(0.02)
        assert service.run_once.await_count > 0
    finally:
        await worker.stop()
    assert task.done()
    assert worker.task is None
    calls = service.run_once.await_count
    await worker.stop()
    await asyncio.sleep(0)
    assert service.run_once.await_count == calls


async def test_startup_failure_creates_no_background_task():
    service = fake_service()
    service.check_schema.side_effect = DeliveryError("schema_unavailable")
    worker = DeliveryWorker(service)
    with pytest.raises(DeliveryError, match="schema_unavailable"):
        await worker.start()
    assert worker.task is None
    service.run_once.assert_not_awaited()
    await worker.stop()


async def test_bounded_shutdown_cancels_inflight_send_and_runs_finally():
    entered, released = asyncio.Event(), asyncio.Event()

    async def in_flight():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    service = fake_service()
    service.run_once.side_effect = in_flight
    worker = DeliveryWorker(service, shutdown_timeout=0.01)
    await worker.start()
    task = worker.task
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(worker.stop(), 1)
    assert task.done()
    assert released.is_set()
    assert service.run_once.await_count == 1


async def test_unrecoverable_worker_error_is_not_swallowed():
    service = fake_service()
    service.run_once.side_effect = DeliveryError("database_unavailable")
    worker = DeliveryWorker(service)
    await worker.start()
    await asyncio.sleep(0)
    with pytest.raises(DeliveryError, match="database_unavailable"):
        await worker.stop()
    assert worker.task is None


async def test_cancelling_shutdown_propagates_and_releases_child():
    entered, released = asyncio.Event(), asyncio.Event()

    async def in_flight():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    service = fake_service()
    service.run_once.side_effect = in_flight
    worker = DeliveryWorker(service, shutdown_timeout=1)
    await worker.start()
    await entered.wait()
    stopping = asyncio.create_task(worker.stop())
    await asyncio.sleep(0)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert released.is_set()
    assert worker.task is None


async def test_uncooperative_send_times_out_without_losing_task_ownership():
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_send():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()  # Simulate a dependency delaying cancellation.

    service = fake_service()
    service.run_once.side_effect = slow_send
    worker = DeliveryWorker(service, shutdown_timeout=0.01)
    await worker.start()
    original = worker.task
    await entered.wait()
    try:
        with pytest.raises(DeliveryError, match="notification_shutdown_timeout"):
            await asyncio.wait_for(worker.stop(), 0.2)
        assert worker.task is original
        await worker.start()
        assert worker.task is original  # Cannot hide it by starting a second task.
    finally:
        release.set()
        await worker.stop()
