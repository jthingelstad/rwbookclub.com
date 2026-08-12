from __future__ import annotations

import os
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "AGENT-TEAM/scripts/preflight.sh"


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
    entries = plan["activities"]

    assert plan["version"] == 1
    assert len(entries) == 3
    assert {entry["objective"] for entry in entries} == {"run", "club", "agent"}
    assert all(entry["status"] == "PAUSED" for entry in entries)
    assert all((ROOT / entry["role"]).is_file() for entry in entries)
    assert len({entry["id"] for entry in entries}) == 3
    assert all(entry["execution_environment"] == "local" for entry in entries)
    assert all(entry["cwd"] == str(ROOT) for entry in entries)
    assert all(entry["rrule"].startswith("RRULE:FREQ=") for entry in entries)


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
