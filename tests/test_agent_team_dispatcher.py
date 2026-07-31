from __future__ import annotations

import importlib.util
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "AGENT-TEAM/scripts/dispatcher.py"
SPEC = importlib.util.spec_from_file_location("agent_team_dispatcher", MODULE_PATH)
assert SPEC and SPEC.loader
dispatcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dispatcher
SPEC.loader.exec_module(dispatcher)


@pytest.fixture
def config():
    return dispatcher.load_config(ROOT / "AGENT-TEAM/dispatch.toml")


def issue(number: int, *labels: str, state: str = "OPEN"):
    return dispatcher.Issue(
        number=number,
        title=f"Issue {number}",
        state=state,
        labels=frozenset(labels),
        created_at=f"2026-07-{number:02d}T00:00:00Z",
        updated_at=f"2026-07-{number:02d}T00:00:00Z",
        url=f"https://example.test/issues/{number}",
    )


def test_config_has_unique_existing_routes(config):
    assert set(config.routes) == {
        "dispatch:operations",
        "dispatch:culture",
        "dispatch:evaluator",
        "dispatch:build",
        "dispatch:product",
        "dispatch:manager",
    }
    assert len({route.priority for route in config.routes.values()}) == len(config.routes)
    assert all(route.role_file.is_file() for route in config.routes.values())


def test_relative_role_files_resolve_from_config_checkout(tmp_path):
    checkout = tmp_path / "checkout"
    config_dir = checkout / "AGENT-TEAM"
    config_dir.mkdir(parents=True)
    runtime_cwd = tmp_path / "runtime"
    runtime_cwd.mkdir()

    source = (ROOT / "AGENT-TEAM/dispatch.toml").read_text(encoding="utf-8")
    source = "\n".join(
        f'cwd = "{runtime_cwd}"' if line.startswith("cwd = ") else line
        for line in source.splitlines()
    )
    config_path = config_dir / "dispatch.toml"
    config_path.write_text(source + "\n", encoding="utf-8")

    raw = tomllib.loads(source)
    expected_role_files = {checkout / route["role_file"] for route in raw["routes"]}
    for role_file in expected_role_files:
        role_file.touch()

    loaded = dispatcher.load_config(config_path)

    assert loaded.cwd == runtime_cwd
    assert {route.role_file for route in loaded.routes.values()} == expected_role_files
    assert all(route.role_file.is_file() for route in loaded.routes.values())


def test_app_dispatcher_heartbeat_is_the_only_queue_poller(config):
    with (ROOT / "AGENT-TEAM/automations.toml").open("rb") as handle:
        registry = tomllib.load(handle)
    activities = {item["id"]: item for item in registry["activities"]}

    heartbeat = activities["oliver-dispatcher"]
    assert heartbeat["status"] == "ACTIVE"
    assert heartbeat["rrule"] == "FREQ=MINUTELY;INTERVAL=15"
    assert heartbeat["role"] == "AGENT-TEAM/dispatcher.md"
    assert config.poll_interval_seconds == 15 * 60
    assert all(
        activities[activity_id]["status"] == "PAUSED"
        for activity_id in (
            "oliver-operations-manager",
            "oliver-build-manager",
            "oliver-product-manager",
            "oliver-club-ethnographer",
        )
    )


def test_explicit_dispatch_wins_over_classification(config):
    selected = dispatcher.infer_route(issue(1, "bug", "eval", "dispatch:evaluator"), config)
    assert selected is not None
    route, source = selected
    assert route.label == "dispatch:evaluator"
    assert source == "explicit"


@pytest.mark.parametrize("stop", ["proposal", "blocked", "needs-design", "wip"])
def test_stop_labels_prevent_dispatch(config, stop):
    assert dispatcher.infer_route(issue(1, "needs-deploy", stop), config) is None


def test_needs_deploy_is_highest_priority_inference(config):
    selected = dispatcher.infer_route(issue(1, "bug", "needs-deploy"), config)
    assert selected is not None
    assert selected[0].label == "dispatch:operations"
    assert selected[1] == "inferred"


def test_pending_eval_prevents_build_reinference(config):
    selected = dispatcher.infer_route(issue(1, "bug", "eval", "needs-eval"), config)
    assert selected is not None
    assert selected[0].label == "dispatch:evaluator"


def test_candidate_priority_beats_issue_age(config):
    candidates = dispatcher.select_candidates(
        [
            issue(1, "dispatch:evaluator"),
            issue(2, "dispatch:operations"),
        ],
        config,
    )
    assert [item.issue.number for item in candidates] == [2, 1]


def test_multiple_explicit_routes_fail_closed(config):
    with pytest.raises(dispatcher.DispatchError, match="multiple dispatch labels"):
        dispatcher.infer_route(issue(1, "dispatch:build", "dispatch:operations"), config)


def test_prompt_accepts_dispatcher_owned_wip_and_requires_state_transition(config):
    selection = dispatcher.Selection(
        issue(81, "dispatch:operations", "wip"),
        config.routes["dispatch:operations"],
        "explicit",
    )
    prompt = dispatcher.build_prompt(selection, config)
    assert "already added `wip`" in prompt
    assert "skip it because it is labeled `wip`" in prompt
    assert "`dispatch:operations` and `wip`" in prompt
    assert "exactly one next `dispatch:*` label" in prompt
    assert "`#81 Ops`" in prompt
    assert "`#81 Ops · <phase>`" in prompt
    assert "`#81 Ops ✓`" in prompt


