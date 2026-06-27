"""release_notes builds the source material from git + docs, prompts Oliver for a
first-person announcement, and pulls the Oliver-written subject + body out of tags."""
from agent import publish
from agent.club import release_notes as rn

MATERIAL = {
    "window": "the last 5 days",
    "days": 5,
    "since_commit": None,
    "count": 3,
    "truncated": False,
    "merges": "- abc123 Merge feature-x: a shiny thing",
    "commits": "### abc123 2026-06-26 Add a shiny thing\nbody detail here\n",
    "changed_docs": ["agent/docs/ROADMAP.md"],
    "roadmap": "# Roadmap\nPhase 7 — shiny things.",
}


def test_git_output_reads_history():
    # The real repo has history; a read-only log returns a non-empty line.
    out = publish.git_output(["log", "--oneline", "-1"])
    assert out.strip()


def test_git_output_empty_on_bad_command():
    assert publish.git_output(["log", "--this-flag-does-not-exist"]) == ""


def test_prompt_embeds_material_and_contract():
    p = rn.release_notes_prompt(MATERIAL)
    assert "the last 5 days" in p
    for section in ("## The story", "## Features", "## Release Notes"):
        assert section in p
    assert "first person" in p.lower()
    assert "<subject>" in p and "<email>" in p
    # Source material is actually included, not just described.
    assert "a shiny thing" in p
    assert "Phase 7" in p
    # New contract: an opening framing sentence, a terse changelog, and a closing sign-off.
    assert "OPEN:" in p and "framing sentence" in p
    assert "CLOSE:" in p and "sign-off" in p
    assert "terse changelog" in p


def test_prompt_uses_since_window():
    p = rn.release_notes_prompt({**MATERIAL, "window": "since commit deadbee 2026-06-27 Did a thing"})
    assert "since commit deadbee" in p


def test_prompt_notes_truncation():
    big = {**MATERIAL, "count": 200, "truncated": True}
    assert "200 commits" in rn.release_notes_prompt(big)


def test_release_notes_email_extracts_oliver_subject_and_body(monkeypatch):
    monkeypatch.setattr(rn, "recent_changes", lambda **kw: MATERIAL)
    monkeypatch.setattr(rn.oliver, "generate",
                        lambda prompt: "<subject>Three new tricks</subject><email>Here's what I can do now.</email>")
    email = rn.release_notes_email(days=5)
    assert email["subject"] == "Three new tricks"
    assert email["body"] == "Here's what I can do now."
    assert email["window"] == "the last 5 days"


def test_release_notes_email_subject_falls_back_when_missing(monkeypatch):
    monkeypatch.setattr(rn, "recent_changes", lambda **kw: MATERIAL)
    monkeypatch.setattr(rn.oliver, "generate", lambda prompt: "<email>Body only, no subject tag.</email>")
    email = rn.release_notes_email(days=5)
    assert email["body"] == "Body only, no subject tag."
    assert email["subject"].startswith("Under my hood:")


def test_release_notes_email_none_when_no_changes(monkeypatch):
    # No commits in the window → recent_changes reports count 0 → no draft, no LLM call.
    monkeypatch.setattr(rn.publish, "git_output", lambda *a, **k: "")

    def _boom(prompt):
        raise AssertionError("generate must not be called when there are no changes")

    monkeypatch.setattr(rn.oliver, "generate", _boom)
    assert rn.release_notes_email(days=5) is None


def test_recent_changes_windows():
    # Day window vs commit window produce distinct human-readable scope labels.
    assert rn.recent_changes(days=5)["window"] == "the last 5 days"
    since = rn.recent_changes(since_commit="HEAD")["window"]
    assert since.startswith("since commit")


def test_resolve_commit_valid_and_invalid():
    assert rn.resolve_commit("HEAD")                       # real repo HEAD resolves to a short hash
    assert rn.resolve_commit("definitely-not-a-commit") is None


def test_extract_subject_variants():
    assert rn._extract_subject("<subject>Hello there</subject>rest") == "Hello there"
    # Tolerates a missing close tag: takes only the first line, not the email that follows.
    assert rn._extract_subject("<subject>Just this line\n<email>not this</email>") == "Just this line"
    assert rn._extract_subject("no tags at all") == ""
