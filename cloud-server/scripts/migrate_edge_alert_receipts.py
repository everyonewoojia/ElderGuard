"""Explicit additive migration; trusted local administrator only, no startup DDL.

From cloud-server: python -B -m scripts.migrate_edge_alert_receipts --database ABSOLUTE_FILE
DATABASE_URL must use that same absolute SQLite file. Preserve partial results on
failure and rerun inspection; this command never drops, rebuilds or deletes.
"""
import argparse
from pathlib import Path
import stat
import sys

from sqlalchemy import inspect, select
from sqlalchemy.engine import make_url

from app.core import database
from app.core.config import settings
from app.models.models import EdgeAlertReceipt, SchemaMigration, NotificationRecovery
from app.services.device_auth import validate_database_target

_ENGINE = database.engine
_FACTORY = database.SessionLocal
VERSION = "edge-alert-delivery-v1"
RECOVERY_VERSION = "notification-recovery-v1"
RECEIPT_COLUMNS = {"id", "device_id", "event_id", "elder_id", "payload_hash", "alert_id", "received_at"}


class MigrationError(RuntimeError):
    """Credential-free refusal; partial schema is preserved for inspection."""


def _path_identity(path: Path) -> tuple:
    """Pin the explicit file and all parent identities, rejecting redirected paths."""
    result = []
    for item in [path, *path.parents]:
        info = item.lstat()
        if (not info.st_ino or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
                or (item == path and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))
                or (item != path and not stat.S_ISDIR(info.st_mode))):
            raise MigrationError("Database filesystem identity unavailable")
        result.append((info.st_dev, info.st_ino))
    return tuple(result)


def _validate(engine, expected_path) -> tuple[Path, tuple]:
    """Do not connect until config, exact objects and an actual Session agree."""
    if (engine is not _ENGINE or database.engine is not _ENGINE
            or database.SessionLocal is not _FACTORY):
        raise MigrationError("Application database objects changed")
    target = validate_database_target(engine, expected_path)
    if (engine.url.query or Path(engine.url.database) != target
            or not Path(engine.url.database).is_absolute()
            or make_url(settings.DATABASE_URL) != engine.url
            or _FACTORY.kw.get("bind") is not engine or _FACTORY.kw.get("binds")):
        raise MigrationError("Application database binding unavailable")
    with _FACTORY() as session:
        if session.get_bind() is not engine:
            raise MigrationError("Actual Session binding unavailable")
    return target, _path_identity(target)


def _check_schema(connection, *, partial=False) -> None:
    """Require the exact new columns, keys, foreign keys and unique constraints."""
    inspector = inspect(connection)
    table = EdgeAlertReceipt.__table__
    columns = inspector.get_columns(table.name)
    actual = {c["name"]: (str(c["type"]).upper(), c["nullable"], c["default"]) for c in columns}
    expected = {c.name: (str(c.type).upper(), c.nullable,
                         str(c.server_default.arg) if c.server_default is not None else None) for c in table.columns}
    matches = (RECEIPT_COLUMNS.issubset(actual) and set(actual).issubset(expected)
               and all(value == expected[name] for name, value in actual.items())) if partial else actual == expected
    if not matches or inspector.get_pk_constraint(table.name)["constrained_columns"] != ["id"]:
        raise MigrationError("Existing receipt structure differs; no repair performed")
    unique = {tuple(item["column_names"]) for item in inspector.get_unique_constraints(table.name)}
    unique.update(tuple(item["column_names"]) for item in inspector.get_indexes(table.name) if item["unique"])
    expected_unique = {("device_id", "event_id"), ("alert_id",)}
    foreign = {(tuple(item["constrained_columns"]), item["referred_table"], tuple(item["referred_columns"]))
               for item in inspector.get_foreign_keys(table.name)}
    expected_foreign = {(("device_id",), "edge_devices", ("device_id",)),
                        (("elder_id",), "elders", ("id",)),
                        (("alert_id",), "alert_logs", ("id",))}
    if unique != expected_unique or foreign != expected_foreign or inspector.get_check_constraints(table.name):
        raise MigrationError("Existing receipt constraints differ; no repair performed")
    indexes = {item["name"]: tuple(item["column_names"]) for item in inspector.get_indexes(table.name) if not item["unique"]}
    expected_indexes = {item.name: tuple(c.name for c in item.columns) for item in table.indexes}
    if any(name not in expected_indexes or columns != expected_indexes[name] for name, columns in indexes.items()):
        raise MigrationError("Existing receipt index differs")
    if not partial and indexes != expected_indexes:
        raise MigrationError("Receipt index unavailable")


