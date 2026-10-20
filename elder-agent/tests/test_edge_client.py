"""P6 老人端 EdgeClient/EdgeReporter 单元测试：上报告警、失败兜底、断网重试 ≤3 次（KK01）"""
import pytest
import secrets
import logging
from uuid import uuid4

from src.integration.edge import EdgeClient, EdgeReporter


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch):
    """Never read deployment credentials while running unit tests."""
    monkeypatch.delenv("EDGE_DEVICE_TOKEN_FILE", raising=False)


@pytest.fixture()
def device_token():
    """Transport tests use a fresh JWT-shaped value, never a deployment secret."""
    return ".".join(secrets.token_urlsafe(24) for _ in range(3))


class _Resp:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json


class _FakeHttp:
    """可注入的假 http 客户端：按脚本返回响应或抛网络异常，记录调用"""

    def __init__(self, post_result=None, get_result=None, raise_post=False):
        self.posts = []
        self.gets = []
        self._post_result = post_result
        self._get_result = get_result
        self._raise_post = raise_post
        self.headers = []

    async def post(self, url, json, headers):
        self.posts.append((url, json))
        self.headers.append(headers)
        if self._raise_post:
            raise RuntimeError("network down")
        return self._post_result

    async def get(self, url, params, headers):
        self.gets.append((url, params))
        self.headers.append(headers)
        return self._get_result


async def test_report_alert_success(device_token):
    """上报告警成功返回 data"""
    http = _FakeHttp(post_result=_Resp(200, {"code": 200, "data": {"alert_id": 1, "status": "pending"}}))
    client = EdgeClient(base_url="http://x", client=http, device_token=device_token)
    data = await client.report_alert(1, "fall", "P0", event_id=str(uuid4()))
    assert data == {"alert_id": 1, "status": "pending"}
    url, payload = http.posts[0]
    assert payload["elder_id"] == 1
    assert "api/v1/edge/alerts" in url


async def test_report_alert_network_error_returns_none(device_token):
    """网络异常返回 None（触发重试）"""
    http = _FakeHttp(raise_post=True)
    client = EdgeClient(base_url="http://x", client=http, device_token=device_token)
    data = await client.report_alert(1, "fall", event_id=str(uuid4()))
    assert data is None


async def test_report_alert_non2xx_returns_none(device_token):
    """云端非 2xx 返回 None"""
    http = _FakeHttp(post_result=_Resp(500, {"code": 500, "message": "err"}))
    client = EdgeClient(base_url="http://x", client=http, device_token=device_token)
    data = await client.report_alert(1, "fall", event_id=str(uuid4()))
    assert data is None


async def test_fetch_privacy_returns_bool(device_token):
    """拉取授权状态返回 bool"""
    http = _FakeHttp(get_result=_Resp(200, {"code": 200, "message": "success", "data": {"elder_id": 1, "enabled": True}}))
    client = EdgeClient(base_url="http://x", client=http, device_token=device_token)
    assert await client.fetch_privacy(1) is True


async def test_fetch_privacy_failure_returns_none(device_token):
    """拉取授权失败返回 None"""
    http = _FakeHttp(raise_post=False, get_result=_Resp(500, {}))
    client = EdgeClient(base_url="http://x", client=http, device_token=device_token)
    assert await client.fetch_privacy(1) is None


class _AlwaysFail(EdgeClient):
    async def report_alert(self, *args, **kwargs):
        return None


async def test_reporter_retry_limits_3(device_token):
    """断网重试：失败入队，重试次数 ≤3，耗尽记失败（KK01）"""
    reporter = EdgeReporter(_AlwaysFail(base_url="http://x", device_token=device_token))
    ok = await reporter.report(1, "fall", "P0", "断网", event_id=str(uuid4()))
    assert ok is False
    assert len(reporter.pending) == 1
    assert reporter.pending[0]["retries_left"] == EdgeReporter.MAX_RETRIES - 1

    # 每次 flush 尝试一次重试，直到耗尽（总尝试 = MAX_RETRIES 次）
    for _ in range(EdgeReporter.MAX_RETRIES + 1):
        await reporter.flush()

    assert reporter.pending == []
    assert len(reporter.failures) == 1
    assert reporter.failures[0]["status"] == "failed"


