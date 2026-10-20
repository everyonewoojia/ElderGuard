"""Database safety regressions; unittest cases also run without conftest."""
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class DatabaseSafetyTests(unittest.TestCase):
    """Exercise refusal paths without opening any database connection."""

    def setUp(self):
        """Keep all deliberately refused objects inside a separate framework owner."""
        from db_safety import OwnedDirectory

        self.framework = OwnedDirectory(prefix="elderguard-case-")
        self.addCleanup(self.framework.close)


    def owner(self):
        """Allocate only a fresh temporary directory for this case."""
        from db_safety import TestDatabase

        owner = TestDatabase(modules={}, parent=self.framework)

        def finish():
            """Safety refusal intentionally retains resources; never override it."""
            from db_safety import CleanupState, SafetyError

            if owner.state is CleanupState.REFUSED:
                with self.assertRaises(SafetyError):
                    owner.close()
                self.assertTrue(owner._allocated_root.exists())
            else:
                owner.close()

        self.addCleanup(finish)
        return owner

    def bindings(self, owner):
        """Build engine/session spies that cannot connect to SQLite."""
        engine = Mock()
        engine.url = SimpleNamespace(
            drivername="sqlite", database=str(owner.path), query={}
        )
        factory = Mock()
        factory.kw = {"bind": engine}
        factory.return_value.get_bind.return_value = engine
        owner.bind(engine, factory)
        return engine, factory

    def test_external_environment_is_replaced_and_restored(self):
        """An external URL is never used as the test target."""
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///never-open.db"}):
            owner = self.owner()
            self.assertEqual(os.environ["DATABASE_URL"], owner.url)
            self.assertNotIn("never-open", owner.url)
            owner.close()
            self.assertEqual(os.environ["DATABASE_URL"], "sqlite:///never-open.db")

    def test_early_import_refuses_before_allocating_resources(self):
        """Cached application modules must not be reloaded or rebound."""
        from db_safety import SafetyError, TestDatabase

        with patch("db_safety.tempfile.mkdtemp") as allocate:
            with self.assertRaises(SafetyError):
                TestDatabase(modules={"app.core.database": object()})
            allocate.assert_not_called()

    def test_wrong_path_refuses_before_ddl_or_connect(self):
        """Rejected engines execute neither schema operations nor connections."""
        from db_safety import SafetyError

        owner = self.owner()
        engine, factory = self.bindings(owner)
        metadata = Mock()
        original = engine.url.database
        engine.url.database = str(owner.root.parent / "unregistered.db")
        try:
            for action in ("create_all", "drop_all"):
                with self.assertRaises(SafetyError):
                    owner.schema(metadata, action)
            metadata.assert_not_called()
            metadata.create_all.assert_not_called()
            metadata.drop_all.assert_not_called()
            engine.connect.assert_not_called()
            engine.raw_connection.assert_not_called()
        finally:
            engine.url.database = original

    def test_wrong_factory_bind_is_rejected(self):
        """Default and per-model Session binds cannot bypass the registry."""
        from db_safety import SafetyError

        owner = self.owner()
        engine, factory = self.bindings(owner)
        factory.kw = {"bind": Mock()}
        with self.assertRaises(SafetyError):
            owner.schema(Mock(), "drop_all")
        factory.kw = {"bind": engine, "binds": {object(): Mock()}}
        with self.assertRaises(SafetyError):
            owner.validate()
        factory.kw = {"bind": engine}
        engine.connect.assert_not_called()

    def test_wrong_session_bind_is_rejected(self):
        """Inspect an unconnected Session's actual bind before DDL."""
        from db_safety import SafetyError

        owner = self.owner()
        engine, factory = self.bindings(owner)
        factory.return_value.get_bind.return_value = Mock()
        metadata = Mock()
        with self.assertRaises(SafetyError):
            owner.schema(metadata, "create_all")
        metadata.create_all.assert_not_called()
        factory.return_value.close.assert_called()
        engine.connect.assert_not_called()

    def test_wrong_module_or_router_binding_is_rejected(self):
        """Reject stale module objects even if a different engine has the same URL."""
        from db_safety import SafetyError

        owner = self.owner()
        engine, factory = self.bindings(owner)
        config = SimpleNamespace(DATABASE_URL=owner.url)
        dependency = object()
        database = SimpleNamespace(engine=engine, SessionLocal=factory, get_db=dependency)
        router = SimpleNamespace(get_db=dependency, SessionLocal=factory)
        owner.validate_application(config, database, [router])
        for obj, attribute in ((database, "engine"), (database, "SessionLocal"),
                               (router, "get_db"), (router, "SessionLocal")):
            original = getattr(obj, attribute)
            setattr(obj, attribute, Mock())
            try:
                with self.assertRaises(SafetyError):
                    owner.validate_application(config, database, [router])
            finally:
                setattr(obj, attribute, original)
        engine.connect.assert_not_called()

    def test_file_cleanup_failure_is_reported_and_retryable(self):
        """A failed removal restores the environment and does not hide failure."""
        owner = self.owner()
        self.bindings(owner)
        with patch("db_safety.shutil.rmtree", side_effect=PermissionError("synthetic")):
            with self.assertRaises(PermissionError):
                owner.close()
        self.assertTrue(owner._directory.current_path.exists())
        owner.close()
        self.assertFalse(owner.root.exists())

    def test_backup_rejects_unknown_source_before_copy(self):
        """Backup copying is gated by the same database identity check."""
        from db_safety import SafetyError

        owner = self.owner()
        engine, factory = self.bindings(owner)
        factory.kw = {"bind": Mock()}
        with patch("db_safety.shutil.copyfile") as copy:
            with self.assertRaises(SafetyError):
                owner.backup()
            copy.assert_not_called()
        factory.kw = {"bind": engine}

    def test_unregistered_backup_engine_is_rejected(self):
        """An arbitrary file cannot be opened as an approved backup."""
        from db_safety import SafetyError

        owner = self.owner()
        engine, _ = self.bindings(owner)
        unknown = owner.root / "not-a-backup.db"
        with self.assertRaises(SafetyError):
            owner.validate_backup_engine(engine, unknown)
        engine.connect.assert_not_called()

    def test_cleanup_disposes_before_removing_owned_directory(self):
        """Normal cleanup removes only its allocated directory and is idempotent."""
        owner = self.owner()
        engine, _ = self.bindings(owner)
        root = owner.root
        engine.dispose.side_effect = lambda: self.assertTrue(root.exists())
        owner.close()
        self.assertFalse(root.exists())
        owner.close()
        engine.dispose.assert_called_once()

    def test_handled_exception_still_cleans_resources(self):
        """An ordinary exception still closes the owned resources."""
        owner = self.owner()
        self.bindings(owner)
        try:
            raise RuntimeError("synthetic setup failure")
        except RuntimeError:
            owner.close()
        self.assertFalse(owner.root.exists())

    def test_binding_failure_permanently_refuses_cleanup(self):
        """Restoring a broken binding must not silently re-enable cleanup."""
        from db_safety import SafetyError

        for kind in ("factory", "session", "ws", "rest", "module", "config", "engine"):
            with self.subTest(kind=kind):
                owner = self.owner()
                engine, factory = self.bindings(owner)
                config = SimpleNamespace(DATABASE_URL=owner.url)
                dependency = object()
                database = SimpleNamespace(engine=engine, SessionLocal=factory, get_db=dependency)
                router = SimpleNamespace(get_db=dependency, SessionLocal=factory)
                owner.validate_application(config, database, [router])
                target, field, wrong = {
                    "factory": (factory, "kw", {"bind": Mock()}),
                    "session": (factory.return_value.get_bind, "return_value", Mock()),
                    "ws": (router, "SessionLocal", Mock()),
                    "rest": (router, "get_db", Mock()),
                    "module": (database, "engine", Mock()),
                    "config": (config, "DATABASE_URL", "sqlite:///never-open.db"),
                    "engine": (engine.url, "database", str(owner.root.parent / "unknown.db")),
                }[kind]
                original = getattr(target, field)
                setattr(target, field, wrong)
                with self.assertRaises(SafetyError):
                    owner.validate_application(config, database, [router])
                setattr(target, field, original)
                with patch("db_safety.shutil.rmtree") as remove:
                    with self.assertRaises(SafetyError) as failure:
                        owner.close()
                    self.assertIn(str(owner.root), str(failure.exception))
                    remove.assert_not_called()
                engine.dispose.assert_not_called()
                self.assertEqual(owner.state.value, "refused")

    def test_close_rechecks_live_ws_binding(self):
        """Cleanup must revalidate saved application references even without a prior check."""
        from db_safety import SafetyError

        owner = self.owner()
        engine, factory = self.bindings(owner)
        dependency = object()
        database = SimpleNamespace(engine=engine, SessionLocal=factory, get_db=dependency)
        router = SimpleNamespace(SessionLocal=factory)
        owner.validate_application(SimpleNamespace(DATABASE_URL=owner.url), database, [router])
        router.SessionLocal = Mock()
        with patch("db_safety.shutil.rmtree") as remove:
            with self.assertRaises(SafetyError):
                owner.close()
            remove.assert_not_called()
        engine.dispose.assert_not_called()

    def test_actual_session_failure_latches_refusal(self):
        """A second Session differing from the validation probe also blocks cleanup."""
        from db_safety import SafetyError

        owner = self.owner()
        engine, factory = self.bindings(owner)
        checked = Mock()
        checked.get_bind.return_value = engine
        wrong = Mock()
        wrong.get_bind.return_value = Mock()
        factory.side_effect = [checked, wrong]
        with self.assertRaises(SafetyError):
            with owner.session():
                self.fail("An incorrectly bound Session must never be yielded")
        factory.side_effect = None
        with self.assertRaises(SafetyError):
            owner.close()
        engine.dispose.assert_not_called()

    def test_removed_ws_factory_refuses_cleanup(self):
        """A previously registered factory becoming absent also fails closed."""
        from db_safety import SafetyError

        owner = self.owner()
        engine, factory = self.bindings(owner)
        database = SimpleNamespace(engine=engine, SessionLocal=factory, get_db=object())
        router = SimpleNamespace(SessionLocal=factory)
        owner.validate_application(SimpleNamespace(DATABASE_URL=owner.url), database, [router])
        del router.SessionLocal
        with patch("db_safety.shutil.rmtree") as remove:
            with self.assertRaises(SafetyError):
                owner.close()
            remove.assert_not_called()
        engine.dispose.assert_not_called()

    def test_missing_registered_engine_refuses_cleanup(self):
        """Losing the live engine reference cannot turn a bound owner into an unbound one."""
        from db_safety import SafetyError

        owner = self.owner()
        engine, _ = self.bindings(owner)
        owner._engine = None
        with patch("db_safety.shutil.rmtree") as remove:
            with self.assertRaises(SafetyError):
                owner.close()
            remove.assert_not_called()
        engine.dispose.assert_not_called()

    def test_replaced_directory_is_retained(self):
        """A different ordinary directory at the original path is never removed."""
        from db_safety import SafetyError

        owner = self.owner()
        root = owner.root
        moved = root.with_name(root.name + "-moved")
        self.assertEqual(moved.parent.resolve(), root.parent.resolve())
        self.assertFalse(moved.exists())
        root.rename(moved)
        root.mkdir()
        marker = root / "replacement-marker.txt"
        marker.write_text("synthetic", encoding="utf-8")
        with patch("db_safety.shutil.rmtree") as remove:
            with self.assertRaises(SafetyError):
                owner.close()
            remove.assert_not_called()
        self.assertTrue(marker.exists())
        self.assertTrue(moved.exists())

    def test_link_or_changed_root_refuses_cleanup(self):
        """Both link redirection and an altered root are refused before deletion."""
        from db_safety import SafetyError

        owner = self.owner()
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(SafetyError):
                owner.close()
        other = self.owner()
        other.root = other.root.parent / "never-owned-directory"
        with patch("db_safety.shutil.rmtree") as remove:
            with self.assertRaises(SafetyError):
                other.close()
            remove.assert_not_called()

    def test_unreliable_directory_identity_fails_closed(self):
        """A platform without reliable file IDs cannot authorize cleanup."""
        from db_safety import SafetyError

        owner = self.owner()
        with patch.object(Path, "lstat", return_value=SimpleNamespace(st_ino=0, st_dev=0)):
            with patch("db_safety.shutil.rmtree") as remove:
                with self.assertRaises(SafetyError):
                    owner.close()
                remove.assert_not_called()

    def test_environment_restore_does_not_overwrite_later_user_change(self):
        """Environment restoration is independent and compare-and-restore only."""
        owner = self.owner()
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///later-user-value.db"}):
            owner.restore_environment()
            self.assertEqual(os.environ["DATABASE_URL"], "sqlite:///later-user-value.db")
        owner.close()

    def test_cleanup_failure_preserves_resources_and_restores_environment(self):
        """Never remove files after a failed engine identity check."""
        from db_safety import SafetyError

        previous = os.environ.get("DATABASE_URL")
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///never-open.db"}):
            owner = self.owner()
            engine, _ = self.bindings(owner)
            original = engine.url.database
            engine.url.database = str(owner.root.parent / "unknown.db")
            try:
                with patch("db_safety.shutil.rmtree") as remove:
                    with self.assertRaises(SafetyError):
                        owner.close()
                    remove.assert_not_called()
                self.assertEqual(os.environ["DATABASE_URL"], "sqlite:///never-open.db")
                engine.dispose.assert_not_called()
            finally:
                engine.url.database = original
        with self.assertRaises(SafetyError):
            owner.close()
        self.assertTrue(owner.root.exists())
        self.assertEqual(os.environ.get("DATABASE_URL"), previous)


