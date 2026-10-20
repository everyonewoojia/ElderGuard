"""CLI contract and refusal before connections, with P0-12 temporary ownership."""
import json
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.core import database
from app.core.config import settings
from scripts.manage_notifications import main
from test_notification_recovery import unknown, pending, identity, device_key  # noqa: F401


def test_query_and_status_do_not_send_or_mutate(unknown, test_database, capsys, monkeypatch):
    from app.services.ws_manager import manager
    forbidden = Mock(side_effect=AssertionError("CLI must never send"))
    monkeypatch.setattr(manager, "push_to_user", forbidden)
    prefix = ["--database", str(test_database.path)]
    assert main(prefix + ["list-unknown"], validate_owner=test_database.validate) == 0
    assert json.loads(capsys.readouterr().out)[0]["receipt_id"] == unknown["receipt_id"]
    assert main(prefix + ["show", "--receipt-id", str(unknown["receipt_id"])], validate_owner=test_database.validate) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["audit"] == []
    assert "content" not in view["records"][0]
    assert main(prefix + ["status"], validate_owner=test_database.validate) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["unknown"] == 1
    assert status["worker"]["state"] == "unavailable"
    forbidden.assert_not_called()


def test_cli_requires_explicit_duplicate_authorization(unknown, test_database, capsys):
    args = ["--database", str(test_database.path), "retry", "--receipt-id", str(unknown["receipt_id"]),
            "--device-id", unknown["device_id"], "--elder-id", str(unknown["elder_id"]),
            "--expected-revision", "0", "--operation-id", str(uuid4()), "--operator", "local-admin",
            "--reason", "duplicate_risk_accepted"]
    assert main(args, validate_owner=test_database.validate) == 1
    rejected = capsys.readouterr()
    assert rejected.out == ""
    assert "重复通知" in rejected.err
    assert main(args + ["--authorize-duplicate"], validate_owner=test_database.validate) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["result"] == "retry_authorized"
    assert "重复通知" in output.err


@pytest.mark.parametrize("problem", ["path", "factory", "engine", "session", "config"])
def test_unverified_cli_target_never_connects(unknown, test_database, monkeypatch, capsys, problem):
    target = test_database.path
    with monkeypatch.context() as patch:
        connect = Mock(side_effect=AssertionError("unverified connection"))
        patch.setattr(database.engine, "connect", connect)
        if problem == "path":
            target = test_database.root / "never-created.sqlite"
        elif problem == "factory":
            patch.setattr(database, "SessionLocal", Mock())
        elif problem == "engine":
            patch.setattr(database, "engine", Mock())
        elif problem == "session":
            patch.setattr(database.SessionLocal.class_, "get_bind", lambda self: object())
        else:
            patch.setattr(settings, "DATABASE_URL", "sqlite:///not-a-target.sqlite")
        assert main(["--database", str(target), "status"]) == 1
        connect.assert_not_called()
    assert capsys.readouterr().out == ""


def test_invalid_cli_arguments_do_not_echo_arbitrary_values(capsys):
    assert main(["--private-option", "sensitive-synthetic-value"]) == 1
    output = capsys.readouterr()
    assert "sensitive-synthetic-value" not in output.out + output.err
