"""Commit (and push) corpus changes Oliver writes — the B→A bridge to Git.

Oliver writes canonical files (reviews now, meetings later) into his repo clone and
commits them; a push to main auto-deploys the site, and git history is the audit log.

`OLIVER_GIT_PUSH=0` commits locally without pushing (used in tests). Host push auth
(deploy key / token) is a deployment concern.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PUSH = os.environ.get("OLIVER_GIT_PUSH", "1") != "0"
DRYRUN = os.environ.get("OLIVER_GIT_DRYRUN") == "1"  # write files but never commit/push (tests)
AUTHOR_NAME = os.environ.get("OLIVER_GIT_AUTHOR_NAME", "Oliver (RWBC bot)")
AUTHOR_EMAIL = os.environ.get("OLIVER_GIT_AUTHOR_EMAIL", "oliver@rwbookclub.com")


class GitWriteError(Exception):
    pass


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=check
    )


def sync() -> None:
    """Best-effort pull --rebase before writing, to reduce push races. No-op if not pushing."""
    if not PUSH or DRYRUN:
        return
    try:
        _git("pull", "--rebase")
    except subprocess.CalledProcessError as e:  # leave the tree as-is; commit will still try
        raise GitWriteError(f"git pull --rebase failed: {e.stderr.strip()}") from e


def commit_paths(paths: list[Path | str], message: str) -> str | None:
    """Stage + commit the given paths (Oliver identity), then push if enabled.

    Returns the new commit sha, or None if there was nothing to commit.
    """
    if DRYRUN:
        return None
    _git("add", *[str(p) for p in paths])
    # Nothing staged? (e.g. an identical re-submit) — treat as a no-op.
    if _git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return None
    _git(
        "-c", f"user.name={AUTHOR_NAME}", "-c", f"user.email={AUTHOR_EMAIL}",
        "commit", "-m", message,
    )
    sha = _git("rev-parse", "HEAD").stdout.strip()
    if PUSH:
        try:
            _git("push")
        except subprocess.CalledProcessError:
            # One retry: integrate remote changes, then push again.
            _git("pull", "--rebase")
            try:
                _git("push")
            except subprocess.CalledProcessError as e:
                raise GitWriteError(f"git push failed: {e.stderr.strip()}") from e
    return sha