class SandboxOwnershipTests(unittest.TestCase):
    """Framework ownership is independent from the database refusal boundary."""

    def sandbox(self):
        """Register a fresh outer owner, never adopt an existing directory."""
        from db_safety import OwnedDirectory

        outer = OwnedDirectory(prefix="elderguard-framework-")
        self.addCleanup(outer.close)
        return outer

    def test_framework_cleans_refused_child_after_assertions(self):
        """A database refusal cannot authorize deletion, but its sandbox owner can."""
        from db_safety import SafetyError, TestDatabase

        outer = self.sandbox()
        owner = TestDatabase(modules={}, parent=outer)
        owner.refuse("synthetic binding failure")
        with self.assertRaises(SafetyError):
            owner.close()
        self.assertTrue(owner.root.exists())
        outer.close()
        self.assertFalse(outer.root.exists())

    def test_outer_replacement_is_not_deleted(self):
        """Framework cleanup must reject its own replaced directory."""
        from db_safety import OwnedDirectory, SafetyError

        parent = self.sandbox()
        outer = OwnedDirectory(parent=parent)
        moved = outer.root.with_name(outer.root.name + "-moved")
        self.assertEqual(moved.parent, parent.root)
        outer.root.rename(moved)
        outer.root.mkdir()
        marker = outer.root / "replacement.txt"
        marker.write_text("synthetic", encoding="utf-8")
        with patch("db_safety.shutil.rmtree") as remove:
            with self.assertRaises(SafetyError):
                outer.close()
            remove.assert_not_called()
        self.assertTrue(marker.exists())
        self.assertTrue(moved.exists())

    def test_outer_link_and_parent_change_are_refused(self):
        """A link or changed parent cannot authorize framework cleanup."""
        from db_safety import OwnedDirectory, SafetyError

        parent = self.sandbox()
        outer = OwnedDirectory(parent=parent)
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(SafetyError):
                outer.close()
        self.assertTrue(outer.root.exists())

    def test_rename_race_never_deletes_replacement(self):
        """Replace the source at rename time; the moved replacement must be retained."""
        from db_safety import OwnedDirectory, SafetyError

        parent = self.sandbox()
        child = OwnedDirectory(parent=parent)
        original = child.root
        saved = original.with_name(original.name + "-original")
        rename = Path.rename

        def raced(source, destination):
            """Inject a replacement inside the sandbox immediately before rename."""
            if source == original:
                self.assertEqual(saved.parent, parent.root)
                self.assertFalse(saved.exists())
                rename(source, saved)
                source.mkdir()
                (source / "replacement.txt").write_text("synthetic", encoding="utf-8")
            return rename(source, destination)

        with patch.object(Path, "rename", raced), patch("db_safety.shutil.rmtree") as remove:
            with self.assertRaises(SafetyError) as failure:
                child.close()
            remove.assert_not_called()
        self.assertIn(str(original), str(failure.exception))
        self.assertIn(str(child.current_path), str(failure.exception))
        self.assertTrue((child.current_path / "replacement.txt").exists())
        self.assertTrue(saved.exists())

    def test_existing_quarantine_target_is_never_overwritten(self):
        """A preexisting destination is retained and no fallback is attempted."""
        from db_safety import OwnedDirectory, SafetyError

        parent = self.sandbox()
        child = OwnedDirectory(parent=parent)
        target = child.root.with_name("occupied-target")
        target.mkdir()
        with patch.object(child, "_quarantine_target", return_value=target):
            with patch.object(Path, "rename") as rename:
                with self.assertRaises(SafetyError):
                    child.close()
                rename.assert_not_called()
        self.assertTrue(target.exists())
        self.assertTrue(child.root.exists())

    def test_parent_redirection_refuses_framework_cleanup(self):
        """A changed parent ownership path cannot authorize a rename or deletion."""
        from db_safety import OwnedDirectory, SafetyError

        parent = self.sandbox()
        child = OwnedDirectory(parent=parent)
        child._parent_path = parent.root / "never-owned-parent"
        with patch.object(Path, "rename") as rename, patch("db_safety.shutil.rmtree") as remove:
            with self.assertRaises(SafetyError):
                child.close()
            rename.assert_not_called()
            remove.assert_not_called()
        self.assertTrue(child.root.exists())

    def test_rename_error_never_falls_back_to_copy(self):
        """Cross-volume or sharing errors retain the source without fallback."""
        from db_safety import OwnedDirectory

        parent = self.sandbox()
        child = OwnedDirectory(parent=parent)
        with patch.object(Path, "rename", side_effect=OSError("synthetic rename error")):
            with patch("db_safety.shutil.copytree") as copy, patch("db_safety.shutil.rmtree") as remove:
                with self.assertRaises(OSError):
                    child.close()
                copy.assert_not_called()
                remove.assert_not_called()
        self.assertTrue(child.root.exists())

    def test_normal_quarantine_cleanup_is_idempotent(self):
        """Only the owned child is moved and deleted; its sibling survives."""
        from db_safety import OwnedDirectory

        parent = self.sandbox()
        child = OwnedDirectory(parent=parent)
        sibling = parent.root / "sibling.txt"
        sibling.write_text("synthetic", encoding="utf-8")
        child.close()
        self.assertFalse(child.root.exists())
        self.assertFalse(child.current_path.exists())
        self.assertTrue(sibling.exists())
        child.close()

    def test_cleanup_failure_reports_current_owned_path(self):
        """An I/O failure retains quarantine; retry validates the same object."""
        from db_safety import OwnedDirectory

        parent = self.sandbox()
        child = OwnedDirectory(parent=parent)
        with patch("db_safety.shutil.rmtree", side_effect=PermissionError("synthetic")):
            with self.assertRaises(PermissionError):
                child.close()
        self.assertTrue(child.current_path.exists())
        self.assertIn(str(child.current_path), child.failure_reason)
        child.close()
        self.assertFalse(child.current_path.exists())


