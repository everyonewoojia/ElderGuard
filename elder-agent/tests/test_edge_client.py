"""P6 老人端 EdgeClient/EdgeReporter 单元测试：上报告警、失败兜底、断网重试 ≤3 次（KK01）"""
import pytest

from src.integration.edge import EdgeClient, EdgeReporter


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

    async def post(self, url, json):
        self.posts.append((url, json))
        if self._raise_post:
            raise RuntimeError("network down")
        return self._post_result

    async def get(self, url, params):
        self.gets.append((url, params))
        return self._get_result


async def test_report_alert_success():
    """上报告警成功返回 data"""
    http = _FakeHttp(post_result=_Resp(200, {"code": 200, "data": {"alert_id": 1, "status": "pending"}}))
    client = EdgeClient(base_url="http://x", client=http)
    data = await client.report_alert(1, "fall", "P0")
    assert data == {"alert_id": 1, "status": "pending"}
    url, payload = http.posts[0]
    assert payload["elder_id"] == 1
    assert "api/v1/edge/alerts" in url


async def test_report_alert_network_error_returns_none():
    """网络异常返回 None（触发重试）"""
    http = _FakeHttp(raise_post=True)
    client = EdgeClient(base_url="http://x", client=http)
    data = await client.report_alert(1, "fall")
    assert data is None


async def test_report_alert_non2xx_returns_none():
    """云端非 2xx 返回 None"""
    http = _FakeHttp(post_result=_Resp(500, {"code": 500, "message": "err"}))
    client = EdgeClient(base_url="http://x", client=http)
    data = await client.report_alert(1, "fall")
    assert data is None


async def test_fetch_privacy_returns_bool():
    """拉取授权状态返回 bool"""
    http = _FakeHttp(get_result=_Resp(200, {"code": 200, "data": {"elder_id": 1, "enabled": True}}))
    client = EdgeClient(base_url="http://x", client=http)
    assert await client.fetch_privacy(1) is True


async def test_fetch_privacy_failure_returns_none():
    """拉取授权失败返回 None"""
    http = _FakeHttp(raise_post=False, get_result=_Resp(500, {}))
    client = EdgeClient(base_url="http://x", client=http)
    assert await client.fetch_privacy(1) is None


class _AlwaysFail(EdgeClient):
    async def report_alert(self, *args, **kwargs):
        return None


async def test_reporter_retry_limits_3():
    """断网重试：失败入队，重试次数 ≤3，耗尽记失败（KK01）"""
    reporter = EdgeReporter(_AlwaysFail(base_url="http://x"))
    ok = await reporter.report(1, "fall", "P0", "断网")
    assert ok is False
    assert len(reporter.pending) == 1
    assert reporter.pending[0]["retries_left"] == EdgeReporter.MAX_RETRIES - 1

    # 每次 flush 尝试一次重试，直到耗尽（总尝试 = MAX_RETRIES 次）
    for _ in range(EdgeReporter.MAX_RETRIES + 1):
        await reporter.flush()

    assert reporter.pending == []
    assert len(reporter.failures) == 1
    assert reporter.failures[0]["status"] == "failed"


async def test_reporter_success_not_queued():
    """上报成功不入队"""
    http = _FakeHttp(post_result=_Resp(200, {"code": 200, "data": {"alert_id": 1}}))
    reporter = EdgeReporter(EdgeClient(base_url="http://x", client=http))
    ok = await reporter.report(1, "fall", "P0")
    assert ok is True
    assert reporter.pending == []
    assert reporter.failures == []
