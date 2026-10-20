"""Own and validate pytest SQLite resources without importing the application."""
from contextlib import contextmanager
import os
from enum import Enum
import stat
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Iterator, Mapping


class SafetyError(RuntimeError):
    """Refuse database operations whose target identity is not established."""


class CleanupState(Enum):
    """Explicit stages; REFUSED is permanent for this resource owner."""

    INITIALIZING = "initializing"
    BOUND = "bound"
    APPLICATION_VALIDATED = "application_validated"
    REFUSED = "refused"
    RESOURCES_CLOSED = "resources_closed"
    CLEANUP_FAILED = "cleanup_failed"
    CLEANED = "cleaned"


def directory_identity(path: Path) -> tuple[int, int]:
    """Require a real directory and a reliable filesystem identifier."""
    info = path.lstat()
    if not isinstance(info.st_ino, int) or info.st_ino <= 0:
        raise SafetyError("Reliable directory file ID is unavailable")
    if (not stat.S_ISDIR(info.st_mode)
            or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise SafetyError("Directory is a link, reparse point or non-directory")
    return info.st_dev, info.st_ino


class OwnedDirectory:
    """Framework ownership of newly allocated files; never adopts existing paths.

    Nested owners quarantine by same-volume rename and verify the moved object.
    The outermost owner uses checked path-based removal. Neither is atomic against
    a concurrent process with permission to modify the sandbox.
    """

    def __init__(self, parent=None, prefix: str = "elderguard-sandbox-") -> None:
        self.parent = parent
        if parent is not None:
            parent.validate()
        self._parent_path = parent.current_path if parent else Path(tempfile.gettempdir()).resolve()
        self._parent_identity = directory_identity(self._parent_path)
        self.root = Path(tempfile.mkdtemp(prefix=prefix, dir=self._parent_path)).resolve()
        self._original = self.root
        self.current_path = self.root
        self._identity = directory_identity(self.root)
        self._closed = False
        self._refused = False
        self._staging = None
        self.failure_reason = None

    def validate(self) -> None:
        """Check own identity, original parent and any quarantine container."""
        try:
            if self._closed or self._refused:
                raise SafetyError("Directory owner is closed or refused")
            if self.root != self._original:
                raise SafetyError("Original ownership path changed")
            if (self.current_path.is_symlink()
                    or self.current_path.resolve() != self.current_path
                    or directory_identity(self.current_path) != self._identity):
                raise SafetyError("Owned directory identity changed")
            if directory_identity(self._parent_path) != self._parent_identity:
                raise SafetyError("Parent directory identity changed")
            if self._parent_path.resolve() != self._parent_path:
                raise SafetyError("Parent path was redirected")
            if self.parent is not None:
                self.parent.validate()
                if self.parent.current_path != self._parent_path:
                    raise SafetyError("Parent ownership path changed")
            expected_parent = self._parent_path
            if self._staging is not None:
                holder, identity = self._staging
                if (holder.parent != self._parent_path or holder.resolve() != holder
                        or directory_identity(holder) != identity):
                    raise SafetyError("Quarantine container identity changed")
                if self.current_path != self.root:
                    expected_parent = holder
            if self.current_path.parent != expected_parent:
                raise SafetyError("Owned directory escaped its registered boundary")
        except Exception as exc:
            self._refused = True
            self.failure_reason = (f"Directory operation refused: original={self._original}; "
                                   f"current={self.current_path}; reason={exc}")
            raise SafetyError(self.failure_reason) from exc

    def _quarantine_target(self) -> Path:
        """Allocate an exclusive destination inside the verified private parent."""
        if self.parent is None:
            raise SafetyError("Quarantine requires a privately owned parent")
        self.parent.validate()
        holder = Path(tempfile.mkdtemp(prefix="cleanup-", dir=self._parent_path)).resolve()
        self._staging = holder, directory_identity(holder)
        return holder / "payload"

    def close(self) -> None:
        """Validate, quarantine nested objects, revalidate, then remove only that object."""
        if self._closed:
            return
        self.validate()
        try:
            if self.parent is not None and self.current_path == self.root:
                target = self._quarantine_target()
                self.validate()
                if (self._staging is None or target != self._staging[0] / "payload"
                        or target.exists() or target.is_symlink()):
                    raise SafetyError("Quarantine destination is occupied or unregistered")
                if os.name != "nt":
                    raise SafetyError("Non-overwriting directory rename is only validated on Windows")
                self.current_path.rename(target)
                self.current_path = target
                self.validate()
            self.validate()
            shutil.rmtree(self.current_path)
            if self._staging is not None:
                holder, identity = self._staging
                self.parent.validate()
                if holder.resolve() != holder or directory_identity(holder) != identity:
                    raise SafetyError("Quarantine container changed before removal")
                holder.rmdir()
            self._closed = True
        except Exception as exc:
            if isinstance(exc, SafetyError):
                self._refused = True
            self.failure_reason = (f"Directory cleanup failed: original={self._original}; "
                                   f"current={self.current_path}; reason={type(exc).__name__}: {exc}")
            if isinstance(exc, SafetyError):
                raise SafetyError(self.failure_reason) from exc
            raise


class TestDatabase:
    """One temporary SQLite file per pytest process; no SQL interception."""

    __test__ = False

    def __init__(self, modules: Mapping[str, Any] | None = None,
                 parent: OwnedDirectory | None = None) -> None:
        loaded = sys.modules if modules is None else modules
        if any(name.startswith("app.") for name in loaded):
            raise SafetyError("Application modules loaded before database isolation")
        self.state = CleanupState.INITIALIZING
        self.failure_reason = None
        self._owned_parent = OwnedDirectory() if parent is None else None
        parent = parent if parent is not None else self._owned_parent
        self._directory = OwnedDirectory(parent=parent, prefix="elderguard-pytest-")
        self.root = self._directory.root
        self._allocated_root = self.root
        self._parent = self.root.parent
        try:
            self._root_identity = self._directory_identity(self.root)
        except Exception as exc:
            self.refuse(str(exc))
            raise SafetyError(self.failure_reason) from exc
        self.path = self.root / "test.sqlite3"
        self.url = f"sqlite:///{self.path.as_posix()}"
        self._previous_url = os.environ.get("DATABASE_URL")
        self._engine = None
        self._factory = None
        self._registered = None
        self._application = None
        self._router_checks: list[tuple[Any, tuple[str, ...]]] = []
        self._dependency = None
        self._resources_closed = False
        self._backups: set[Path] = set()
        self._closed = False
        self._environment_restored = False
        os.environ["DATABASE_URL"] = self.url

    @staticmethod
    def _directory_identity(path: Path) -> tuple[int, int]:
        """Require a real directory with a nonzero filesystem file ID."""
        return directory_identity(path)

    def refuse(self, reason: str) -> None:
        """Latch refusal: restoring a binding cannot authorize later cleanup."""
        already_refused = self.state is CleanupState.REFUSED
        self.state = CleanupState.REFUSED
        if not already_refused:
            self.failure_reason = f"Cleanup refused for {self._allocated_root}: {reason}"

    def _require_not_refused(self) -> None:
        """Keep every destructive entry point closed after a failed check."""
        if self.state is CleanupState.REFUSED:
            raise SafetyError(self.failure_reason)

    def _check_root(self) -> None:
        """Require the allocated path, parent and original filesystem identity."""
        if (self.root != self._allocated_root or self.root.parent != self._parent
                or self.root.is_symlink() or self.root.resolve() != self._allocated_root):
            raise SafetyError("Temporary directory path or ownership changed")
        self._directory.validate()
        if self._directory_identity(self._directory.current_path) != self._root_identity:
            raise SafetyError("Temporary directory was moved or replaced")

    def _check_path(self, path: Path) -> None:
        """Only the exact allocated database file is a schema target."""
        self._check_root()
        if not path.is_absolute() or path != self.path:
            raise SafetyError("Database path is not the registered temporary file")
        if path.is_symlink() or path.resolve() != self.path:
            raise SafetyError("Database file identity changed")
        if path.exists() and path.stat().st_nlink != 1:
            raise SafetyError("Database file has an unexpected hard link")

    def _check_engine(self, engine: Any) -> None:
        """Inspect SQLAlchemy URL properties without opening a connection."""
        url = engine.url
        if url.drivername != "sqlite" or url.query:
            raise SafetyError("Only plain temporary SQLite file URLs are allowed")
        if not url.database:
            raise SafetyError("SQLite file path is missing")
        self._check_path(Path(url.database))

    def bind(self, engine: Any, factory: Any) -> None:
        """Register original objects; any binding failure permanently refuses cleanup."""
        try:
            self._require_not_refused()
            if self._engine is not None:
                raise SafetyError("Database engine is already registered")
            self._check_engine(engine)
            self._engine, self._factory = engine, factory
            self._registered = (engine, factory)
            self.validate()
            self.state = CleanupState.BOUND
        except Exception as exc:
            self.refuse(str(exc))
            raise SafetyError(self.failure_reason) from exc

    def _validate_bindings(self) -> None:
        """Inspect exact registered objects and an unconnected actual Session."""
        self._require_not_refused()
        if self._closed or self._registered is None:
            raise SafetyError("Test database is not active and registered")
        engine, factory = self._registered
        if self._engine is not engine or self._factory is not factory:
            raise SafetyError("Registered engine or Session factory object changed")
        self._check_engine(engine)
        if factory.kw.get("bind") is not engine or factory.kw.get("binds"):
            raise SafetyError("Session factory binding does not match test engine")
        session = factory()
        try:
            if session.get_bind() is not engine:
                raise SafetyError("Session binding does not match test engine")
        finally:
            session.close()
        if self._application is not None:
            config, database, _ = self._application
            if config.DATABASE_URL != self.url:
                raise SafetyError("Application configuration differs from test database")
            if (database.engine is not engine or database.SessionLocal is not factory
                    or database.get_db is not self._dependency):
                raise SafetyError("Module-level database objects changed")
            for router, attributes in self._router_checks:
                for attribute in attributes:
                    expected = self._dependency if attribute == "get_db" else factory
                    if getattr(router, attribute, None) is not expected:
                        raise SafetyError(f"Router {attribute} identity changed or disappeared")

    def validate(self) -> None:
        """Latch every failed identity check before propagating a safe error."""
        try:
            self._validate_bindings()
        except Exception as exc:
            self.refuse(str(exc))
            raise SafetyError(self.failure_reason) from exc

    def validate_application(self, config: Any, database: Any,
                             routers: list[Any]) -> None:
        """Retain live application references for revalidation during close."""
        try:
            self._require_not_refused()
            if self._application is None:
                self._dependency = database.get_db
            else:
                previous_config, previous_database, _ = self._application
                if config is not previous_config or database is not previous_database:
                    raise SafetyError("Application configuration or module object changed")
                self.validate()
            for router in routers:
                if not any(saved is router for saved, _ in self._router_checks):
                    attributes = tuple(name for name in ("get_db", "SessionLocal")
                                       if hasattr(router, name))
                    self._router_checks.append((router, attributes))
            self._application = (config, database, tuple(routers))
            self.validate()
            self.state = CleanupState.APPLICATION_VALIDATED
        except Exception as exc:
            self.refuse(str(exc))
            raise SafetyError(self.failure_reason) from exc

    @contextmanager
    def session(self) -> Iterator[Any]:
        """Provide a checked Session and always close it."""
        self.validate()
        session = self._factory()
        try:
            try:
                if session.get_bind() is not self._engine:
                    raise SafetyError("Session binding does not match test engine")
            except Exception as exc:
                self.refuse(str(exc))
                raise SafetyError(self.failure_reason) from exc
            yield session
        finally:
            session.close()

    def schema(self, metadata: Any, action: str) -> None:
        """The only test entry point for creating or dropping application tables."""
        self.validate()
        if action not in {"create_all", "drop_all"}:
            raise SafetyError("Unsupported schema operation")
        getattr(metadata, action)(bind=self._engine)

    def backup(self) -> Path:
        """Copy only the registered database into a newly allocated owned directory."""
        self.validate()
        destination = Path(tempfile.mkdtemp(prefix="backup-", dir=self.root)) / "backup.db"
        if destination.parent.resolve().parent != self.root:
            raise SafetyError("Backup destination escaped temporary directory")
        shutil.copyfile(self.path, destination)
        self._backups.add(destination)
        return destination

    def validate_backup_engine(self, engine: Any, backup: Path) -> None:
        """Allow restoration reads only from a copy made by this owner."""
        self.validate()
        if (backup not in self._backups or backup.is_symlink()
                or backup.resolve() != backup
                or not backup.is_relative_to(self.root)):
            raise SafetyError("Backup file is not registered for this test run")
        if (engine.url.drivername != "sqlite" or engine.url.query
                or Path(engine.url.database) != backup):
            raise SafetyError("Backup engine does not match the registered copy")

    def restore_environment(self) -> None:
        """Restore once, without overwriting a subsequent user/environment change."""
        if self._environment_restored:
            return
        if os.environ.get("DATABASE_URL") == self.url:
            if self._previous_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = self._previous_url
            self._environment_restored = True

    def close(self) -> None:
        """Recheck identities, close resources, then recheck and remove owned files."""
        if self._closed:
            return
        try:
            try:
                self._require_not_refused()
                self._check_root()
                if (self._registered is not None or self._engine is not None
                        or self._factory is not None):
                    self.validate()
            except Exception as exc:
                self.refuse(str(exc))
                raise SafetyError(self.failure_reason) from exc
            if self._engine is not None and not self._resources_closed:
                try:
                    self._engine.dispose()
                except Exception as exc:
                    self.state = CleanupState.CLEANUP_FAILED
                    self.failure_reason = f"Resource close failed for {self._allocated_root}: {type(exc).__name__}"
                    raise SafetyError(self.failure_reason) from exc
                self._resources_closed = True
                self.state = CleanupState.RESOURCES_CLOSED
            try:
                self._check_root()
            except Exception as exc:
                self.refuse(str(exc))
                raise SafetyError(self.failure_reason) from exc
            try:
                self._directory.close()
                if self._owned_parent is not None:
                    self._owned_parent.close()
            except Exception as exc:
                self.state = (CleanupState.REFUSED if isinstance(exc, SafetyError)
                              else CleanupState.CLEANUP_FAILED)
                self.failure_reason = f"Directory cleanup failed for {self._allocated_root}; current={self._directory.current_path}: {exc}"
                raise
            self._closed = True
            self.state = CleanupState.CLEANED
        finally:
            self.restore_environment()