def test_live_bindings_and_external_url_replacement(test_database):
    """The actual application has one original engine and checked factory."""
    from app.core import database
    from app.core.config import settings
    from app.routers import ws

    assert os.environ["DATABASE_URL"] == test_database.url == settings.DATABASE_URL
    assert Path(database.engine.url.database).resolve() == test_database.path
    assert ws.SessionLocal is database.SessionLocal
    with test_database.session() as session:
        assert session.get_bind() is database.engine


def test_rest_ws_and_direct_session_share_database(auth, db, test_database):
    """A direct-session record is visible through REST and WS replay."""
    from app.models.models import AlertLog, Elder, User

    auth["register"]("isolation-family", None)
    token = auth["login"]("isolation-family")
    owner = db.query(User).filter_by(username="isolation-family").one()
    elder = Elder(owner_id=owner.id, name="synthetic elder")
    db.add(elder)
    db.commit()
    alert = AlertLog(elder_id=elder.id, alert_type="help", priority="P0")
    db.add(alert)
    db.commit()
    result = auth["client"].get(
        "/api/v1/alerts", headers={"Authorization": f"Bearer {token}"}
    )
    assert result.status_code == 200
    assert [item["id"] for item in result.json()] == [alert.id]
    with auth["client"].websocket_connect(f"/api/v1/ws/alerts?token={token}") as ws:
        assert ws.receive_json()["data"]["id"] == alert.id
    assert db.get_bind().url.database == str(test_database.path).replace("\\", "/")