async def test_reporter_success_not_queued(device_token):
    """上报成功不入队"""
    http = _FakeHttp(post_result=_Resp(200, {"code": 200, "data": {"alert_id": 1}}))
    reporter = EdgeReporter(EdgeClient(base_url="http://x", client=http, device_token=device_token))
    ok = await reporter.report(1, "fall", "P0", event_id=str(uuid4()))
    assert ok is True
    assert reporter.pending == []
    assert reporter.failures == []


async def _invoke(client, method):
    """Call all four public operations using only business identifiers."""
    if method == "report_alert":
        return await client.report_alert(1, "fall", "P0", event_id=str(uuid4()))
    return await getattr(client, method)(1)


METHODS = ["report_alert", "fetch_tasks", "fetch_settings", "fetch_privacy"]


@pytest.mark.parametrize("method", METHODS)
async def test_all_requests_send_only_header_credentials(device_token, method):
    response = _Resp(200, {"code": 200, "data": {"enabled": True}})
    http = _FakeHttp(post_result=response, get_result=response)
    client = EdgeClient(client=http, device_token=device_token)
    await _invoke(client, method)
    valid = http.headers == [{"Authorization": f"Bearer {device_token}"}]
    assert valid
    leaked = device_token in repr((http.posts, http.gets))
    assert not leaked


@pytest.mark.parametrize("source", ["argument", "environment"])
@pytest.mark.parametrize("method", METHODS)
async def test_credential_file_configuration(tmp_path, monkeypatch, device_token, source, method):
    path = tmp_path / "device.credential"
    path.write_text(device_token, encoding="utf-8")
    response = _Resp(200, {"code": 200, "message": "success", "data": {"elder_id": 1, "enabled": False}})
    http = _FakeHttp(get_result=response, post_result=response)
    kwargs = {"token_file": path} if source == "argument" else {}
    if source == "environment":
        monkeypatch.setenv("EDGE_DEVICE_TOKEN_FILE", str(path))
    client = EdgeClient(client=http, **kwargs)
    expected = [] if method == "fetch_tasks" else False if method == "fetch_privacy" else {"elder_id": 1, "enabled": False}
    assert await _invoke(client, method) == expected
    valid = http.headers == [{"Authorization": f"Bearer {device_token}"}]
    assert valid


def test_explicit_token_precedes_file_configuration(monkeypatch, device_token):
    monkeypatch.setenv("EDGE_DEVICE_TOKEN_FILE", "nonexistent.credential")
    client = EdgeClient(device_token=device_token)
    leaked = device_token in repr(client)
    assert not leaked


@pytest.mark.parametrize("token", [None, "", " ", "invalid", "a.b.c\r\nInjected: value"])
def test_missing_or_malformed_credentials_never_send_http(token):
    http = _FakeHttp()
    with pytest.raises(ValueError):
        EdgeClient(client=http, device_token=token)
    assert http.posts == []
    assert http.gets == []


@pytest.mark.parametrize("case", ["missing", "empty", "directory", "relative", "oversized", "link"])
def test_invalid_credential_file_fails_closed(tmp_path, monkeypatch, case):
    path = tmp_path / "device.credential"
    if case == "directory":
        path = tmp_path
    elif case != "missing":
        path.write_text("a" * 17000 if case == "oversized" else "", encoding="utf-8")
    if case == "relative":
        path = "device.credential"
    if case == "link":
        from pathlib import Path
        original = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda self: self == path or original(self))
    http = _FakeHttp()
    with pytest.raises(ValueError):
        EdgeClient(client=http, token_file=path)
    assert http.posts == []
    assert http.gets == []


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failures_keep_existing_fallback(device_token, method, status, caplog):
    response = _Resp(status, {"message": device_token})
    http = _FakeHttp(post_result=response, get_result=response)
    client = EdgeClient(client=http, device_token=device_token)
    with caplog.at_level(logging.WARNING):
        result = await _invoke(client, method)
    assert result == ([] if method == "fetch_tasks" else None)
    assert str(status) in caplog.text
    leaked = device_token in caplog.text + repr(client)
    assert not leaked


