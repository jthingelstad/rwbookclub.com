from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "AGENT-TEAM/scripts/preflight.sh"


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


automation_audit = load_module("oliver_automation_audit", "AGENT-TEAM/scripts/automation_audit.py")
objective_lease = load_module("oliver_objective_lease", "AGENT-TEAM/scripts/objective_lease.py")


def run(command: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd)


def make_remote(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    git(tmp_path, "init", "--bare", str(origin))
    git(tmp_path, "clone", str(origin), str(seed))
    git(seed, "config", "user.name", "Agent Team Test")
    git(seed, "config", "user.email", "agent-team@example.invalid")
    git(seed, "commit", "--allow-empty", "-m", "base")
    git(seed, "branch", "-M", "main")
    git(seed, "push", "-u", "origin", "main")
    git(tmp_path, f"--git-dir={origin}", "symbolic-ref", "HEAD", "refs/heads/main")
    return origin, seed


def clone_remote(tmp_path: Path, origin: Path) -> Path:
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", str(origin), str(checkout))
    git(checkout, "config", "user.name", "Agent Team Test")
    git(checkout, "config", "user.email", "agent-team@example.invalid")
    return checkout


def preflight(checkout: Path) -> subprocess.CompletedProcess[str]:
    return run([str(PREFLIGHT)], checkout, check=False)


def test_registry_has_three_paused_objective_owners():
    plan = tomllib.loads((ROOT / "AGENT-TEAM/automations.toml").read_text())
    entries = plan["automation"]

    assert plan["version"] == 2
    assert plan["repo"] == str(ROOT)
    assert len(entries) == 3
    assert {entry["objective"] for entry in entries} == {"run", "club", "agent"}
    assert all(entry["status"] == "PAUSED" for entry in entries)
    assert all((ROOT / entry["objective_file"]).is_file() for entry in entries)
    assert len({entry["id"] for entry in entries}) == 3
    assert all(entry["rrule"].startswith("RRULE:FREQ=") for entry in entries)


def test_automation_prompt_uses_the_common_objective_contract():
    plan = tomllib.loads((ROOT / "AGENT-TEAM/automations.toml").read_text())
    for entry in plan["automation"]:
        prompt = automation_audit.prompt(entry)
        assert entry["objective"] in prompt
        assert "Measure current evidence" in prompt
        assert "source fix" in prompt
        assert "objective lease only before mutation" in prompt
        assert "human and privacy boundaries" in prompt
        assert "one replace-in-place Latest run" in prompt


def test_workflow_pins_end_to_end_ownership_and_member_boundary():
    workflow = (ROOT / "AGENT-TEAM/WORKFLOW.md").read_text()
    readme = (ROOT / "AGENT-TEAM/README.md").read_text()

    assert (
        "Run Oliver owns the deployment, restart, and technical-health acceptance standard"
        in workflow
    )
    assert "originating objective owns semantic acceptance" in workflow
    assert "Issues are an exception ledger" in readme
    assert "Never manufacture a Discord message, DM, email" in readme
    assert "Current state" in workflow
    assert "Active watches" in workflow
    assert "replace-in-place `Latest run`" in workflow
    assert "objective_lease.py claim" in workflow
    assert "Outcome: HEALTHY | CHANGED | WATCHING | BLOCKED | NEEDS JAMIE" in workflow
    assert "Run <objective> now and own the highest-impact measured gap." in readme


def test_checkout_lease_is_atomic_and_owner_scoped(tmp_path: Path, monkeypatch):
    lease_path = tmp_path / ".git" / "agent-team-objective-lease.json"
    lease_path.parent.mkdir()
    monkeypatch.setattr(objective_lease, "LEASE_PATH", lease_path)
    claimed = objective_lease.claim(
        "run",
        now=datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc),
        holder_id="thread-1",
        holder_pid=4321,
        hostname="test-host",
        starting_head="abc123",
        lease_id="lease-1",
    )
    assert claimed["lease_id"] == "lease-1"
    assert lease_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SystemExit, match="already held"):
        objective_lease.claim("club")
    with pytest.raises(SystemExit, match="another run"):
        objective_lease.release("run", lease_id="wrong")
    monkeypatch.setattr(objective_lease, "_git", lambda *args: "")
    objective_lease.release("run", lease_id="lease-1")
    assert not lease_path.exists()


def test_stale_lease_requires_proof_of_inactive_holder(tmp_path: Path, monkeypatch):
    lease_path = tmp_path / ".git" / "agent-team-objective-lease.json"
    lease_path.parent.mkdir()
    monkeypatch.setattr(objective_lease, "LEASE_PATH", lease_path)
    monkeypatch.setattr(objective_lease.socket, "gethostname", lambda: "test-host")
    objective_lease.claim(
        "club",
        now=datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc),
        holder_id="thread-2",
        holder_pid=9876,
        hostname="test-host",
        starting_head="abc123",
    )
    checkout = {"dirty": False, "head": "abc123"}

    def fake_git(*args: str) -> str:
        if args == ("status", "--porcelain"):
            return " M file" if checkout["dirty"] else ""
        if args == ("rev-parse", "HEAD"):
            return checkout["head"]
        raise AssertionError(args)

    monkeypatch.setattr(objective_lease, "_git", fake_git)
    monkeypatch.setattr(objective_lease, "_process_exists", lambda pid: True)
    with pytest.raises(SystemExit, match="still active"):
        objective_lease.clear_stale(hours=8, now=datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(objective_lease, "_process_exists", lambda pid: False)
    objective_lease.clear_stale(hours=8, now=datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc))
    assert not lease_path.exists()


