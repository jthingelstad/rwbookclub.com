from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_registry_has_three_paused_objective_owners():
    plan = tomllib.loads((ROOT / "AGENT-TEAM/automations.toml").read_text())
    entries = plan["activities"]

    assert plan["version"] == 1
    assert len(entries) == 3
    assert {entry["objective"] for entry in entries} == {"run", "club", "agent"}
    assert all(entry["status"] == "PAUSED" for entry in entries)
    assert all((ROOT / entry["role"]).is_file() for entry in entries)


def test_workflow_pins_end_to_end_ownership_and_member_boundary():
    workflow = (ROOT / "AGENT-TEAM/WORKFLOW.md").read_text()
    readme = (ROOT / "AGENT-TEAM/README.md").read_text()

    assert "Run Oliver owns deployment, restart, and technical-health acceptance" in workflow
    assert "originating objective owns semantic acceptance" in workflow
    assert "Issues are an exception ledger" in readme
    assert "Never manufacture a Discord message, DM, email" in readme
    assert "Current state" in workflow
    assert "Active watches" in workflow
    assert "replace-in-place `Latest run`" in workflow


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