@pytest.mark.parametrize("method", METHODS)
async def test_exception_details_and_repr_are_redacted(device_token, method, caplog):
    class FailingHttp:
        """Simulate a transport exception containing sensitive request details."""

        async def post(self, *args, **kwargs):
            raise RuntimeError(device_token)

        async def get(self, *args, **kwargs):
            raise RuntimeError(device_token)

    client = EdgeClient(client=FailingHttp(), device_token=device_token)
    with caplog.at_level(logging.WARNING):
        result = await _invoke(client, method)
    assert result == ([] if method == "fetch_tasks" else None)
    leaked = device_token in caplog.text + repr(client)
    assert not leaked


async def test_retry_never_copies_credentials_to_business_state(device_token):
    http = _FakeHttp(post_result=_Resp(401, {}))
    reporter = EdgeReporter(EdgeClient(client=http, device_token=device_token))
    assert await reporter.report(1, "fall", event_id=str(uuid4())) is False
    leaked = device_token in repr(reporter.pending)
    assert not leaked
    for _ in range(EdgeReporter.MAX_RETRIES + 1):
        await reporter.flush()
    # Preserve existing behavior: initial attempt plus MAX_RETRIES retries.
    assert len(http.posts) == EdgeReporter.MAX_RETRIES + 1
    assert reporter.pending == []
    assert len(reporter.failures) == 1
    valid = all(h == {"Authorization": f"Bearer {device_token}"} for h in http.headers)
    assert valid
    leaked = device_token in repr((reporter.failures, http.posts))
    assert not leaked


def test_credential_error_never_includes_token(device_token):
    with pytest.raises(ValueError) as captured:
        EdgeClient(device_token=device_token + "\r\n")
    leaked = device_token in str(captured.value) + repr(captured.value)
    assert not leaked


def test_empty_explicit_token_never_falls_back(tmp_path, monkeypatch, device_token):
    path = tmp_path / "device.credential"
    path.write_text(device_token, encoding="utf-8")
    monkeypatch.setenv("EDGE_DEVICE_TOKEN_FILE", str(path))
    with pytest.raises(ValueError):
        EdgeClient(device_token="")


@pytest.mark.parametrize("body", [
    None, [], "invalid", {}, {"code": 200, "message": "success"},
    {"code": 200, "message": "success", "data": {"elder_id": 1, "enabled": "false"}},
    {"code": 200, "message": "success", "data": {"elder_id": 1, "enabled": 1}},
    {"code": 200, "message": "success", "data": {"elder_id": 2, "enabled": True}},
    {"code": 200, "message": "success", "data": {"elder_id": True, "enabled": True}},
    {"code": 403, "message": "denied", "data": {"elder_id": 1, "enabled": True}},
    {"data": {"elder_id": 1, "enabled": True}},
])
async def test_privacy_response_requires_strict_contract(device_token, body):
    http = _FakeHttp(get_result=_Resp(200, body))
    client = EdgeClient(client=http, device_token=device_token)
    assert await client.fetch_privacy(1) is None


async def test_close_only_owned_http_client(device_token, monkeypatch):
    from unittest.mock import AsyncMock
    import httpx
    owned = AsyncMock()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: owned)
    client = EdgeClient(device_token=device_token)
    assert client._http() is owned
    await client.aclose()
    await client.aclose()
    owned.aclose.assert_awaited_once()
    injected = AsyncMock()
    client = EdgeClient(client=injected, device_token=device_token)
    await client.aclose()
    injected.aclose.assert_not_awaited()
