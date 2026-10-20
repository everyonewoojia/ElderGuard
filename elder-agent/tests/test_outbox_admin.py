"""JWT-only GET reconciliation and an exclusive, non-creating operator CLI."""
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from src.core.alert_outbox import OutboxError, payload_hash
from src.integration.edge import EdgeClient, ReceiptQueryError
from src.integration.outbox_admin import OutboxAdmin, run
from test_alert_outbox import event, queue
from test_outbox_lifecycle import arguments, terminal
from test_reporter_production import receipt, token


def query_body(row, *, found=True, **changes):
    if not found:
        return {"code": 200, "message": "success", "data": {
            "event_id": row["event_id"], "device_id": row["device_id"], "receipt_status": "not_found"}}
    body = receipt({"eventId": row["event_id"]}, payload_hash=payload_hash(row))
    body["data"].update(changes)
    return body


async def test_reconcile_cloud_received_without_post_and_keep_original_identity(tmp_path):
    with queue(tmp_path) as outbox:
        row = terminal(outbox, received=False)
        observed = []
        secret = token()
        def handler(request):
            observed.append(request.method)
            authenticated = request.headers["Authorization"] == f"Bearer {secret}"
            assert authenticated
            return httpx.Response(200, json=query_body(row))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = EdgeClient(base_url="https://example.invalid", client=http, device_token=secret)
            admin = OutboxAdmin(outbox, client)
            before = outbox.path.read_bytes()
            assert (await admin.check(row["event_id"]))["receipt_status"] == "received"
            assert before == outbox.path.read_bytes()
            args = arguments(row, "reconcile")
            await admin.apply(**args)
            await admin.apply(**args)
        stored = outbox.get(row["event_id"])
        assert stored["state"] == "received" and stored["cloud_alert_id"] == 17
        assert stored["cloud_notification"] == "unknown" and stored["cloud_confirmation"] == "unconfirmed"
        assert observed == ["GET", "GET"]
        assert len(outbox.audit()) == 1


@pytest.mark.parametrize("case", ["401", "403", "500", "network", "bad_json", "wrong_device", "hash", "bad_id", "missing"])
async def test_uncertain_query_never_mutates_or_posts(tmp_path, case, caplog):
    with queue(tmp_path) as outbox:
        row = terminal(outbox, received=False)
        before = outbox.path.read_bytes()
        def handler(request):
            assert request.method == "GET"
            if case.isdigit():
                return httpx.Response(int(case))
            if case == "network":
                raise httpx.ReadError("private synthetic failure")
            if case == "bad_json":
                return httpx.Response(200, content=b"not json")
            data = query_body(row)
            if case == "wrong_device":
                data["data"]["device_id"] = "other"
            elif case == "hash":
                data["data"]["payload_hash"] = "0" * 64
            elif case == "bad_id":
                data["data"]["alert_id"] = True
            else:
                data["data"].pop("receipt_status")
            return httpx.Response(200, json=data)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            admin = OutboxAdmin(outbox, EdgeClient(base_url="https://example.invalid", client=http, device_token=token()))
            with pytest.raises((OutboxError, ReceiptQueryError)):
                await admin.apply(**arguments(row, "retry"))
        assert outbox.path.read_bytes() == before
        assert "private synthetic failure" not in caplog.text


async def test_not_found_is_observation_only_until_explicit_retry(tmp_path):
    with queue(tmp_path) as outbox:
        row = terminal(outbox, received=False)
        def handler(request):
            assert request.method == "GET"
            return httpx.Response(200, json=query_body(row, found=False))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            admin = OutboxAdmin(outbox, EdgeClient(base_url="https://example.invalid", client=http, device_token=token()))
            original = outbox.path.read_bytes()
            assert (await admin.apply(**arguments(row, "reconcile")))["result"] == "not_found"
            assert outbox.path.read_bytes() == original
            await admin.apply(**arguments(row, "retry"))
        assert outbox.get(row["event_id"])["state"] == "pending"
        assert outbox.get(row["event_id"])["attempts"] == 1