def test_backup_uses_registered_temporary_source(db, test_database):
    """Copy and read back only a newly created and registered backup file."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.models.models import User

    db.add(User(username="backup-isolation", password_hash="synthetic hash"))
    db.commit()
    backup = test_database.backup()
    assert backup.is_relative_to(test_database.root)
    engine = create_engine(f"sqlite:///{backup.as_posix()}")
    try:
        test_database.validate_backup_engine(engine, backup)
        with Session(engine) as restored:
            assert restored.query(User).filter_by(username="backup-isolation").one()
    finally:
        engine.dispose()


def test_controlled_lifecycle_subprocesses(test_database):
    """Exercise real fixture teardown, external URL replacement and error exits."""
    import json
    import subprocess
    import sys
    import tempfile

    tests_dir = Path(__file__).resolve().parent
    for scenario in ("normal", "test_failure", "collection_failure"):
        directory = Path(tempfile.mkdtemp(prefix="child-", dir=test_database.root))
        target = directory / "test_controlled.py"
        if scenario == "normal":
            source = '''
from app.models.models import User
def test_first(db, auth):
    assert db.query(User).count() == 0
    db.add(User(username="child-marker", password_hash="synthetic"))
    db.commit()
def test_second(db):
    assert db.query(User).count() == 0
'''
        elif scenario == "test_failure":
            source = 'def test_failure(db, client):\n    raise RuntimeError("synthetic failure")\n'
        else:
            source = 'raise RuntimeError("synthetic collection failure")\n'
        target.write_text(source, encoding="utf-8")
        forbidden = directory / "external-never-open.sqlite3"
        driver = '''
import json, os, pathlib, sys
sys.path.insert(0, sys.argv[1])
external = os.environ["DATABASE_URL"]
import conftest as bootstrap
root = bootstrap._TEST_DATABASE.root
outer = bootstrap._TEST_DATABASE._owned_parent.root
assert root.is_relative_to(pathlib.Path(sys.argv[2]).parent)
counts = {"create_all": 0, "drop_all": 0}
original = bootstrap._TEST_DATABASE.schema
def counted(metadata, action):
    counts[action] += 1
    return original(metadata, action)
bootstrap._TEST_DATABASE.schema = counted
import pytest
code = pytest.main([sys.argv[2], "--confcutdir", str(pathlib.Path(sys.argv[2]).parent),
                   "-p", "conftest", "-p", "no:cacheprovider", "-q"])
print("ISOLATION_RESULT=" + json.dumps({"code": int(code), "removed": not root.exists(),
      "restored": os.environ.get("DATABASE_URL") == external, "counts": counts,
      "outer_removed": not outer.exists()}))
'''
        env = os.environ.copy()
        env["DATABASE_URL"] = f"sqlite:///{forbidden.as_posix()}"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        env["TEMP"] = env["TMP"] = str(directory)
        result = subprocess.run(
            [sys.executable, "-B", "-c", driver, str(tests_dir), str(target)],
            env=env, capture_output=True, text=True, timeout=45,
        )
        assert result.returncode == 0, result.stderr
        report = next(line.split("=", 1)[1] for line in result.stdout.splitlines()
                      if line.startswith("ISOLATION_RESULT="))
        expected = {"normal": 0, "test_failure": 1, "collection_failure": 2}[scenario]
        resets = {"normal": 2, "test_failure": 1, "collection_failure": 0}[scenario]
        assert json.loads(report) == {
            "code": expected, "removed": True, "restored": True,
            "counts": {"create_all": resets, "drop_all": resets},
            "outer_removed": True,
        }
        assert not forbidden.exists()


def test_controlled_bootstrap_refusal_and_import_failure(test_database):
    """Early-import refusal and import failure never connect or execute DDL."""
    import json
    import subprocess
    import sys

    driver = '''
import builtins, json, os, pathlib, sys, types
from unittest.mock import Mock, patch
sys.path.insert(0, sys.argv[1])
import db_safety
import sqlalchemy
allocated = []
original_allocate = db_safety.tempfile.mkdtemp
def allocate(*args, **kwargs):
    value = original_allocate(*args, **kwargs)
    allocated.append(pathlib.Path(value))
    return value
original_import = builtins.__import__
def import_failure(name, *args, **kwargs):
    if name == "app.main":
        raise ImportError("synthetic bootstrap failure")
    return original_import(name, *args, **kwargs)
external = os.environ["DATABASE_URL"]
if sys.argv[2] == "early":
    sys.modules["app.core.database"] = types.ModuleType("app.core.database")
with patch("db_safety.tempfile.mkdtemp", side_effect=allocate), \
     patch("sqlite3.connect") as connect, \
     patch.object(sqlalchemy.MetaData, "create_all") as create, \
     patch.object(sqlalchemy.MetaData, "drop_all") as drop, \
     patch("builtins.__import__", side_effect=import_failure):
    try:
        import conftest
    except (db_safety.SafetyError, ImportError):
        pass
    else:
        raise AssertionError("bootstrap must fail")
    assert connect.call_count == create.call_count == drop.call_count == 0
assert len(allocated) == (0 if sys.argv[2] == "early" else 3)
assert all(root.is_relative_to(pathlib.Path(os.environ["TEMP"])) for root in allocated)
assert all(not root.exists() for root in allocated)
assert os.environ["DATABASE_URL"] == external
print("BOOTSTRAP_SAFE")
'''
    for scenario in ("early", "import_failure"):
        import tempfile

        directory = Path(tempfile.mkdtemp(prefix="bootstrap-", dir=test_database.root))
        env = os.environ.copy()
        env["DATABASE_URL"] = "sqlite:///external-never-open.sqlite3"
        env["TEMP"] = env["TMP"] = str(directory)
        result = subprocess.run(
            [sys.executable, "-B", "-c", driver, str(Path(__file__).parent), scenario],
            env=env, cwd=directory, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "BOOTSTRAP_SAFE" in result.stdout


if __name__ == "__main__":
    unittest.main()
