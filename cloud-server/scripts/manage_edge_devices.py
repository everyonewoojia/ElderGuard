"""Trusted local administrator tool; never expose this as an HTTP endpoint.

Run from cloud-server: python -B -m scripts.manage_edge_devices --help
Token files must be placed in an administrator-controlled directory. Existing
files are never overwritten. On failure retain output for verification/reissue;
neither database files nor credential files are automatically deleted.
"""
import argparse
import csv
import os
from pathlib import Path
import re
import subprocess
import sys

from app.core import database
from app.services import device_auth as service


def secure_output(path: Path, descriptor: int) -> None:
    """Restrict a new, still-empty file before writing any credential bytes.

    Windows mode bits do not establish a private DACL. Use the platform tools
    with argument arrays, no shell, and fail closed if they are unavailable.
    """
    if os.name == "nt":
        system32 = Path(os.environ["SystemRoot"]) / "System32"
        user = subprocess.run([str(system32 / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
                              capture_output=True, text=True, check=True,
                              creationflags=subprocess.CREATE_NO_WINDOW)
        rows = list(csv.reader(user.stdout.splitlines()))
        sid = rows[0][-1].strip() if len(rows) == 1 else ""
        if not re.fullmatch(r"S-1-[0-9-]+", sid):
            raise service.DeviceAuthError("Cannot establish credential owner")
        subprocess.run([str(system32 / "icacls.exe"), str(path), "/inheritance:r",
                        "/grant:r", f"*{sid}:(F)"],
                       capture_output=True, check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        os.fchmod(descriptor, 0o600)


def write_token(path: Path, token: str) -> None:
    """Exclusively create a token file; administrators control its parent ACL."""
    if (not path.is_absolute() or path.parent.resolve() != path.parent
            or not path.parent.is_dir()
            or any(p.is_symlink() or p.is_junction() for p in path.parents)):
        raise service.DeviceAuthError("Credential output requires a controlled absolute directory")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        secure_output(path, output.fileno())
        opened = os.fstat(output.fileno())
        current = path.lstat()
        if (not opened.st_ino or (opened.st_dev, opened.st_ino)
                != (current.st_dev, current.st_ino) or path.is_symlink()):
            raise service.DeviceAuthError("Credential output identity changed")
        output.write(token)
        output.flush()
        os.fsync(output.fileno())


def main(argv: list[str] | None = None) -> int:
    """Use only the configured application engine, with an explicit matching file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path,
                        help="Absolute existing SQLite file matching DATABASE_URL")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-table", help="Explicitly create only edge_devices")
    register = sub.add_parser("register", help="Enroll against the current database owner")
    register.add_argument("--elder-id", required=True, type=int)
    for name in ("issue", "rotate", "revoke"):
        command = sub.add_parser(name)
        command.add_argument("--device-id", required=True)
        if name != "revoke":
            command.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        service.validate_database_target(database.engine, args.database)
        if args.command == "init-table":
            service.initialize_device_table(database.engine, args.database)
        else:
            # Never silently use a replaced factory/bind.
            if database.SessionLocal.kw.get("bind") is not database.engine:
                raise service.DeviceConfigurationError("Session binding unavailable")
            with database.SessionLocal() as db:
                if db.get_bind() is not database.engine:
                    raise service.DeviceConfigurationError("Session binding unavailable")
                if args.command == "register":
                    device = service.register_device(db, args.elder_id)
                    print(f"Registered device: {device.device_id}")
                elif args.command == "revoke":
                    service.revoke_device(db, args.device_id)
                else:
                    operation = service.issue_token if args.command == "issue" else service.rotate_token
                    operation(db, args.device_id,
                              publish=lambda token: write_token(args.output, token))
        print("Operation completed; credentials are never printed.")
        return 0
    except Exception:
        # No exception text, URL, token, SQL parameters or traceback on this CLI.
        print("Operation failed. Preserve any output file; verify registry state before reissuing.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