async def test_changed_revision_during_get_cannot_be_overwritten(tmp_path):
    with queue(tmp_path) as outbox:
        row = outbox.enqueue(event())
        async def handler(request):
            outbox.claim(row["event_id"])
            return httpx.Response(200, json=query_body(row))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            admin = OutboxAdmin(outbox, EdgeClient(base_url="https://example.invalid", client=http, device_token=token()))
            with pytest.raises(OutboxError, match="revision_conflict"):
                await admin.apply(**arguments(row, "reconcile"))
        assert outbox.get(row["event_id"])["state"] == "sending"
        assert outbox.audit() == []


def prefix(path):
    return ["--outbox", str(path / "alerts_outbox.json"), "--device-id", "device-a", "--elder-id", "1",
            "--cloud-url", "https://example.invalid"]


async def test_cli_respects_live_owner_and_read_only_queries(tmp_path, capsys):
    with queue(tmp_path) as outbox:
        row = terminal(outbox, received=False)
        before = outbox.path.read_bytes()
        assert await run(prefix(tmp_path) + ["show", "--event-id", row["event_id"]]) == 1
        assert outbox.path.read_bytes() == before
    assert await run(prefix(tmp_path) + ["list"]) == 0
    assert await run(prefix(tmp_path) + ["show", "--event-id", row["event_id"]]) == 0
    output = capsys.readouterr().out
    assert "synthetic alert" not in output
    assert before == (tmp_path / "alerts_outbox.json").read_bytes()


@pytest.mark.parametrize("problem", ["missing", "relative", "device", "elder", "cloud", "confirmation", "reason"])
async def test_cli_refuses_bad_target_or_confirmation(tmp_path, capsys, problem):
    with queue(tmp_path) as outbox:
        row = terminal(outbox, received=False)
        before = outbox.path.read_bytes()
    args = prefix(tmp_path)
    if problem == "missing":
        args[1] = str(tmp_path / "absent.json")
    elif problem == "relative":
        args[1] = "alerts_outbox.json"
    elif problem in ("device", "elder", "cloud"):
        args[{"device": 3, "elder": 5, "cloud": 7}[problem]] = {"device": "other", "elder": "2", "cloud": "https://other.invalid"}[problem]
    if problem in ("confirmation", "reason"):
        args += ["resolve", "--event-id", row["event_id"]]
    else:
        args += ["list"]
    assert await run(args) == 1
    assert before == (tmp_path / "alerts_outbox.json").read_bytes()


async def test_cli_close_failure_is_sanitized(tmp_path, capsys):
    with queue(tmp_path) as outbox:
        row = terminal(outbox, received=False)
    fake = Mock(base_url="https://example.invalid", fetch_receipt=AsyncMock(return_value=query_body(row, found=False)["data"]),
                aclose=AsyncMock(side_effect=RuntimeError("sensitive-close-text")))
    assert await run(prefix(tmp_path) + ["check", "--event-id", row["event_id"]], client_factory=lambda **kwargs: fake) == 1
    captured = capsys.readouterr()
    assert "sensitive-close-text" not in captured.out + captured.err


async def test_cli_argument_errors_never_echo_input(capsys):
    assert await run(["--private-value", "synthetic-sensitive-value"]) == 1
    captured = capsys.readouterr()
    assert "synthetic-sensitive-value" not in captured.out + captured.err


async def test_prune_during_send_preserves_current_claim_and_receipt(tmp_path):
    from datetime import datetime, timedelta, timezone
    from src.integration.edge import EdgeReporter
    now = [datetime(2026, 10, 18, tzinfo=timezone.utc)]
    with queue(tmp_path, now=lambda: now[0]) as outbox:
        old = terminal(outbox)
        now[0] += timedelta(days=31)
        # enqueue would prune automatically, so first stage new work before aging.
        now[0] -= timedelta(days=31)
        item = event()
        outbox.enqueue(item)
        now[0] += timedelta(days=31)
        def handler(request):
            assert outbox.get(item["eventId"])["state"] == "sending"
            assert outbox.prune() == 1
            return httpx.Response(200, json=receipt(item))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = EdgeClient(base_url="https://example.invalid", client=http, device_token=token())
            assert await EdgeReporter(client, outbox=outbox).run_once() is True
        assert outbox.get(old["event_id"]) is None
        assert outbox.get(item["eventId"])["state"] == "received"
        assert len(outbox.audit()) == 1
