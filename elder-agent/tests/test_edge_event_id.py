"""Transport adaptation only: no production worker or real HTTP requests."""
import secrets
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from src.integration.edge import EdgeClient, EdgeReporter


async def test_explicit_event_id_is_transported_without_regeneration():
    event_id = str(uuid4())
    token = ".".join(secrets.token_urlsafe(24) for _ in range(3))
    response = Mock(status_code=200, json=lambda: {"data": {"alert_id": 1, "event_id": event_id}})
    http = Mock(post=AsyncMock(return_value=response))
    client = EdgeClient(client=http, device_token=token)
    result = await client.report_alert(1, "help", "P0", "synthetic", event_id=event_id)
    assert result["event_id"] == event_id
    body = http.post.call_args.kwargs["json"]
    assert body["event_id"] == event_id
    assert not any(token in str(value) for value in body.values())
    assert event_id not in http.post.call_args.args[0]


@pytest.mark.parametrize("value", [None, "invalid", "", 1])
async def test_invalid_identity_fails_before_http(value):
    token = ".".join(secrets.token_urlsafe(24) for _ in range(3))
    http = Mock(post=AsyncMock())
    client = EdgeClient(client=http, device_token=token)
    with pytest.raises(ValueError):
        await client.report_alert(1, "help", event_id=value)
    http.post.assert_not_called()


async def test_missing_identity_has_no_anonymous_or_regenerated_fallback():
    token = ".".join(secrets.token_urlsafe(24) for _ in range(3))
    http = Mock(post=AsyncMock())
    client = EdgeClient(client=http, device_token=token)
    with pytest.raises(TypeError):
        await client.report_alert(1, "help")
    http.post.assert_not_called()


async def test_legacy_reporter_keeps_one_identity_through_exhaustion():
    client = Mock(report_alert=AsyncMock(return_value=None))
    reporter = EdgeReporter(client)
    event_id = str(uuid4())
    assert await reporter.report(1, "help", event_id=event_id) is False
    assert reporter.pending[0]["event_id"] == event_id
    for _ in range(EdgeReporter.MAX_RETRIES + 1):
        await reporter.flush()
    assert reporter.pending == []
    assert reporter.failures[0]["event_id"] == event_id
    assert all(call.kwargs["event_id"] == event_id for call in client.report_alert.call_args_list)
    assert client.report_alert.await_count == 1 + EdgeReporter.MAX_RETRIES