def _check_version_table(connection):
    """Reject an unrelated table rather than interpreting it as migration history."""
    inspector = inspect(connection)
    columns = {c["name"]: (str(c["type"]).upper(), c["nullable"]) for c in inspector.get_columns("schema_migrations")}
    expected = {c.name: (str(c.type).upper(), c.nullable) for c in SchemaMigration.__table__.columns}
    if columns != expected or inspector.get_pk_constraint("schema_migrations")["constrained_columns"] != ["version"]:
        raise MigrationError("Migration version structure differs")


def _check_audit_table(connection) -> None:
    """Existing audit storage is never repaired or rebuilt."""
    inspector = inspect(connection)
    table = NotificationRecovery.__table__
    actual = {c["name"]: (str(c["type"]).upper(), c["nullable"], c["default"])
              for c in inspector.get_columns(table.name)}
    expected = {c.name: (str(c.type).upper(), c.nullable, None) for c in table.columns}
    keys = inspector.get_pk_constraint(table.name)["constrained_columns"]
    indexes = {(item["name"], tuple(item["column_names"]), item["unique"])
               for item in inspector.get_indexes(table.name)}
    expected_indexes = {(item.name, tuple(c.name for c in item.columns), 0) for item in table.indexes}
    foreign = {(tuple(item["constrained_columns"]), item["referred_table"], tuple(item["referred_columns"]))
               for item in inspector.get_foreign_keys(table.name)}
    if (actual != expected or keys != ["operation_id"] or indexes != expected_indexes
            or foreign != {(("receipt_id",), "edge_alert_receipts", ("id",))}
            or inspector.get_unique_constraints(table.name) or inspector.get_check_constraints(table.name)):
        raise MigrationError("Recovery audit structure differs")


def migrate(engine, expected_path, *, validate_owner=None) -> None:
    """Add receipt notification fields/version; tests supply P0-12 validation.

    A local administrator controls application configuration and the explicit
    target's parent ACL. Path checks do not defeat a hostile same-account actor.
    No engines, Sessions bound elsewhere, cleanup or rollback-by-deletion exist.
    """
    try:
        if validate_owner is not None:
            validate_owner()
        target, identity = _validate(engine, expected_path)
        with engine.begin() as connection:
            if validate_owner is not None:
                validate_owner()
            if _validate(engine, target) != (target, identity):
                raise MigrationError("Database identity changed")
            existing = set(inspect(connection).get_table_names())
            if not {"users", "elders", "edge_devices", "alert_logs"}.issubset(existing):
                raise MigrationError("Existing application tables required")
            if "schema_migrations" in existing:
                _check_version_table(connection)
            if "notification_recoveries" in existing:
                _check_audit_table(connection)
            if "edge_alert_receipts" not in existing:
                EdgeAlertReceipt.__table__.create(connection)
            else:
                _check_schema(connection, partial=True)
                present = {c["name"] for c in inspect(connection).get_columns("edge_alert_receipts")}
                for column in EdgeAlertReceipt.__table__.columns:
                    if column.name in present:
                        continue
                    if validate_owner is not None:
                        validate_owner()
                    if _validate(engine, target) != (target, identity):
                        raise MigrationError("Database identity changed")
                    # Identifiers and defaults come exclusively from declared
                    # metadata, never CLI or client input. No rebuild/copy/drop.
                    definition = f"{column.name} {column.type.compile(dialect=connection.dialect)}"
                    if not column.nullable:
                        definition += " NOT NULL"
                    if column.server_default is not None:
                        definition += f" DEFAULT {column.server_default.arg}"
                    connection.exec_driver_sql(f"ALTER TABLE edge_alert_receipts ADD COLUMN {definition}")
                for index in EdgeAlertReceipt.__table__.indexes:
                    index.create(connection, checkfirst=True)
            _check_schema(connection)
            if validate_owner is not None:
                validate_owner()
            if _validate(engine, target) != (target, identity):
                raise MigrationError("Database identity changed")
            NotificationRecovery.__table__.create(connection, checkfirst=True)
            _check_audit_table(connection)
            SchemaMigration.__table__.create(connection, checkfirst=True)
            _check_version_table(connection)
            versions = SchemaMigration.__table__
            for version in (VERSION, RECOVERY_VERSION):
                if connection.execute(select(versions.c.version).where(versions.c.version == version)).first() is None:
                    connection.execute(versions.insert().values(version=version))
            if _path_identity(target) != identity:
                raise MigrationError("Database identity changed")
    except Exception:
        # SQLite may retain DDL after an interruption. Never delete to recover.
        raise MigrationError("Migration refused or incomplete; preserve database and inspect schema") from None


def main(argv=None) -> int:
    """An explicit local command, never imported into application startup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        migrate(database.engine, args.database)
        print("Receipt migration completed; existing alerts unchanged.")
        return 0
    except MigrationError:
        print("Migration refused or incomplete; preserve database and inspect schema.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
