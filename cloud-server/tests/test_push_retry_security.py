"""Synthetic error and diagnostic boundaries for the legacy retry service."""
import asyncio
import json
import logging

import pytest

from app.services import push_retry


TOKEN = "SYNTHETIC_TOKEN_CANARY"
TARGET = "SYNTHETIC_TARGET_CANARY"
PAYLOAD = "SYNTHETIC_PAYLOAD_CANARY"
EXCEPTION = "SYNTHETIC_EXCEPTION_CANARY"
PRIVATE = f"Bearer {TOKEN} {PAYLOAD} https://internal.invalid/?credential={TARGET} {EXCEPTION}"
MODES = ["push", "apush-sync", "apush-async"]


class UnprintableError(Exception):
    """Native exception formatting itself must never run in diagnostics."""

    def __str__(self):
        raise RuntimeError(EXCEPTION)

    def __repr__(self):
        raise RuntimeError(TOKEN)


class PrivateObject:
    """Payloads and targets may not be safe to print or serialize."""

    def __str__(self):
        raise RuntimeError(PRIVATE)

    def __repr__(self):
        raise RuntimeError(PRIVATE)


def error_case(kind):
    """Build errors without reading any real credentials or configuration."""
    if kind == "timeout":
        return TimeoutError(PRIVATE), "timeout"
    if kind == "connection":
        return ConnectionError(PRIVATE), "transport_error"
    if kind == "empty":
        return ValueError(""), "unknown_error"
    if kind == "none":
        return Exception(None), "unknown_error"
    if kind == "long":
        return RuntimeError(PRIVATE * 4096), "unknown_error"
    if kind == "unprintable":
        return UnprintableError(), "unknown_error"
    if kind == "chain":
        error = RuntimeError(EXCEPTION)
        error.__cause__ = ValueError(PRIVATE)
        error.__context__ = ConnectionError(PRIVATE)
        return error, "unknown_error"
    if kind == "os-error":
        return OSError(PRIVATE), "unknown_error"
    return RuntimeError(PRIVATE), "unknown_error"


def service(mode, behavior, retries=3):
    """Adapt one controlled sender to both supported asynchronous call styles."""
    async def async_sender(payload, target):
        return behavior(payload, target)

    return push_retry.PushRetryService(
        max_retries=retries, sender=async_sender if mode == "apush-async" else behavior,
    )


async def invoke(instance, mode, payload, target):
    """Execute the actual production retry loop without network or sleeps."""
    if mode == "push":
        return instance.push(payload, target)
    return await instance.apush(payload, target)


def assert_safe_records(instance, caplog, category, attempts=3):
    """Check actual LogRecord arguments as well as normal diagnostic renderings."""
    assert instance.failure_count() == 1
    assert set(instance.failures[0]) == {"payload", "target", "attempts", "error"}
    assert instance.failures[0]["payload"] is None, "raw payload must not be retained"
    assert instance.failures[0]["target"] is None, "raw target must not be retained"
    assert instance.failures[0]["attempts"] == attempts
    assert instance.failures[0]["error"] == category
    logs = [record for record in caplog.records if record.name == push_retry.logger.name]
    assert len(logs) == 1
    record = logs[0]
    assert record.levelno == logging.ERROR
    assert record.msg == "push_retry_failed attempts=%d category=%s"
    assert record.args == (attempts, category)
    assert record.exc_info is None
    assert record.stack_info is None
    rendered = " ".join([
        caplog.text, record.getMessage(), repr(record.args),
        json.dumps(instance.failures), str(instance.failures), repr(instance.failures),
    ])
    for marker in [TOKEN, TARGET, PAYLOAD, EXCEPTION, "internal.invalid"]:
        assert marker not in rendered, "controlled diagnostics must omit sensitive markers"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", [
    "timeout", "connection", "runtime", "empty", "none", "long", "unprintable", "chain", "os-error",
])
async def test_sensitive_errors_use_finite_categories_and_safe_failure_records(mode, kind, caplog):
    """Exception text, attributes and chains cannot become retained diagnostics."""
    error, category = error_case(kind)
    calls = []
    payload = {"content": PAYLOAD, "Authorization": f"Bearer {TOKEN}"}
    target = f"https://internal.invalid/push?credential={TARGET}"

    def sender(p, t):
        assert p is payload and t is target
        calls.append(1)
        raise error

    instance = service(mode, sender)
    assert await invoke(instance, mode, payload, target) is False
    assert len(calls) == 3
    assert_safe_records(instance, caplog, category)


@pytest.mark.parametrize("mode", MODES)
async def test_complex_payload_and_target_are_not_formatted_or_retained(mode, caplog):
    """Only sender receives the original objects; all retained fields are safe."""
    payload, target = PrivateObject(), PrivateObject()
    calls = []

    def sender(p, t):
        assert p is payload and t is target
        calls.append(1)
        return False

    instance = service(mode, sender)
    assert await invoke(instance, mode, payload, target) is False
    assert len(calls) == 3
    assert_safe_records(instance, caplog, "delivery_error")


