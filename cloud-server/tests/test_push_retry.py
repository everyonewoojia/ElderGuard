"""P8-2 告警推送重试模块测试：重试 ≤3 次、失败有记录（M-APP-05 / FR-SC-06）"""
import pytest

from app.services.push_retry import PushRetryService


def test_retry_limited_to_max_retries():
    """sender 恒失败 → 重试恰好 3 次（不多于 3）、最终失败记录、返回 False"""
    calls = {"n": 0}

    def sender(payload, target):
        calls["n"] += 1
        return False

    svc = PushRetryService(max_retries=3, sender=sender)
    ok = svc.push({"alert_id": 1}, "user-1")
    assert ok is False
    assert calls["n"] == 3
    assert svc.failure_count() == 1
    assert svc.failures[0]["attempts"] == 3


def test_retry_succeeds_after_retry():
    """第二次成功 → 返回 True、仅调用 2 次、无失败记录"""
    calls = {"n": 0}

    def sender(payload, target):
        calls["n"] += 1
        return calls["n"] >= 2

    svc = PushRetryService(max_retries=3, sender=sender)
    ok = svc.push({}, None)
    assert ok is True
    assert calls["n"] == 2
    assert svc.failure_count() == 0


def test_retry_records_on_exception():
    """sender 抛网络异常 → 重试 3 次并记录异常原因"""
    def sender(payload, target):
        raise ConnectionError("网络异常")

    svc = PushRetryService(max_retries=3, sender=sender)
    ok = svc.push({}, None)
    assert ok is False
    assert svc.failure_count() == 1
    assert svc.failures[0]["error"] == "transport_error"
    assert svc.failures[0]["attempts"] == 3
    assert svc.failures[0]["payload"] is None
    assert svc.failures[0]["target"] is None


@pytest.mark.asyncio
async def test_apush_with_async_sender():
    """异步 sender：失败重试 ≤3 次、最终失败记录"""
    calls = {"n": 0}

    async def sender(payload, target):
        calls["n"] += 1
        return False

    svc = PushRetryService(max_retries=3, sender=sender)
    ok = await svc.apush({}, None)
    assert ok is False
    assert calls["n"] == 3
    assert svc.failure_count() == 1
