"""Additive DDL only on the P0-12 owned engine; never create a second engine."""
from unittest.mock import Mock

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, inspect, String, DateTime, ForeignKey, UniqueConstraint

from app.core import database
from app.models.models import AlertLog, EdgeAlertReceipt, SchemaMigration, User, Elder, Device, NotificationRecovery
from scripts.migrate_edge_alert_receipts import MigrationError, migrate, main


@pytest.fixture()
def legacy(test_database, monkeypatch, request):
    """Let the existing guarded db_state create old tables; retain its teardown."""
    original = database.Base.metadata.create_all
    tables = [table for table in database.Base.metadata.sorted_tables
              if table not in (EdgeAlertReceipt.__table__, SchemaMigration.__table__, NotificationRecovery.__table__)]
    with monkeypatch.context() as patch:
        patch.setattr(database.Base.metadata, "create_all", lambda bind: original(bind=bind, tables=tables))
        request.getfixturevalue("db_state")
    test_database.validate()
    return test_database


def run(owner):
    """Both P0-12 registration and migration target checks must permit DDL."""
    return migrate(database.engine, owner.path, validate_owner=owner.validate)


def test_old_schema_migration_is_additive_repeatable_and_preserves_history(legacy):
    with legacy.session() as session:
        alert = AlertLog(alert_type="legacy", priority="P0", content="historical", status="cancelled")
        session.add(alert)
        session.commit()
        before = [(a.id, a.alert_type, a.priority, a.content, a.status, a.created_at)
                  for a in session.query(AlertLog).all()]
    legacy.validate()
    original_tables = set(inspect(database.engine).get_table_names())
    assert "edge_alert_receipts" not in original_tables
    run(legacy)
    assert set(inspect(database.engine).get_table_names()) == original_tables | {"edge_alert_receipts", "schema_migrations", "notification_recoveries"}
    run(legacy)
    assert set(inspect(database.engine).get_table_names()) == original_tables | {"edge_alert_receipts", "schema_migrations", "notification_recoveries"}
    with legacy.session() as session:
        after = [(a.id, a.alert_type, a.priority, a.content, a.status, a.created_at)
                 for a in session.query(AlertLog).all()]
        assert after == before
        assert session.query(EdgeAlertReceipt).count() == 0


def test_cli_dispatches_explicit_target_without_credentials(legacy, capsys):
    assert main(["--database", str(legacy.path)]) == 0
    assert "completed" in capsys.readouterr().out
    assert "edge_alert_receipts" in inspect(database.engine).get_table_names()


@pytest.mark.parametrize("failure", ["wrong_path", "external_engine", "factory", "session", "configuration"])
def test_wrong_target_fails_before_connect_or_ddl(legacy, monkeypatch, failure):
    target = legacy.path
    selected = database.engine
    with monkeypatch.context() as patch:
        connect = Mock(side_effect=AssertionError("unverified connect"))
        begin = Mock(side_effect=AssertionError("unverified DDL"))
        patch.setattr(database.engine, "connect", connect)
        patch.setattr(database.engine, "begin", begin)
        if failure == "wrong_path":
            target = target.parent / "unregistered.sqlite"
        elif failure == "external_engine":
            selected = Mock()
        elif failure == "factory":
            patch.setattr(database, "SessionLocal", Mock(kw={"bind": Mock()}))
        elif failure == "session":
            patch.setattr(database.SessionLocal, "class_", Mock(return_value=Mock(get_bind=Mock(return_value=Mock()))))
        else:
            from app.core.config import settings
            patch.setattr(settings, "DATABASE_URL", "sqlite:///unregistered.sqlite")
        with pytest.raises(MigrationError):
            migrate(selected, target)
        connect.assert_not_called()
        begin.assert_not_called()
        if failure == "external_engine":
            selected.connect.assert_not_called()
            selected.begin.assert_not_called()


def test_owner_refusal_prevents_any_database_connection(legacy, monkeypatch):
    refused = Mock(side_effect=RuntimeError("controlled owner refusal"))
    with monkeypatch.context() as patch:
        connect = Mock(side_effect=AssertionError("unverified connect"))
        patch.setattr(database.engine, "connect", connect)
        with pytest.raises(MigrationError):
            migrate(database.engine, legacy.path, validate_owner=refused)
        connect.assert_not_called()


def test_unexpected_existing_schema_is_not_rebuilt(legacy):
    corrupt = MetaData()
    Table("edge_alert_receipts", corrupt, Column("wrong", Integer, primary_key=True))
    legacy.schema(corrupt, "create_all")
    with pytest.raises(MigrationError):
        run(legacy)
    assert [c["name"] for c in inspect(database.engine).get_columns("edge_alert_receipts")] == ["wrong"]


def test_partial_ddl_failure_keeps_created_table_and_can_be_rechecked(legacy, monkeypatch):
    original = EdgeAlertReceipt.__table__.create

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("controlled interruption after DDL")

    with monkeypatch.context() as patch:
        patch.setattr(EdgeAlertReceipt.__table__, "create", interrupted)
        with pytest.raises(MigrationError):
            run(legacy)
    assert "edge_alert_receipts" in inspect(database.engine).get_table_names()
    run(legacy)  # Inspect the retained valid table, never remove/rebuild it.
    assert legacy.root.exists()


