"""The charter (SOUL/PURPOSE/PROCESS) is wired into Oliver's system prompt."""
from pathlib import Path

import pytest

from agent import persona


def test_charter_includes_all_three_files():
    # A load-bearing phrase from each charter file must reach the prompt.
    assert "de facto sixth member" in persona.CHARTER          # SOUL
    assert "the meeting is the point" in persona.CHARTER        # PURPOSE
    assert "deterministic" in persona.CHARTER                   # PROCESS (rotation)


def test_charter_uses_section_headings_not_file_titles():
    assert "# WHO YOU ARE" in persona.CHARTER
    assert "# WHY YOU EXIST" in persona.CHARTER
    assert "# HOW YOU OPERATE" in persona.CHARTER
    # The files' own "# SOUL.md - ..." titles are stripped, and the meta
    # "Document Roles" section is gone.
    assert "SOUL.md - Who Oliver Is" not in persona.CHARTER
    assert "Document Roles" not in persona.CHARTER


def test_charter_leads_the_system_prompt():
    from agent import oliver

    blocks = oliver._system_blocks()
    assert len(blocks) == 2
    block0 = blocks[0]["text"]
    assert block0.startswith("# WHO YOU ARE")
    assert "witty but not snarky" in block0          # charter voice
    assert "OPERATING MECHANICS" in block0           # operational scaffolding kept
    assert "Triumph of the City" in block0           # answer-shape example kept
    # The dynamic club overview keeps the cache breakpoint.
    assert blocks[1]["cache_control"]["type"] == "ephemeral"


def test_meeting_date_grounding_rule_present():
    from agent import oliver

    block0 = oliver._system_blocks()[0]["text"]
    # Oliver must verify a meeting date against current_meeting_status, not echo a member's.
    assert "current_meeting_status" in block0
    assert "NEVER repeat a date" in block0


def test_missing_charter_file_fails_loudly(monkeypatch, tmp_path):
    # Oliver must refuse to start voiceless rather than silently drop identity.
    monkeypatch.setattr(persona, "_DOCS", Path(tmp_path))
    with pytest.raises(FileNotFoundError):
        persona._load()
