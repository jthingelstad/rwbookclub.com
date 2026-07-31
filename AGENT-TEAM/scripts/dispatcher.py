#!/usr/bin/env python3
"""Event-driven AGENT-TEAM dispatcher for Oliver.

The process is intentionally deterministic until an issue has an actionable route. Idle polls
query GitHub and exit without invoking Codex. A real handoff asks the Codex app to create one
project-visible role thread, then verifies issue state instead of trusting final prose.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
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
    session_label: str
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
    codex_home: Path
    codex_project_id: str
    codex_state_db: Path
    codex_ipc_socket: Path
    relay_model: str
    relay_reasoning_effort: str
    session_start_timeout_seconds: int
    session_poll_seconds: int
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
            session_label=item["session_label"],
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
        codex_home=Path(raw["codex_home"]).expanduser().resolve(),
        codex_project_id=raw["codex_project_id"],
        codex_state_db=Path(raw["codex_state_db"]).expanduser().resolve(),
        codex_ipc_socket=Path(raw["codex_ipc_socket"]).expanduser().resolve(),
        relay_model=raw["relay_model"],
        relay_reasoning_effort=raw["relay_reasoning_effort"],
        session_start_timeout_seconds=int(raw["session_start_timeout_seconds"]),
        session_poll_seconds=int(raw["session_poll_seconds"]),
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
    session_title = f"#{issue.number} {route.session_label}"
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

Keep this Codex project thread easy to scan in the sidebar. At the start, use the Codex app title
tool on the current thread to set `{session_title}`. Update it only at meaningful checkpoints,
using `{session_title} · <phase>` and keeping the entire title at 24 characters or fewer. Before
your final response, use `{session_title} ✓` after a valid GitHub transition or
`{session_title} !` if the role is blocked or cannot complete the transition.

GitHub state—not your final prose—is the completion protocol. Before finishing:

1. Update issue #{issue.number} with evidence and the result.
2. Remove `{route.label}` and `wip`.
3. If another lane is required, add exactly one next `dispatch:*` label and retain the appropriate
   `needs-eval` / `needs-culture` / `needs-deploy` pending labels.
4. Close the issue only when no downstream lane or human decision remains. New product direction
   stops at `proposal`; blocked or ambiguous work stops with `blocked` or `needs-design`.
5. End with a clean repository. Never push a pre-existing commit.

Do not invoke another role directly. The dispatcher will re-read the authoritative issue state and
launch the next app-visible project thread when appropriate.
"""