def old_receipt_schema(owner):
    """Reproduce the previous seven-column schema on the registered engine."""
    from datetime import datetime, timezone
    from uuid import uuid4
    metadata = MetaData()
    for model in (User, Elder, Device, AlertLog):
        model.__table__.to_metadata(metadata)
    receipt = Table("edge_alert_receipts", metadata,
                    Column("id", Integer, primary_key=True),
                    Column("device_id", String(64), ForeignKey("edge_devices.device_id"), nullable=False),
                    Column("event_id", String(36), nullable=False),
                    Column("elder_id", Integer, ForeignKey("elders.id"), nullable=False),
                    Column("payload_hash", String(64), nullable=False),
                    Column("alert_id", Integer, ForeignKey("alert_logs.id"), nullable=False, unique=True),
                    Column("received_at", DateTime, nullable=False),
                    UniqueConstraint("device_id", "event_id", name="uq_edge_alert_event"))
    owner.schema(metadata, "create_all")
    with owner.session() as session:
        user = User(username="historical", role="family")
        session.add(user)
        session.flush()
        elder = Elder(owner_id=user.id, name="synthetic")
        session.add(elder)
        session.flush()
        device = Device(device_id="historical-device", elder_id=elder.id, owner_id=user.id)
        alert = AlertLog(elder_id=elder.id, alert_type="help", priority="P0", content="historical")
        session.add_all([device, alert])
        session.flush()
        identity = str(uuid4())
        session.execute(receipt.insert().values(device_id=device.device_id, event_id=identity,
                                               elder_id=elder.id, payload_hash="0" * 64,
                                               alert_id=alert.id, received_at=datetime.now(timezone.utc)))
        session.commit()
        return identity, alert.id


def test_old_receipt_remains_unknown_without_synthetic_historical_task(legacy):
    identity, alert_id = old_receipt_schema(legacy)
    run(legacy)
    run(legacy)
    with legacy.session() as session:
        row = session.query(EdgeAlertReceipt).one()
        assert row.event_id == identity and row.alert_id == alert_id
        assert row.notification_state == "unknown"
        assert row.notification_attempts == 0
        assert row.notification_last_attempt_at is None
        assert row.notification_updated_at is None
        assert session.query(AlertLog).one().content == "historical"
        assert {row.version for row in session.query(SchemaMigration).all()} == {"edge-alert-delivery-v1", "notification-recovery-v1"}


def test_partial_add_columns_is_preserved_and_resumable(legacy, monkeypatch):
    from sqlalchemy.engine import Connection
    identity, alert_id = old_receipt_schema(legacy)
    original = Connection.exec_driver_sql

    def interrupt(connection, statement, *args, **kwargs):
        if statement.startswith("ALTER TABLE") and "notification_updated_at" in statement:
            raise OSError("controlled interruption during additive migration")
        return original(connection, statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "exec_driver_sql", interrupt)
        with pytest.raises(MigrationError):
            run(legacy)
    columns = {c["name"] for c in inspect(database.engine).get_columns("edge_alert_receipts")}
    assert "notification_state" in columns
    assert "notification_updated_at" not in columns
    run(legacy)
    with legacy.session() as session:
        assert session.query(EdgeAlertReceipt).one().event_id == identity
        assert session.get(AlertLog, alert_id).content == "historical"


def test_previous_delivery_schema_adds_revision_and_audit_without_changing_evidence(legacy):
    """Model the preceding release, not only the original seven-column table."""
    metadata = MetaData()
    for model in (User, Elder, Device, AlertLog):
        model.__table__.to_metadata(metadata)
    current = EdgeAlertReceipt.__table__
    old = Table(current.name, metadata,
                *(Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable,
                         server_default=c.server_default.arg if c.server_default else None)
                  for c in current.columns if c.name != "notification_revision"),
                UniqueConstraint("device_id", "event_id"), UniqueConstraint("alert_id"))
    # Use the same declared foreign keys as the previous production structure.
    from sqlalchemy import ForeignKeyConstraint, Index
    for name, target in (("device_id", "edge_devices.device_id"), ("elder_id", "elders.id"), ("alert_id", "alert_logs.id")):
        old.append_constraint(ForeignKeyConstraint([name], [target]))
    Index("ix_edge_receipt_delivery", old.c.notification_state, old.c.notification_next_at)
    legacy.schema(metadata, "create_all")
    run(legacy)
    assert "notification_revision" in {c["name"] for c in inspect(database.engine).get_columns(current.name)}
    assert "notification_recoveries" in inspect(database.engine).get_table_names()
    run(legacy)


def test_malformed_audit_table_is_preserved_and_refused(legacy):
    malformed = MetaData()
    Table("notification_recoveries", malformed, Column("wrong", Integer, primary_key=True))
    legacy.schema(malformed, "create_all")
    with pytest.raises(MigrationError):
        run(legacy)
    assert [c["name"] for c in inspect(database.engine).get_columns("notification_recoveries")] == ["wrong"]
    assert "edge_alert_receipts" not in inspect(database.engine).get_table_names()


def test_interrupted_audit_create_preserves_structure_and_resumes(legacy, monkeypatch):
    original = NotificationRecovery.__table__.create

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("synthetic interruption after audit DDL")

    with monkeypatch.context() as patch:
        patch.setattr(NotificationRecovery.__table__, "create", interrupted)
        with pytest.raises(MigrationError):
            run(legacy)
    assert "notification_recoveries" in inspect(database.engine).get_table_names()
    run(legacy)
    with legacy.session() as session:
        assert session.query(NotificationRecovery).count() == 0
        assert session.query(SchemaMigration).count() == 2
