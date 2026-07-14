"""Owner-only runtime permission policy for Oliver's private local state."""

from __future__ import annotations

import os
import stat

from agent import security


def _mode(path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _runtime_paths(tmp_path) -> security.RuntimePaths:
    return security.RuntimePaths(
        repo_root=tmp_path,
        env_file=tmp_path / ".env",
        db_path=tmp_path / "agent" / "oliver.db",
        logs_dir=tmp_path / "agent" / "logs",
        backups_dir=tmp_path / "agent" / "backups",
        corpus_dir=tmp_path / "corpus" / "data",
        offsite_dir=tmp_path / "offsite",
    )


def _seed_insecure(paths: security.RuntimePaths) -> None:
    paths.env_file.write_text("SECRET=value")
    paths.env_file.chmod(0o644)
    paths.db_path.parent.mkdir()
    for path in (paths.db_path, paths.db_path.with_name("oliver.db-wal"),
                 paths.db_path.with_name("oliver.db-shm")):
        path.write_bytes(b"private")
        path.chmod(0o666)
    for directory in (paths.logs_dir, paths.backups_dir, paths.corpus_dir, paths.offsite_dir):
        directory.mkdir(parents=True)
        directory.chmod(0o755)
        nested = directory / "nested"
        nested.mkdir()
        nested.chmod(0o755)
        private_file = nested / "state.txt"
        private_file.write_text("member state")
        private_file.chmod(0o644)


def test_runtime_permission_repair_is_complete_and_idempotent(tmp_path):
    paths = _runtime_paths(tmp_path)
    _seed_insecure(paths)

    report = security.enforce_runtime_permissions(paths=paths, repair=True)
    assert report.ok
    assert report.changed
    assert _mode(paths.env_file) == 0o600
    assert _mode(paths.db_path) == 0o600
    assert _mode(paths.db_path.with_name("oliver.db-wal")) == 0o600
    assert _mode(paths.db_path.with_name("oliver.db-shm")) == 0o600
    for directory in (paths.logs_dir, paths.backups_dir, paths.corpus_dir, paths.offsite_dir):
        assert _mode(directory) == 0o700
        assert _mode(directory / "nested") == 0o700
        assert _mode(directory / "nested" / "state.txt") == 0o600

    second = security.enforce_runtime_permissions(paths=paths, repair=True)
    assert second.ok and second.changed == []


def test_audit_reports_mode_drift_without_mutating(tmp_path):
    paths = _runtime_paths(tmp_path)
    _seed_insecure(paths)
    before = _mode(paths.env_file)

    report = security.enforce_runtime_permissions(paths=paths, repair=False)
    assert not report.ok
    assert report.changed == []
    assert any(path == paths.env_file and "expected 0600" in reason
               for path, reason in report.unresolved)
    assert _mode(paths.env_file) == before


def test_private_tree_refuses_symlinks_without_touching_target(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    target = tmp_path / "public.txt"
    target.write_text("public")
    target.chmod(0o644)
    (private / "linked").symlink_to(target)

    report = security.secure_directory_tree(private, repair=True)
    assert not report.ok
    assert any(path.name == "linked" and "symbolic link" in reason
               for path, reason in report.unresolved)
    assert _mode(target) == 0o644


def test_private_umask_creates_owner_only_files(tmp_path):
    previous = os.umask(0)
    try:
        security.set_private_umask()
        created = tmp_path / "created.txt"
        created.write_text("private")
        assert _mode(created) == 0o600
    finally:
        os.umask(previous)