def build_relay_prompt(selection: Selection, config: Config) -> str:
    """Build the short-lived automation prompt that opens a normal project thread."""

    session_title = f"#{selection.issue.number} {selection.route.session_label}"
    role_prompt = build_prompt(selection, config)
    return f"""Open exactly one normal Codex project thread for an Oliver AGENT-TEAM handoff.

Do not inspect the repository, GitHub, email, Discord, or production state. Do not do the role's
work yourself. Use the Codex app project list to confirm project
`{config.codex_project_id}`, then create one local project thread in that project with:

- model: `{selection.route.model}`
- reasoning effort: `{selection.route.reasoning_effort}`
- prompt: the complete text between ROLE PROMPT START and ROLE PROMPT END below

After creation, set the child thread title to `{session_title}`. Then archive this relay thread and
finish. If thread creation fails, do not create a substitute CLI session and do not do role work.

ROLE PROMPT START
{role_prompt}
ROLE PROMPT END
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


class CodexAppIpc:
    """Small owner-only client for the Codex app's local automation bridge."""

    def __init__(self, path: Path, *, timeout_seconds: float = 40) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds

    def validate(self) -> None:
        try:
            parent = self.path.parent.stat()
            endpoint = self.path.stat()
        except FileNotFoundError as exc:
            raise DispatchError(
                "Codex app IPC is unavailable; open Codex before the dispatcher retries"
            ) from exc
        uid = os.geteuid()
        if parent.st_uid != uid or not stat.S_ISDIR(parent.st_mode):
            raise DispatchError("Codex IPC directory is not an owner-controlled directory")
        if stat.S_IMODE(parent.st_mode) & 0o077:
            raise DispatchError("Codex IPC directory is accessible by another user")
        if endpoint.st_uid != uid or not stat.S_ISSOCK(endpoint.st_mode):
            raise DispatchError("Codex IPC endpoint is not an owner-controlled socket")
        if stat.S_IMODE(endpoint.st_mode) & 0o077:
            raise DispatchError("Codex IPC socket is accessible by another user")

    @staticmethod
    def _send(sock: socket.socket, message: dict[str, Any]) -> None:
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        sock.sendall(struct.pack("<I", len(payload)) + payload)

    @staticmethod
    def _receive(sock: socket.socket) -> dict[str, Any]:
        header = CodexAppIpc._receive_exact(sock, 4)
        length = struct.unpack("<I", header)[0]
        if length <= 0 or length > 16 * 1024 * 1024:
            raise DispatchError(f"invalid Codex IPC frame length: {length}")
        try:
            message = json.loads(CodexAppIpc._receive_exact(sock, length))
        except json.JSONDecodeError as exc:
            raise DispatchError("Codex IPC returned invalid JSON") from exc
        if not isinstance(message, dict):
            raise DispatchError("Codex IPC returned a non-object message")
        return message

    @staticmethod
    def _receive_exact(sock: socket.socket, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = sock.recv(remaining)
            if not chunk:
                raise DispatchError("Codex IPC connection closed unexpectedly")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _wait_response(self, sock: socket.socket, request_id: str) -> dict[str, Any]:
        while True:
            message = self._receive(sock)
            if message.get("type") == "response" and message.get("requestId") == request_id:
                return message

    def request(self, method: str, params: dict[str, Any]) -> Any:
        self.validate()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout_seconds)
                sock.connect(str(self.path))
                initialize_id = str(uuid.uuid4())
                self._send(
                    sock,
                    {
                        "type": "request",
                        "requestId": initialize_id,
                        "version": 0,
                        "method": "initialize",
                        "params": {"clientType": "oliver-agent-team-dispatcher"},
                    },
                )
                initialized = self._wait_response(sock, initialize_id)
                if initialized.get("resultType") != "success":
                    raise DispatchError(
                        f"Codex IPC initialize failed: {initialized.get('error', 'unknown error')}"
                    )
                client_id = initialized.get("result", {}).get("clientId")
                if not isinstance(client_id, str) or not client_id:
                    raise DispatchError("Codex IPC initialize returned no client id")

                request_id = str(uuid.uuid4())
                self._send(
                    sock,
                    {
                        "type": "request",
                        "requestId": request_id,
                        "sourceClientId": client_id,
                        "version": 0,
                        "method": method,
                        "params": params,
                        "timeoutMs": int(self.timeout_seconds * 1000),
                    },
                )
                response = self._wait_response(sock, request_id)
        except (OSError, socket.timeout) as exc:
            raise DispatchError(f"Codex app IPC request failed: {exc}") from exc
        if response.get("resultType") != "success":
            raise DispatchError(
                f"Codex app rejected {method}: {response.get('error', 'unknown error')}"
            )
        return response.get("result")


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _write_transient_automation(
    selection: Selection, config: Config, automation_id: str
) -> Path:
    root = config.codex_home / "automations"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    automation_dir = root / automation_id
    automation_dir.mkdir(mode=0o700)
    now_ms = int(time.time() * 1000)
    session_title = f"#{selection.issue.number} {selection.route.session_label}"
    content = "\n".join(
        [
            "version = 1",
            f"id = {_toml_string(automation_id)}",
            'kind = "cron"',
            f"name = {_toml_string(session_title + ' relay')}",
            f"prompt = {_toml_string(build_relay_prompt(selection, config))}",
            'status = "PAUSED"',
            'rrule = "RRULE:FREQ=WEEKLY;BYHOUR=3;BYMINUTE=17;BYDAY=SU"',
            f"model = {_toml_string(config.relay_model)}",
            f"reasoning_effort = {_toml_string(config.relay_reasoning_effort)}",
            'execution_environment = "local"',
            "target = { type = \"project\", project_id = "
            f"{_toml_string(config.codex_project_id)} }}",
            f"cwds = [{_toml_string(str(config.cwd))}]",
            f"created_at = {now_ms}",
            f"updated_at = {now_ms}",
            "",
        ]
    )
    path = automation_dir / "automation.toml"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return automation_dir


def _cleanup_transient_automation(
    automation_id: str, automation_dir: Path, ipc: CodexAppIpc
) -> None:
    deleted = False
    try:
        result = ipc.request("automation-delete", {"id": automation_id})
        deleted = isinstance(result, dict) and result.get("success") is True
    except DispatchError:
        pass
    if deleted or not automation_dir.exists():
        return
    # The exact directory was created by this run and contains no durable role evidence.
    for name in ("automation.toml", "memory.md"):
        path = automation_dir / name
        if path.is_file():
            path.unlink()
    try:
        automation_dir.rmdir()
    except OSError:
        pass


