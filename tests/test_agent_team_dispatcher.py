from __future__ import annotations

import importlib.util
import plistlib
import sys
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
    assert {route.session_label for route in config.routes.values()} == {
        "Ops",
        "Culture",
        "Eval",
        "Build",
        "Product",
        "Team",
    }


def test_dispatcher_polls_every_fifteen_minutes(config):
    with (ROOT / "AGENT-TEAM/ops/com.rwbookclub.agent-team-dispatcher.plist").open("rb") as handle:
        launch_agent = plistlib.load(handle)

    assert config.poll_interval_seconds == 15 * 60
    assert launch_agent["StartInterval"] == config.poll_interval_seconds


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


def test_relay_prompt_creates_one_normal_project_thread(config):
    selection = dispatcher.Selection(
        issue(81, "dispatch:evaluator", "wip"),
        config.routes["dispatch:evaluator"],
        "explicit",
    )
    prompt = dispatcher.build_relay_prompt(selection, config)
    assert "create one local project thread" in prompt
    assert config.codex_project_id in prompt
    assert "set the child thread title to `#81 Eval`" in prompt
    assert "archive this relay thread" in prompt
    assert "Do not inspect the repository" in prompt


def test_transient_relay_automation_is_paused_and_project_scoped(config, tmp_path):
    local_config = replace(config, codex_home=tmp_path / ".codex")
    selection = dispatcher.Selection(
        issue(81, "dispatch:evaluator", "wip"),
        local_config.routes["dispatch:evaluator"],
        "explicit",
    )
    automation_dir = dispatcher._write_transient_automation(
        selection, local_config, "oliver-dispatch-test"
    )
    with (automation_dir / "automation.toml").open("rb") as handle:
        raw = dispatcher.tomllib.load(handle)
    assert raw["status"] == "PAUSED"
    assert raw["target"] == {
        "type": "project",
        "project_id": config.codex_project_id,
    }
    assert raw["name"] == "#81 Eval relay"
    assert raw["model"] == config.relay_model


def test_transient_relay_cleanup_falls_back_when_app_does_not_delete(config, tmp_path):
    local_config = replace(config, codex_home=tmp_path / ".codex")
    selection = dispatcher.Selection(
        issue(81, "dispatch:evaluator", "wip"),
        local_config.routes["dispatch:evaluator"],
        "explicit",
    )
    automation_dir = dispatcher._write_transient_automation(
        selection, local_config, "oliver-dispatch-test"
    )

    class Ipc:
        def request(self, method, params):
            assert method == "automation-delete"
            assert params == {"id": "oliver-dispatch-test"}
            return {"success": False}

    dispatcher._cleanup_transient_automation("oliver-dispatch-test", automation_dir, Ipc())
    assert not automation_dir.exists()


def test_codex_ipc_frame_round_trip():
    left, right = dispatcher.socket.socketpair()
    try:
        dispatcher.CodexAppIpc._send(left, {"type": "test", "value": 7})
        assert dispatcher.CodexAppIpc._receive(right) == {"type": "test", "value": 7}
    finally:
        left.close()
        right.close()


def test_rollout_completion_detects_task_complete(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        '{"type":"event_msg","payload":{"type":"agent_message"}}\n'
        '{"type":"event_msg","payload":{"type":"task_complete"}}\n',
        encoding="utf-8",
    )
    assert dispatcher._rollout_complete(rollout)


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
