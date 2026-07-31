#!/usr/bin/env python3
"""Event-driven AGENT-TEAM dispatcher for Oliver.

The process is intentionally deterministic until an issue has an actionable route. Idle polls
query GitHub and exit without invoking Codex. A real handoff launches one persisted `codex exec`
session, then verifies the issue state instead of trusting the agent's final prose.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "dispatch.toml"
LAUNCH_AGENT = Path.home() / "Library/LaunchAgents/com.rwbookclub.agent-team-dispatcher.plist"
EXPECTED_LAUNCH_LABEL = "com.rwbookclub.agent-team-dispatcher"


class DispatchError(RuntimeError):
    """Fail-closed dispatcher error."""


@dataclass(frozen=True)
class Route:
    label: str
    role: str
    role_file: Path
    model: str
    reasoning_effort: str
    priority: int


@dataclass(frozen=True)
class InferenceRule:
    route: str
    all_labels: frozenset[str]
    any_labels: frozenset[str]
    no_labels: frozenset[str]


@dataclass(frozen=True)
class Config:
    path: Path
    repo: str
    cwd: Path
    state_dir: Path
    poll_interval_seconds: int
    role_timeout_seconds: int
    chain_limit: int
    chain_window_seconds: int
    max_attempts: int
    retry_delays_seconds: tuple[int, ...]
    log_retention_days: int
    codex_bin: Path
    gh_bin: Path
    git_bin: Path
    preflight: Path
    automation_registry: Path
    dispatch_prefix: str
    stop_labels: frozenset[str]
    pending_labels: frozenset[str]
    routes: dict[str, Route]
    inference: tuple[InferenceRule, ...]


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    state: str
    labels: frozenset[str]
    created_at: str
    updated_at: str
    url: str


@dataclass(frozen=True)
class Selection:
    issue: Issue
    route: Route
    source: str


@dataclass(frozen=True)
class Transition:
    valid: bool
    outcome: str
    next_route: str | None = None


def _absolute_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    path = path.expanduser().resolve()
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    if raw.get("version") != 1:
        raise DispatchError(f"unsupported dispatch config version: {raw.get('version')!r}")

    cwd = Path(raw["cwd"]).expanduser().resolve()
    routes: dict[str, Route] = {}
    for item in raw.get("routes", []):
        route = Route(
            label=item["label"],
            role=item["role"],
            role_file=_absolute_path(item["role_file"], cwd),
            model=item["model"],
            reasoning_effort=item["reasoning_effort"],
            priority=int(item["priority"]),
        )
        if route.label in routes:
            raise DispatchError(f"duplicate route label: {route.label}")
        routes[route.label] = route
    if not routes:
        raise DispatchError("dispatch config has no routes")

    inference = tuple(
        InferenceRule(
            route=item["route"],
            all_labels=frozenset(item.get("all", [])),
            any_labels=frozenset(item.get("any", [])),
            no_labels=frozenset(item.get("none", [])),
        )
        for item in raw.get("inference", [])
    )
    unknown = sorted({rule.route for rule in inference} - set(routes))
    if unknown:
        raise DispatchError(f"inference references unknown routes: {', '.join(unknown)}")

    retry_delays = tuple(int(value) for value in raw["retry_delays_seconds"])
    max_attempts = int(raw["max_attempts"])
    if len(retry_delays) < max_attempts:
        raise DispatchError("retry_delays_seconds must cover max_attempts")

    return Config(
        path=path,
        repo=raw["repo"],
        cwd=cwd,
        state_dir=Path(raw["state_dir"]).expanduser().resolve(),
        poll_interval_seconds=int(raw["poll_interval_seconds"]),
        role_timeout_seconds=int(raw["role_timeout_seconds"]),
        chain_limit=int(raw["chain_limit"]),
        chain_window_seconds=int(raw["chain_window_minutes"]) * 60,
        max_attempts=max_attempts,
        retry_delays_seconds=retry_delays,
        log_retention_days=int(raw["log_retention_days"]),
        codex_bin=Path(raw["codex_bin"]).expanduser().resolve(),
        gh_bin=Path(raw["gh_bin"]).expanduser().resolve(),
        git_bin=Path(raw["git_bin"]).expanduser().resolve(),
        preflight=Path(raw["preflight"]).expanduser().resolve(),
        automation_registry=Path(raw["automation_registry"]).expanduser().resolve(),
        dispatch_prefix=raw["dispatch_prefix"],
        stop_labels=frozenset(raw["stop_labels"]),
        pending_labels=frozenset(raw["pending_labels"]),
        routes=routes,
        inference=inference,
    )


def parse_issue(raw: dict[str, Any]) -> Issue:
    labels = frozenset(
        item["name"] if isinstance(item, dict) else str(item) for item in raw.get("labels", [])
    )
    return Issue(
        number=int(raw["number"]),
        title=raw.get("title", ""),
        state=raw.get("state", "OPEN").upper(),
        labels=labels,
        created_at=raw.get("createdAt", ""),
        updated_at=raw.get("updatedAt", ""),
        url=raw.get("url", ""),
    )


def infer_route(issue: Issue, config: Config) -> tuple[Route, str] | None:
    if issue.state != "OPEN" or issue.labels & config.stop_labels:
        return None
    explicit = sorted(label for label in issue.labels if label.startswith(config.dispatch_prefix))
    if len(explicit) > 1:
        raise DispatchError(
            f"issue #{issue.number} has multiple dispatch labels: {', '.join(explicit)}"
        )
    if explicit:
        label = explicit[0]
        route = config.routes.get(label)
        if route is None:
            raise DispatchError(f"issue #{issue.number} has unknown dispatch label {label}")
        return route, "explicit"

    for rule in config.inference:
        if not rule.all_labels.issubset(issue.labels):
            continue
        if rule.any_labels and not (rule.any_labels & issue.labels):
            continue
        if rule.no_labels & issue.labels:
            continue
        return config.routes[rule.route], "inferred"
    return None


def select_candidates(issues: Sequence[Issue], config: Config) -> list[Selection]:
    selected: list[Selection] = []
    for issue in issues:
        inferred = infer_route(issue, config)
        if inferred is None:
            continue
        route, source = inferred
        selected.append(Selection(issue=issue, route=route, source=source))
    return sorted(
        selected,
        key=lambda item: (item.route.priority, item.issue.created_at, item.issue.number),
    )


def assess_transition(before_route: str, after: Issue, config: Config) -> Transition:
    if after.state != "OPEN":
        return Transition(True, "closed")
    routes = sorted(label for label in after.labels if label.startswith(config.dispatch_prefix))
    if before_route in routes:
        return Transition(False, "current dispatch label was not cleared")
    if len(routes) > 1:
        return Transition(False, "multiple next dispatch labels remain")
    if routes:
        if routes[0] not in config.routes:
            return Transition(False, f"unknown next dispatch label {routes[0]}")
        return Transition(True, "handoff", routes[0])
    if after.labels & (config.stop_labels - {"wip"}):
        return Transition(True, "stopped")
    return Transition(False, "open issue has no next dispatch label or explicit stop state")


def build_prompt(selection: Selection, config: Config) -> str:
    issue = selection.issue
    route = selection.route
    return f"""Oliver AGENT-TEAM event dispatch