def _find_role_thread(
    selection: Selection, config: Config, *, created_after_ms: int
) -> tuple[str, Path] | None:
    uri = f"file:{config.codex_state_db}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=2) as conn:
            row = conn.execute(
                """
                SELECT id, rollout_path
                FROM threads
                WHERE cwd = ?
                  AND thread_source = 'subagent'
                  AND created_at_ms >= ?
                  AND first_user_message LIKE ?
                  AND first_user_message LIKE ?
                ORDER BY created_at_ms DESC, id DESC
                LIMIT 1
                """,
                (
                    str(config.cwd),
                    created_after_ms,
                    f"%Work exactly one issue: #{selection.issue.number} %",
                    f"%Current handoff: `{selection.route.label}`%",
                ),
            ).fetchone()
    except sqlite3.Error as exc:
        raise DispatchError(f"could not read Codex thread index: {exc}") from exc
    if row is None:
        return None
    rollout_path = Path(row[1]).expanduser().resolve()
    try:
        rollout_path.relative_to(config.codex_home)
    except ValueError as exc:
        raise DispatchError("Codex returned a rollout path outside its owner directory") from exc
    return str(row[0]), rollout_path


def _rollout_complete(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 512 * 1024))
            data = handle.read()
    except OSError:
        return False
    for raw_line in data.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "event_msg":
            continue
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "task_complete":
            return True
    return False


def run_role(
    selection: Selection, config: Config, store: StateStore, state: dict[str, Any]
) -> tuple[int, str | None, Path, Path, bool]:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    automation_id = (
        f"oliver-dispatch-{selection.issue.number}-{selection.route.session_label.lower()}-"
        f"{uuid.uuid4().hex[:8]}"
    )
    stem = (
        f"{stamp}-issue-{selection.issue.number}-{selection.route.role}-"
        f"{automation_id.rsplit('-', 1)[-1]}"
    )
    summary_path = store.runs_dir / f"{stem}.summary.md"
    state["current"] = {
        "issue": selection.issue.number,
        "title": selection.issue.title,
        "role": selection.route.role,
        "route": selection.route.label,
        "started_at": now_iso(),
        "log": None,
        "summary": str(summary_path),
        "thread_id": None,
    }
    store.save(state)

    ipc = CodexAppIpc(config.codex_ipc_socket)
    automation_dir: Path | None = None
    started_ms = int(time.time() * 1000) - 1000
    try:
        automation_dir = _write_transient_automation(selection, config, automation_id)
        ipc.request(
            "automation-run-now",
            {"id": automation_id, "collaborationMode": None, "permissions": None},
        )
        start_deadline = time.monotonic() + config.session_start_timeout_seconds
        thread: tuple[str, Path] | None = None
        while time.monotonic() < start_deadline:
            thread = _find_role_thread(selection, config, created_after_ms=started_ms)
            if thread is not None:
                break
            time.sleep(config.session_poll_seconds)
        if thread is None:
            raise DispatchError("Codex relay did not create an app-visible role thread")

        thread_id, log_path = thread
        state["current"].update({"thread_id": thread_id, "log": str(log_path)})
        store.save(state)
        summary_path.write_text(
            f"App-visible Codex thread: {thread_id}\nRollout: {log_path}\n",
            encoding="utf-8",
        )
        os.chmod(summary_path, 0o600)

        _cleanup_transient_automation(automation_id, automation_dir, ipc)
        automation_dir = None

        deadline = time.monotonic() + config.role_timeout_seconds
        while time.monotonic() < deadline:
            if _rollout_complete(log_path):
                return 0, thread_id, log_path, summary_path, False
            time.sleep(config.session_poll_seconds)
        return 124, thread_id, log_path, summary_path, True
    finally:
        if automation_dir is not None:
            _cleanup_transient_automation(automation_id, automation_dir, ipc)


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
                "app-visible Codex project thread from the current handoff state.",
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
                    reason = f"Codex app thread ended with status {exit_code}; {reason}"
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
                + (f" App-visible Codex thread: `{thread_id}`." if thread_id else ""),
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
        (config.codex_home, False),
        (config.codex_home / "automations", False),
        (config.codex_state_db, False),
        (config.codex_ipc_socket.parent, False),
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
        if not route.session_label or len(route.session_label) > 10:
            problems.append(f"invalid compact session label: {route.session_label!r}")
    if len({route.priority for route in config.routes.values()}) != len(config.routes):
        problems.append("route priorities must be unique")
    return problems


def _live_config_problems(config: Config) -> list[str]:
    problems: list[str] = []
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
        if missing:
            problems.append(f"missing GitHub labels: {', '.join(missing)}")
    except (DispatchError, json.JSONDecodeError) as exc:
        problems.append(f"live label check failed: {exc}")
    try:
        CodexAppIpc(config.codex_ipc_socket).validate()
    except DispatchError as exc:
        problems.append(f"Codex app bridge unavailable: {exc}")
    return problems


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