def test_cli_refuses_non_visible_automatic_launch(monkeypatch, capsys):
    called = False

    def unexpected_dispatch(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("automatic dispatch should be unreachable")

    monkeypatch.setattr(dispatcher, "dispatch_once", unexpected_dispatch)
    assert dispatcher.main(["--config", str(ROOT / "AGENT-TEAM/dispatch.toml")]) == 2
    assert not called
    assert "Direct role launch is disabled" in capsys.readouterr().err


def test_transition_requires_current_route_to_clear(config):
    transition = dispatcher.assess_transition(
        "dispatch:operations", issue(81, "dispatch:operations"), config
    )
    assert not transition.valid


def test_transition_accepts_one_next_route(config):
    transition = dispatcher.assess_transition(
        "dispatch:operations", issue(81, "needs-eval", "dispatch:evaluator"), config
    )
    assert transition.valid
    assert transition.outcome == "handoff"
    assert transition.next_route == "dispatch:evaluator"


def test_open_orphan_is_not_success(config):
    transition = dispatcher.assess_transition("dispatch:build", issue(81, "bug"), config)
    assert not transition.valid
    assert "no next dispatch" in transition.outcome


def test_closed_issue_is_success(config):
    transition = dispatcher.assess_transition(
        "dispatch:evaluator", issue(79, state="CLOSED"), config
    )
    assert transition.valid
    assert transition.outcome == "closed"


class FakeGitHub:
    def __init__(self, config, current=None):
        self.config = config
        self.current = current
        self.comments = []

    def list_open(self):
        return [self.current] if self.current and self.current.state == "OPEN" else []

    def view(self, number):
        assert self.current and self.current.number == number
        return self.current

    def add_label(self, number, label):
        self._labels(number, add={label})

    def remove_label(self, number, label):
        self._labels(number, remove={label})

    def _labels(self, number, add=frozenset(), remove=frozenset()):
        assert self.current and self.current.number == number
        self.current = replace(
            self.current,
            labels=(self.current.labels | set(add)) - set(remove),
        )

    def comment(self, number, body):
        assert self.current and self.current.number == number
        self.comments.append(body)


def test_idle_poll_invokes_no_role(monkeypatch, config, tmp_path):
    fake = FakeGitHub(config)
    monkeypatch.setattr(dispatcher, "GitHub", lambda _config: fake)
    called = False

    def unexpected_role(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("idle poll invoked Codex")

    monkeypatch.setattr(dispatcher, "run_role", unexpected_role)
    local_config = replace(config, state_dir=tmp_path / "state")
    assert dispatcher.dispatch_once(local_config) == 0
    assert not called
    assert not (local_config.state_dir / "state.json").exists()


def test_active_poll_claims_and_accepts_authoritative_handoff(monkeypatch, config, tmp_path):
    fake = FakeGitHub(config, issue(81, "dispatch:operations"))
    monkeypatch.setattr(dispatcher, "GitHub", lambda _config: fake)
    monkeypatch.setattr(dispatcher, "run_preflight", lambda _config: (True, "clean"))

    def successful_role(selection, _config, store, state):
        fake.remove_label(selection.issue.number, "dispatch:operations")
        fake.add_label(selection.issue.number, "needs-eval")
        fake.add_label(selection.issue.number, "dispatch:evaluator")
        log = store.runs_dir / "run.jsonl"
        summary = store.runs_dir / "run.summary.md"
        log.write_text("", encoding="utf-8")
        summary.write_text("done", encoding="utf-8")
        return 0, "thread-123", log, summary, False

    monkeypatch.setattr(dispatcher, "run_role", successful_role)
    local_config = replace(config, state_dir=tmp_path / "state")
    assert dispatcher.dispatch_once(local_config) == 0
    assert fake.current
    assert "wip" not in fake.current.labels
    assert "dispatch:operations" not in fake.current.labels
    assert "dispatch:evaluator" in fake.current.labels
    state = dispatcher.StateStore(local_config.state_dir).load()
    assert state["recent"][-1]["thread_id"] == "thread-123"
    assert state["recent"][-1]["next_route"] == "dispatch:evaluator"


def test_invalid_role_transition_releases_claim_and_backs_off(monkeypatch, config, tmp_path):
    fake = FakeGitHub(config, issue(81, "dispatch:operations"))
    monkeypatch.setattr(dispatcher, "GitHub", lambda _config: fake)
    monkeypatch.setattr(dispatcher, "run_preflight", lambda _config: (True, "clean"))

    def no_handoff(selection, _config, store, state):
        log = store.runs_dir / "run.jsonl"
        summary = store.runs_dir / "run.summary.md"
        log.write_text("", encoding="utf-8")
        summary.write_text("stopped early", encoding="utf-8")
        return 0, "thread-456", log, summary, False

    monkeypatch.setattr(dispatcher, "run_role", no_handoff)
    local_config = replace(config, state_dir=tmp_path / "state")
    assert dispatcher.dispatch_once(local_config) == 2
    assert fake.current
    assert "wip" not in fake.current.labels
    assert "dispatch:operations" in fake.current.labels
    state = dispatcher.StateStore(local_config.state_dir).load()
    failure = state["failures"]["81:dispatch:operations"]
    assert failure["attempts"] == 1
    assert "current dispatch label" in failure["reason"]
