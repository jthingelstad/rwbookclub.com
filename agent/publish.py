"""Build the static site from the live DB and deploy it to GitHub Pages (gh-pages branch).

The corpus (`corpus/data/`) is a private, on-disk artifact regenerated from the `club_*`
tables — it is no longer committed to git, and CI no longer builds the site (it has no DB).
Instead the build + deploy run locally, where the data lives:

    python -m agent.publish        # developers: after a template/code change
    (Oliver runs publish_site() in the background after every data write)

`publish_site()` regenerates the corpus, runs `npm run build`, refuses to deploy an empty
site (or one missing the CNAME), and pushes `website/_site` to the `gh-pages` branch, which
GitHub Pages serves. A non-blocking file lock prevents overlapping builds.
"""

from __future__ import annotations

import fcntl
import logging
import os
import shutil
import subprocess
from pathlib import Path

from agent import corpus_gen

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
# Git identity for the gh-pages deploy commit (Oliver writes nothing else to git).
AUTHOR_NAME = os.environ.get("OLIVER_GIT_AUTHOR_NAME", "Oliver (RWBC bot)")
AUTHOR_EMAIL = os.environ.get("OLIVER_GIT_AUTHOR_EMAIL", "oliver@rwbookclub.com")
SITE_DIR = REPO_ROOT / "website" / "_site"
LOCK_PATH = REPO_ROOT / ".publish.lock"  # gitignored
GH_PAGES_BRANCH = "gh-pages"
BUILD_TIMEOUT = 300
DEPLOY_TIMEOUT = 300

# launchd / cron can give a minimal PATH; resolve node tooling explicitly and ensure the
# build/deploy subprocesses (and npm's `#!/usr/bin/env node` shebang) can find node.
_EXTRA_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")
_ENV = {**os.environ, "PATH": os.pathsep.join([*_EXTRA_BIN_DIRS, os.environ.get("PATH", "")])}


class PublishError(Exception):
    """A publish failed (build error, empty site, deploy push failed)."""


class PublishBusy(PublishError):
    """Another publish is already running — caller should skip (and retry later)."""


def _bin(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for d in _EXTRA_BIN_DIRS:
        candidate = Path(d) / name
        if candidate.exists():
            return str(candidate)
    raise PublishError(
        f"`{name}` not found on PATH — Oliver's launchd PATH must include node/npm")


def ensure_corpus() -> dict:
    """Regenerate corpus/data from the DB so the on-disk corpus mirrors club_*.
    Idempotent (full regen + prune); safe to call at startup and before each build."""
    return corpus_gen.generate()


def _run(cmd: list[str], timeout: int, cwd: Path = REPO_ROOT) -> None:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=_ENV)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "")[-2000:]
        raise PublishError(f"`{' '.join(cmd)}` failed (rc={r.returncode}):\n{tail}")


def git_output(args: list[str], *, timeout: int = 15) -> str:
    """Run a READ-ONLY git command from the repo root and return its stdout.

    Returns "" on any failure (non-zero exit, git missing, timeout) so callers can treat
    "no output" uniformly. Uses the same launchd-safe `_bin("git")` + `_ENV` resolution as
    the deploy path, so it works under a minimal cron/launchd PATH. Read-only by contract —
    do not pass mutating subcommands here.
    """
    try:
        r = subprocess.run([_bin("git"), *args], cwd=REPO_ROOT, capture_output=True,
                           text=True, timeout=timeout, env=_ENV)
    except (subprocess.SubprocessError, OSError, PublishError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def _deploy_gh_pages(message: str) -> None:
    """Force-push the built `_site` to the gh-pages branch as a clean orphan commit.

    Uses a throwaway git repo *inside* `_site` so the branch contains exactly the built
    site (plus `.nojekyll`) — nothing from `main`. This is deterministic and dependency-free
    (the `gh-pages` npm package leaks repo-root files from its cache clone)."""
    git = _bin("git")
    origin = subprocess.run([git, "remote", "get-url", "origin"], cwd=REPO_ROOT,
                            capture_output=True, text=True, check=True, env=_ENV).stdout.strip()
    (SITE_DIR / ".nojekyll").touch()  # tell GitHub Pages to serve the tree as-is (no Jekyll)

    def g(*args: str) -> None:
        _run([git, *args], DEPLOY_TIMEOUT, cwd=SITE_DIR)

    shutil.rmtree(SITE_DIR / ".git", ignore_errors=True)
    try:
        g("init", "-q", "-b", GH_PAGES_BRANCH)
        g("add", "-A")
        g("-c", f"user.name={AUTHOR_NAME}", "-c", f"user.email={AUTHOR_EMAIL}",
          "commit", "-q", "-m", message)
        g("push", "-q", "-f", origin, f"HEAD:{GH_PAGES_BRANCH}")
    finally:
        shutil.rmtree(SITE_DIR / ".git", ignore_errors=True)


# Floor for the partial-build guard: well below the ~179 real book pages, but high enough
# that a corpus-less or half-rendered build (which can still emit index.html + CNAME) is caught.
MIN_BOOK_PAGES = 50


def publish_site() -> dict:
    """Regenerate corpus → build the site → deploy `_site` to the gh-pages branch.

    Guards against the silent-empty-site / partial-build failure modes (a missing or bad
    corpus can still emit `index.html` + the passthrough `CNAME`) by refusing to deploy
    unless those exist AND at least MIN_BOOK_PAGES book pages rendered. Builds from a clean
    `_site` so a crash-orphaned `_site/.git` or stale pages can never ride along. Raises
    PublishBusy if a publish is already running (non-blocking lock)."""
    lock_file = open(LOCK_PATH, "w")  # noqa: SIM115 - held for the duration below
    try:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise PublishBusy("a publish is already in progress") from e

        written = ensure_corpus()
        shutil.rmtree(SITE_DIR, ignore_errors=True)  # clean tree every build
        _run([_bin("npm"), "run", "build"], BUILD_TIMEOUT)
        if not (SITE_DIR / "index.html").exists():
            raise PublishError("build produced no _site/index.html — refusing to deploy")
        if not (SITE_DIR / "CNAME").exists():
            raise PublishError("_site/CNAME missing — refusing to deploy (would drop the custom domain)")
        book_pages = len(list((SITE_DIR / "books").glob("*/index.html")))
        if book_pages < MIN_BOOK_PAGES:
            raise PublishError(
                f"build produced only {book_pages} book pages (< {MIN_BOOK_PAGES}) — "
                "refusing to deploy a partial/empty site")
        _deploy_gh_pages("Deploy site")
        log.info("published site to gh-pages (%d book pages): %s", book_pages, written)
        return {"corpus": written, "deployed": True}
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(publish_site())


if __name__ == "__main__":
    main()
