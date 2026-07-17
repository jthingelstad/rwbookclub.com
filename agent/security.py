"""Owner-only permissions for Oliver's local secrets and private runtime state.

The repository contains public source alongside gitignored credentials, member state, mailbox
history, generated corpus data, logs, and backups. This module keeps that boundary explicit and
idempotent. It never reads or prints file contents.
"""

from __future__ import annotations

import argparse
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700


@dataclass(frozen=True)
class RuntimePaths:
    repo_root: Path
    env_file: Path
    db_path: Path
    logs_dir: Path
    backups_dir: Path
    corpus_dir: Path
    offsite_dir: Path | None = None

    @classmethod
    def defaults(cls) -> RuntimePaths:
        repo = Path(__file__).resolve().parent.parent
        agent_dir = repo / "agent"
        db_path = Path(os.environ.get("OLIVER_DB_PATH") or agent_dir / "oliver.db").expanduser()
        offsite = os.environ.get(
            "OLIVER_OFFSITE_BACKUP_DIR",
            "~/Library/Mobile Documents/com~apple~CloudDocs/Oliver/backups",
        )
        return cls(
            repo_root=repo,
            env_file=repo / ".env",
            db_path=db_path,
            logs_dir=agent_dir / "logs",
            backups_dir=agent_dir / "backups",
            corpus_dir=repo / "corpus" / "data",
            offsite_dir=Path(offsite).expanduser() if offsite else None,
        )


@dataclass
class PermissionReport:
    checked: int = 0
    changed: list[Path] = field(default_factory=list)
    unresolved: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unresolved

    def merge(self, other: PermissionReport) -> None:
        self.checked += other.checked
        self.changed.extend(other.changed)
        self.unresolved.extend(other.unresolved)


def set_private_umask() -> None:
    """Make subsequently created runtime files private by default for this process."""
    os.umask(0o077)


def _secure_one(path: Path, expected_mode: int, *, repair: bool) -> PermissionReport:
    report = PermissionReport()
    try:
        info = path.lstat()
    except FileNotFoundError:
        return report
    report.checked = 1
    if stat.S_ISLNK(info.st_mode):
        report.unresolved.append((path, "symbolic link is not allowed for private runtime state"))
        return report
    if info.st_uid != os.getuid():
        report.unresolved.append((path, "path is not owned by the Oliver runtime user"))
        return report
    actual = stat.S_IMODE(info.st_mode)
    if actual == expected_mode:
        return report
    if not repair:
        report.unresolved.append((path, f"mode is {actual:04o}; expected {expected_mode:04o}"))
        return report
    try:
        path.chmod(expected_mode)
        report.changed.append(path)
    except OSError as exc:
        report.unresolved.append(
            (path, f"could not set mode {expected_mode:04o}: {type(exc).__name__}")
        )
    return report


def secure_file(path: Path, *, repair: bool = True) -> PermissionReport:
    return _secure_one(Path(path), PRIVATE_FILE_MODE, repair=repair)


def secure_directory_tree(path: Path, *, repair: bool = True) -> PermissionReport:
    """Secure an existing directory and every non-symlink entry below it."""
    root = Path(path)
    report = PermissionReport()
    if not root.exists() and not root.is_symlink():
        return report
    report.merge(_secure_one(root, PRIVATE_DIR_MODE, repair=repair))
    if not root.is_dir() or root.is_symlink():
        return report
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in dirnames:
            report.merge(_secure_one(current_path / name, PRIVATE_DIR_MODE, repair=repair))
        for name in filenames:
            report.merge(_secure_one(current_path / name, PRIVATE_FILE_MODE, repair=repair))
    return report


def secure_database_files(db_path: Path, *, repair: bool = True) -> PermissionReport:
    report = PermissionReport()
    path = Path(db_path)
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        report.merge(secure_file(candidate, repair=repair))
    return report


def enforce_runtime_permissions(
    *, paths: RuntimePaths | None = None, repair: bool = True
) -> PermissionReport:
    """Audit or repair every known private runtime path without reading its contents."""
    set_private_umask()
    paths = paths or RuntimePaths.defaults()
    report = PermissionReport()
    report.merge(secure_file(paths.env_file, repair=repair))
    report.merge(secure_database_files(paths.db_path, repair=repair))
    for directory in (paths.logs_dir, paths.backups_dir, paths.corpus_dir, paths.offsite_dir):
        if directory is not None:
            report.merge(secure_directory_tree(directory, repair=repair))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit Oliver private runtime file permissions")
    parser.add_argument("--repair", action="store_true", help="repair safe mode violations")
    args = parser.parse_args()
    report = enforce_runtime_permissions(repair=args.repair)
    print(
        f"Oliver runtime permissions: mode={'repair' if args.repair else 'audit'} "
        f"checked={report.checked} changed={len(report.changed)} "
        f"unresolved={len(report.unresolved)}"
    )
    for path, reason in report.unresolved:
        print(f"  unsafe: {path} ({reason})")
    if not report.ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