@pytest.mark.parametrize("mode", MODES)
async def test_mixed_retry_errors_keep_final_category_and_attempt_count(mode, caplog):
    """Multiple failures neither reduce attempts nor create duplicate records."""
    errors = [ConnectionError(TOKEN), RuntimeError(PAYLOAD), TimeoutError(TARGET)]
    calls = []

    def sender(_payload, _target):
        error = errors[len(calls)]
        calls.append(1)
        raise error

    instance = service(mode, sender)
    assert await invoke(instance, mode, None, None) is False
    assert len(calls) == 3
    assert_safe_records(instance, caplog, "timeout")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("retries", [0, 1, 5])
async def test_existing_custom_retry_limits_and_none_inputs_are_preserved(mode, retries, caplog):
    """Zero and custom attempt limits preserve the existing range semantics."""
    calls = []

    def sender(payload, target):
        assert payload is None and target is None
        calls.append(1)
        return False

    instance = service(mode, sender, retries)
    assert await invoke(instance, mode, None, None) is False
    assert len(calls) == retries
    assert_safe_records(instance, caplog, "delivery_error" if retries else "unknown_error", retries)


@pytest.mark.parametrize("mode", MODES)
async def test_failure_history_survives_later_success_without_duplicate_record(mode, caplog):
    """Keep the same list and count across failed, successful and failed calls."""
    outcome, calls = [False], []

    def sender(_payload, _target):
        calls.append(1)
        return outcome[0]

    instance = service(mode, sender, 2)
    history = instance.failures
    assert await invoke(instance, mode, PAYLOAD, TARGET) is False
    assert len(calls) == 2
    outcome[0] = True
    assert await invoke(instance, mode, PAYLOAD, TARGET) is True
    assert len(calls) == 3
    assert instance.failures is history
    assert_safe_records(instance, caplog, "delivery_error", 2)
    outcome[0] = False
    assert await invoke(instance, mode, PAYLOAD, TARGET) is False
    assert len(calls) == 5
    assert instance.failure_count() == 2
    assert instance.failures is history
    assert all(row == {"payload": None, "target": None, "attempts": 2, "error": "delivery_error"}
               for row in history)


@pytest.mark.parametrize("mode", MODES)
async def test_formatter_exception_does_not_escape_or_undo_failure_record(mode, capsys):
    """A propagating diagnostic sink failure cannot disclose its exception."""
    class BrokenFormatter(logging.Formatter):
        def format(self, _record):
            raise RuntimeError(PRIVATE)

    class StrictHandler(logging.Handler):
        def emit(self, record):
            self.format(record)

    handler = StrictHandler()
    handler.setFormatter(BrokenFormatter())
    calls = []

    def sender(_payload, _target):
        calls.append(1)
        raise ConnectionError(PRIVATE)

    instance = service(mode, sender)
    push_retry.logger.addHandler(handler)
    try:
        assert await invoke(instance, mode, PAYLOAD, TARGET) is False
    finally:
        push_retry.logger.removeHandler(handler)
        handler.close()
    assert len(calls) == 3
    assert instance.failures == [{"payload": None, "target": None, "attempts": 3, "error": "transport_error"}]
    output = capsys.readouterr()
    for marker in [TOKEN, PAYLOAD, TARGET, EXCEPTION]:
        assert marker not in output.out + output.err


@pytest.mark.parametrize("mode", MODES)
async def test_cancellation_keeps_original_exception_propagation(mode, caplog):
    """Do not widen Exception handling to swallow task cancellation."""
    def sender(_payload, _target):
        raise asyncio.CancelledError(EXCEPTION)

    instance = service(mode, sender)
    with pytest.raises(asyncio.CancelledError):
        await invoke(instance, mode, PAYLOAD, TARGET)
    assert instance.failures == []
    assert not [record for record in caplog.records if record.name == push_retry.logger.name]


@pytest.mark.parametrize("category", [None, "", PRIVATE, {"private": TOKEN}, PrivateObject()],
                         ids=["none", "empty", "private-text", "mapping", "object"])
def test_record_boundary_rejects_non_whitelisted_error_categories(category, caplog):
    """No arbitrary label can reintroduce sensitive data at the logging boundary."""
    instance = push_retry.PushRetryService()
    instance._record(PAYLOAD, TARGET, 3, category)
    assert_safe_records(instance, caplog, "unknown_error")


def test_default_sender_behavior_is_unchanged():
    """Do not change the legacy constructor's existing no-sender behavior."""
    instance = push_retry.PushRetryService()
    assert instance.push(PAYLOAD, TARGET) is True
    assert instance.failures == []