def test_registry_passes_the_common_contract_audit():
    plan = tomllib.loads((ROOT / "AGENT-TEAM/automations.toml").read_text())
    assert automation_audit.validate(plan) == []


def test_objective_issue_templates_apply_exactly_one_owner():
    template_dir = ROOT / ".github/ISSUE_TEMPLATE"
    expected = {
        "run-oliver.md": "objective:run",
        "understand-club.md": "objective:club",
        "improve-oliver.md": "objective:agent",
    }

    assert {path.name for path in template_dir.glob("*.md")} == set(expected)
    for filename, objective in expected.items():
        text = (template_dir / filename).read_text()
        front_matter = text.split("---", 2)[1]
        assert f'labels: ["{objective}"]' in front_matter
        assert 'labels: ["generated"]' not in front_matter


def test_issue_templates_do_not_reference_retired_workflow():
    combined = "\n".join(
        path.read_text() for path in (ROOT / ".github/ISSUE_TEMPLATE").glob("*.md")
    )
    retired_terms = [
        "Build Manager",
        "Operations Manager",
        "Product Manager",
        "`proposal`",
        "`approved`",
        "`ready`",
        "dispatch:",
        "`wip`",
    ]
    assert all(term not in combined for term in retired_terms)


def test_runbooks_name_executable_commands():
    workflow = (ROOT / "AGENT-TEAM/WORKFLOW.md").read_text()
    improve = (ROOT / "AGENT-TEAM/improve-oliver.md").read_text()
    verify = ROOT / "AGENT-TEAM/scripts/verify.sh"

    assert "AGENT-TEAM/scripts/verify.sh" in workflow
    assert "uv run --locked python scripts/evaluator_evidence.py --days 7" in improve
    assert "uv run --locked python scripts/eval_oliver.py --round <N> --goldens-only" in improve
    assert verify.is_file()
    assert os.stat(verify).st_mode & stat.S_IXUSR


def test_preflight_accepts_only_clean_synchronized_branch(tmp_path: Path):
    origin, _ = make_remote(tmp_path)
    checkout = clone_remote(tmp_path, origin)

    result = preflight(checkout)

    assert result.returncode == 0
    assert "clean and in sync — safe to work" in result.stdout


@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        ("ahead", "AHEAD"),
        ("behind", "BEHIND"),
        ("diverged", "DIVERGED"),
        ("detached", "DETACHED HEAD"),
        ("dirty", "DIRTY"),
        ("fetch-failure", "git fetch failed"),
        ("no-upstream", "no upstream configured"),
        ("non-main", "only from main"),
    ],
)
def test_preflight_rejects_unsafe_states(tmp_path: Path, scenario: str, message: str):
    origin, seed = make_remote(tmp_path)
    checkout = clone_remote(tmp_path, origin)

    if scenario in {"ahead", "diverged"}:
        git(checkout, "commit", "--allow-empty", "-m", "local")
    if scenario in {"behind", "diverged"}:
        git(seed, "commit", "--allow-empty", "-m", "remote")
        git(seed, "push", "origin", "main")
    elif scenario == "detached":
        git(checkout, "checkout", "--detach")
    elif scenario == "dirty":
        (checkout / "unexpected.txt").write_text("unexpected\n")
    elif scenario == "fetch-failure":
        git(checkout, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    elif scenario == "no-upstream":
        git(checkout, "checkout", "-b", "scratch")
    elif scenario == "non-main":
        git(checkout, "checkout", "-b", "feature")
        git(checkout, "push", "-u", "origin", "feature")

    result = preflight(checkout)

    assert result.returncode != 0
    assert message in result.stdout
    assert "safe to work" not in result.stdout


def test_retired_dispatcher_and_job_roles_are_absent():
    retired = [
        "build-manager.md",
        "club-ethnographer.md",
        "dispatch.toml",
        "dispatcher.md",
        "evaluator.md",
        "manager.md",
        "operations-manager.md",
        "product-manager.md",
        "scripts/dispatcher-admin.sh",
        "scripts/dispatcher.py",
    ]

    assert all(not (ROOT / "AGENT-TEAM" / path).exists() for path in retired)
    assert not (ROOT / ".github/ISSUE_TEMPLATE/bug.md").exists()
    assert not (ROOT / ".github/ISSUE_TEMPLATE/task.md").exists()
    assert not (ROOT / ".github/ISSUE_TEMPLATE/proposal.md").exists()