You are the {route.role} for `/Users/otto/Projects/rwbookclub.com`.
Work exactly one issue: #{issue.number} — {issue.title}
Issue URL: {issue.url}
Current handoff: `{route.label}`

The deterministic dispatcher has already added `wip` and claimed this issue for this role. Do not
skip it because it is labeled `wip`; treat that claim as yours. Read `AGENTS.md`,
`AGENT-TEAM/WORKFLOW.md`, `AGENT-TEAM/README.md`, and `{route.role_file}` completely, then execute
that role's current contract. Run preflight again before acting and preserve every approval,
privacy, database, deployment, and commit-lane boundary.

GitHub state—not your final prose—is the completion protocol. Before finishing:

1. Update issue #{issue.number} with evidence and the result.
2. Remove `{route.label}` and `wip`.
3. If another lane is required, add exactly one next `dispatch:*` label and retain the appropriate
   `needs-eval` / `needs-culture` / `needs-deploy` pending labels.
4. Close the issue only when no downstream lane or human decision remains. New product direction
   stops at `proposal`; blocked or ambiguous work stops with `blocked` or `needs-design`.
5. End with a clean repository. Never push a pre-existing commit.

Do not invoke another role directly. The dispatcher will re-read the authoritative issue state and
launch the next persisted role session when appropriate.
"""


class StateStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "state.json"
        self.events_path = root / "events.jsonl"
        self.runs_dir = root / "runs"
        self.lock_path = root / "dispatcher.lock"

    def prepare(self) -> None:
        os.umask(0o077)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(self.runs_dir, 0o700)

    @contextmanager
    def lock(self) -> Iterator[bool]:
        self.prepare()
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            os.chmod(self.lock_path, 0o600)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "failures": {}, "chains": {}, "recent": [], "current": None}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, state: dict[str, Any]) -> None:
        self.prepare()
        fd, raw_path = tempfile.mkstemp(prefix="state-", suffix=".json", dir=self.root)
        temp_path = Path(raw_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def event(self, event: str, **fields: Any) -> None:
        self.prepare()
        record = {"at": now_iso(), "event": event, **fields}
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        fd = os.open(self.events_path, flags, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        print(json.dumps(record, sort_keys=True), flush=True)

    def prune(self, days: int) -> None:
        cutoff = time.time() - days * 86400
        for path in self.runs_dir.iterdir():
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _run_json(command: Sequence[str], *, cwd: Path) -> Any:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise DispatchError(detail)
    return json.loads(result.stdout)


class GitHub:
    def __init__(self, config: Config) -> None:
        self.config = config

    def list_open(self) -> list[Issue]:
        raw = _run_json(
            [
                str(self.config.gh_bin),
                "issue",
                "list",
                "--repo",
                self.config.repo,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "number,title,state,labels,createdAt,updatedAt,url",
            ],
            cwd=self.config.cwd,
        )
        return [parse_issue(item) for item in raw]

    def view(self, number: int) -> Issue:
        raw = _run_json(
            [
                str(self.config.gh_bin),
                "issue",
                "view",
                str(number),
                "--repo",
                self.config.repo,
                "--json",
                "number,title,state,labels,createdAt,updatedAt,url",
            ],
            cwd=self.config.cwd,
        )
        return parse_issue(raw)

    def add_label(self, number: int, label: str) -> None:
        self._edit(number, "--add-label", label)

    def remove_label(self, number: int, label: str) -> None:
        current = self.view(number)
        if label in current.labels:
            self._edit(number, "--remove-label", label)

    def _edit(self, number: int, operation: str, label: str) -> None:
        result = subprocess.run(
            [
                str(self.config.gh_bin),
                "issue",
                "edit",
                str(number),
                "--repo",
                self.config.repo,
                operation,
                label,
            ],
            cwd=self.config.cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise DispatchError(result.stderr.strip() or result.stdout.strip())

    def comment(self, number: int, body: str) -> None:
        result = subprocess.run(
            [
                str(self.config.gh_bin),
                "issue",
                "comment",
                str(number),
                "--repo",
                self.config.repo,
                "--body",
                body,
            ],
            cwd=self.config.cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise DispatchError(result.stderr.strip() or result.stdout.strip())


def label_fingerprint(issue: Issue, route: Route) -> str:
    labels = sorted(label for label in issue.labels if label != "wip")
    return f"{issue.number}:{route.label}:{'|'.join(labels)}"


def retry_ready(selection: Selection, state: dict[str, Any], now: float) -> bool:
    key = f"{selection.issue.number}:{selection.route.label}"
    record = state.get("failures", {}).get(key)
    if not record:
        return True
    if record.get("fingerprint") != label_fingerprint(selection.issue, selection.route):
        return True
    return now >= float(record.get("next_retry_at", 0))


def run_preflight(config: Config) -> tuple[bool, str]:
    result = subprocess.run(
        [str(config.preflight)],
        cwd=config.cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


def _extract_thread_id(log_path: Path) -> str | None:
    try:
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            for key in ("thread_id", "threadId"):
                value = event.get(key)
                if isinstance(value, str):
                    return value
            thread = event.get("thread")
            if isinstance(thread, dict):
                value = thread.get("id") or thread.get("thread_id")
                if isinstance(value, str):
                    return value
    except OSError:
        return None
    return None


def run_role(
    selection: Selection, config: Config, store: StateStore, state: dict[str, Any]
) -> tuple[int, str | None, Path, Path, bool]:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    stem = f"{stamp}-issue-{selection.issue.number}-{selection.route.role}"
    log_path = store.runs_dir / f"{stem}.jsonl"
    summary_path = store.runs_dir / f"{stem}.summary.md"
    prompt = build_prompt(selection, config)
    command = [
        str(config.codex_bin),
        "exec",
        "--json",
        "--cd",
        str(config.cwd),
        "--model",
        selection.route.model,
        "--config",
        f'model_reasoning_effort="{selection.route.reasoning_effort}"',
        "--config",
        'approval_policy="never"',
        "--sandbox",
        "danger-full-access",
        "--output-last-message",
        str(summary_path),
        prompt,
    ]
    state["current"] = {
        "issue": selection.issue.number,
        "title": selection.issue.title,
        "role": selection.route.role,
        "route": selection.route.label,
        "started_at": now_iso(),
        "log": str(log_path),
        "summary": str(summary_path),
        "thread_id": None,
    }
    store.save(state)

    timed_out = False
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            try:
                result = subprocess.run(
                    command,
                    cwd=config.cwd,
                    text=True,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    timeout=config.role_timeout_seconds,
                    check=False,
                    env=os.environ.copy(),
                )
                exit_code = result.returncode
            except subprocess.TimeoutExpired:
                exit_code = 124
                timed_out = True
    finally:
        if summary_path.exists():
            os.chmod(summary_path, 0o600)
    thread_id = _extract_thread_id(log_path)
    return exit_code, thread_id, log_path, summary_path, timed_out


def _record_recent(state: dict[str, Any], record: dict[str, Any]) -> None:
    recent = state.setdefault("recent", [])
    recent.append(record)
    del recent[:-50]
    state["current"] = None


def _clear_failure(state: dict[str, Any], selection: Selection) -> None:
    key = f"{selection.issue.number}:{selection.route.label}"
    state.setdefault("failures", {}).pop(key, None)


def _mark_failure(
    selection: Selection,
    reason: str,
    config: Config,
    github: GitHub,
    store: StateStore,
    state: dict[str, Any],
    *,
    exit_code: int | None = None,
    thread_id: str | None = None,
) -> None:
    key = f"{selection.issue.number}:{selection.route.label}"
    failures = state.setdefault("failures", {})
    fingerprint = label_fingerprint(selection.issue, selection.route)
    previous = failures.get(key, {})
    attempts = (
        int(previous.get("attempts", 0)) + 1 if previous.get("fingerprint") == fingerprint else 1
    )
    delay = config.retry_delays_seconds[min(attempts - 1, len(config.retry_delays_seconds) - 1)]
    failures[key] = {
        "attempts": attempts,
        "fingerprint": fingerprint,
        "next_retry_at": time.time() + delay,
        "reason": reason,
        "updated_at": now_iso(),
    }
    github.remove_label(selection.issue.number, "wip")
    if attempts >= config.max_attempts:
        github.add_label(selection.issue.number, "blocked")
        body = (
            f"Dispatcher blocked `{selection.route.role}` after {attempts} failed attempts. "
            f"Reason: {reason}. The `{selection.route.label}` handoff remains for recovery after "
            "the blocker is cleared."
        )
    else:
        body = (
            f"Dispatcher run for `{selection.route.role}` did not produce a valid handoff "
            f"(attempt {attempts}/{config.max_attempts}): {reason}. It will retry after the "
            "configured backoff."
        )
    github.comment(selection.issue.number, body)
    record = {
        "issue": selection.issue.number,
        "role": selection.route.role,
        "route": selection.route.label,
        "status": "failed",
        "reason": reason,
        "exit_code": exit_code,
        "thread_id": thread_id,
        "finished_at": now_iso(),
    }
    _record_recent(state, record)
    store.save(state)
    store.event("role_failed", **record)


def _chain_allowed(
    selection: Selection, config: Config, github: GitHub, store: StateStore, state: dict[str, Any]
) -> bool:
    key = str(selection.issue.number)
    now = time.time()
    chains = state.setdefault("chains", {})
    chain = chains.get(key)
    if not chain or now - float(chain.get("started_at", 0)) > config.chain_window_seconds:
        chain = {"started_at": now, "hops": 0}
        chains[key] = chain
    if int(chain["hops"]) >= config.chain_limit:
        github.add_label(selection.issue.number, "blocked")
        github.comment(
            selection.issue.number,
            f"Dispatcher stopped after {config.chain_limit} role hops within "
            f"{config.chain_window_seconds // 60} minutes. The current handoff remains, but a "
            "human must review the loop and clear `blocked` before it can continue.",
        )
        store.save(state)
        store.event(
            "chain_blocked",
            issue=selection.issue.number,
            route=selection.route.label,
            hops=chain["hops"],
        )
        return False
    chain["hops"] = int(chain["hops"]) + 1
    store.save(state)
    return True


def dispatch_once(config: Config, *, shadow: bool = False, show_all: bool = False) -> int:
    store = StateStore(config.state_dir)
    github = GitHub(config)
    with store.lock() as acquired:
        if not acquired:
            return 0
        store.prune(config.log_retention_days)
        state = store.load()
        issues = github.list_open()
        try:
            candidates = select_candidates(issues, config)
        except DispatchError as exc:
            store.event("routing_error", error=str(exc))
            return 2

        if shadow:
            visible = candidates if show_all else candidates[:1]
            if not visible:
                print("No actionable issues.")
            for item in visible:
                print(
                    json.dumps(
                        {
                            "issue": item.issue.number,
                            "title": item.issue.title,
                            "route": item.route.label,
                            "role": item.route.role,
                            "source": item.source,
                            "priority": item.route.priority,
                        },
                        sort_keys=True,
                    )
                )
            return 0

        now = time.time()
        selection = next((item for item in candidates if retry_ready(item, state, now)), None)
        if selection is None:
            return 0

        ok, preflight_output = run_preflight(config)
        if not ok:
            store.event("preflight_blocked", output=preflight_output[-4000:])
            return 2
        if not _chain_allowed(selection, config, github, store, state):
            return 2

        try:
            current = github.view(selection.issue.number)
            rerouted = infer_route(current, config)
            if rerouted is None or rerouted[0].label != selection.route.label:
                store.event("claim_skipped", issue=current.number, reason="route changed")
                return 0
            if selection.source == "inferred":
                github.add_label(current.number, selection.route.label)
            github.add_label(current.number, "wip")
            github.comment(
                current.number,
                f"Dispatcher claimed this issue for `{selection.route.role}` at "
                f"{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}. Starting one "
                "persisted Codex role session from the current handoff state.",
            )
            current = github.view(current.number)
            selection = Selection(current, selection.route, "explicit")
            store.event(
                "role_started",
                issue=current.number,
                role=selection.route.role,
                route=selection.route.label,
            )
            exit_code, thread_id, log_path, summary_path, timed_out = run_role(
                selection, config, store, state
            )
            github.remove_label(current.number, "wip")
            after = github.view(current.number)
            transition = assess_transition(selection.route.label, after, config)
            if not transition.valid:
                reason = transition.outcome
                if timed_out:
                    reason = f"role timed out; {reason}"
                elif exit_code != 0:
                    reason = f"codex exited {exit_code}; {reason}"
                _mark_failure(
                    selection,
                    reason,
                    config,
                    github,
                    store,
                    state,
                    exit_code=exit_code,
                    thread_id=thread_id,
                )
                return 2

            _clear_failure(state, selection)
            if transition.outcome == "closed":
                state.setdefault("chains", {}).pop(str(current.number), None)
            record = {
                "issue": current.number,
                "role": selection.route.role,
                "route": selection.route.label,
                "status": "completed" if exit_code == 0 else "completed_with_nonzero_exit",
                "transition": transition.outcome,
                "next_route": transition.next_route,
                "exit_code": exit_code,
                "thread_id": thread_id,
                "log": str(log_path),
                "summary": str(summary_path),
                "finished_at": now_iso(),
            }
            _record_recent(state, record)
            store.save(state)
            store.event("role_completed", **record)
            github.comment(
                current.number,
                f"Dispatcher verified the `{selection.route.role}` transition from authoritative "
                f"GitHub state. Result: `{transition.outcome}`"
                + (f" → `{transition.next_route}`." if transition.next_route else ".")
                + (f" Persisted Codex thread: `{thread_id}`." if thread_id else ""),
            )
            return 0
        except DispatchError as exc:
            try:
                github.remove_label(selection.issue.number, "wip")
            except DispatchError:
                pass
            _mark_failure(selection, str(exc), config, github, store, state)
            return 2


def _static_config_problems(config: Config) -> list[str]:
    problems: list[str] = []
    for path, executable in (
        (config.cwd, False),
        (config.state_dir.parent, False),
        (config.codex_bin, True),
        (config.gh_bin, True),
        (config.git_bin, True),
        (config.preflight, True),
        (config.automation_registry, False),
    ):
        if not path.exists():
            problems.append(f"missing path: {path}")
        elif executable and not os.access(path, os.X_OK):
            problems.append(f"not executable: {path}")
    for route in config.routes.values():
        if not route.role_file.is_file():
            problems.append(f"missing role file: {route.role_file}")
        if not route.label.startswith(config.dispatch_prefix):
            problems.append(f"route outside dispatch prefix: {route.label}")
    if len({route.priority for route in config.routes.values()}) != len(config.routes):
        problems.append("route priorities must be unique")
    return problems


def _live_config_problems(config: Config) -> list[str]:
    try:
        raw = _run_json(
            [
                str(config.gh_bin),
                "label",
                "list",
                "--repo",
                config.repo,
                "--limit",
                "300",
                "--json",
                "name",
            ],
            cwd=config.cwd,
        )
        actual = {item["name"] for item in raw}
        expected = set(config.routes) | set(config.pending_labels)
        missing = sorted(expected - actual)
        return [f"missing GitHub labels: {', '.join(missing)}"] if missing else []
    except (DispatchError, json.JSONDecodeError) as exc:
        return [f"live label check failed: {exc}"]


def _installation_problems() -> list[str]:
    if not LAUNCH_AGENT.is_file():
        return [f"launch agent is not installed: {LAUNCH_AGENT}"]
    with LAUNCH_AGENT.open("rb") as handle:
        plist = plistlib.load(handle)
    problems = []
    if plist.get("Label") != EXPECTED_LAUNCH_LABEL:
        problems.append(f"unexpected launch agent label: {plist.get('Label')}")
    mode = stat.S_IMODE(LAUNCH_AGENT.stat().st_mode)
    if mode & 0o022:
        problems.append(f"launch agent is group/world writable: {oct(mode)}")
    return problems


def check_config(config: Config, *, live: bool = False, installed: bool = False) -> int:
    problems = _static_config_problems(config)
    if live:
        problems.extend(_live_config_problems(config))
    if installed:
        problems.extend(_installation_problems())

    if problems:
        for problem in problems:
            print(f"FAIL {problem}")
        return 1
    print(f"PASS dispatcher config: {len(config.routes)} routes, {len(config.inference)} rules")
    if live:
        print("PASS GitHub dispatch labels")
    if installed:
        print("PASS launch agent installation")
    return 0


def print_status(config: Config, *, as_json: bool = False) -> int:
    store = StateStore(config.state_dir)
    state = store.load()
    if as_json:
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0
    current = state.get("current")
    if current:
        print(
            f"ACTIVE  #{current['issue']}  {current['role']}  started {current['started_at']}\n"
            f"        log: {current['log']}"
        )
    else:
        print("ACTIVE  none")
    recent = state.get("recent", [])[-10:]
    if not recent:
        print("RECENT  none")
        return 0
    print("RECENT")
    for item in reversed(recent):
        print(
            f"  {item.get('finished_at', '?')}  #{item.get('issue')}  {item.get('role')}  "
            f"{item.get('status')}  next={item.get('next_route') or '-'}  "
            f"thread={item.get('thread_id') or '-'}"
        )
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--shadow", action="store_true", help="print routing without mutation")
    parser.add_argument("--all", action="store_true", help="show all shadow candidates")
    parser.add_argument("--check", action="store_true", help="validate local configuration")
    parser.add_argument("--live", action="store_true", help="include GitHub labels in --check")
    parser.add_argument("--installed", action="store_true", help="include LaunchAgent in --check")
    parser.add_argument("--status", action="store_true", help="show active and recent runs")
    parser.add_argument("--json-status", action="store_true", help="emit status as JSON")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    if args.check:
        return check_config(config, live=args.live, installed=args.installed)
    if args.status or args.json_status:
        return print_status(config, as_json=args.json_status)
    return dispatch_once(config, shadow=args.shadow, show_all=args.all)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DispatchError, OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"dispatcher error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
