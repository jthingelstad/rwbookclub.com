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
    "changed_docs": ["agent/README.md"],
    "capabilities": "# Oliver\nShiny things are live.",
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
    assert "Shiny things are live" in p
    # New contract: an opening framing sentence, a terse changelog, and a closing sign-off.
    assert "OPEN:" in p and "framing sentence" in p
    assert "CLOSE:" in p and "sign-off" in p
    assert "terse changelog" in p


def test_prompt_uses_since_window():
    p = rn.release_notes_prompt(
        {**MATERIAL, "window": "since commit deadbee 2026-06-27 Did a thing"}
    )
    assert "since commit deadbee" in p


def test_prompt_notes_truncation():
    big = {**MATERIAL, "count": 200, "truncated": True}
    assert "200 commits" in rn.release_notes_prompt(big)


def test_release_notes_email_extracts_oliver_subject_and_body(monkeypatch):
    monkeypatch.setattr(rn, "recent_changes", lambda **kw: dict(MATERIAL))
    monkeypatch.setattr(rn, "coin_release_name", lambda material: "")
    monkeypatch.setattr(
        rn.oliver,
        "generate",
        lambda prompt: (
            "<subject>Three new tricks</subject><email>Here's what I can do now.</email>"
        ),
    )
    email = rn.release_notes_email(days=5)
    assert email["subject"] == "Three new tricks"
    assert email["body"] == "Here's what I can do now."
    assert email["window"] == "the last 5 days"
    assert email["release_name"] == ""  # a failed naming call never blocks the notes


def test_release_notes_email_subject_falls_back_when_missing(monkeypatch):
    monkeypatch.setattr(rn, "recent_changes", lambda **kw: dict(MATERIAL))
    monkeypatch.setattr(rn, "coin_release_name", lambda material: "")
    monkeypatch.setattr(
        rn.oliver, "generate", lambda prompt: "<email>Body only, no subject tag.</email>"
    )
    email = rn.release_notes_email(days=5)
    assert email["body"] == "Body only, no subject tag."
    assert email["subject"].startswith("Under my hood:")


def test_release_notes_email_carries_name_into_prompt_and_result(monkeypatch):
    prompts = []
    monkeypatch.setattr(rn, "recent_changes", lambda **kw: dict(MATERIAL))
    monkeypatch.setattr(rn, "coin_release_name", lambda material: "Blistering Blindsight")
    monkeypatch.setattr(
        rn.oliver,
        "generate",
        lambda prompt: prompts.append(prompt) or "<subject>S</subject><email>B</email>",
    )
    email = rn.release_notes_email(days=5)
    assert email["release_name"] == "Blistering Blindsight"
    assert 'christened "Blistering Blindsight"' in prompts[0]


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
    assert rn.resolve_commit("HEAD")  # real repo HEAD resolves to a short hash
    assert rn.resolve_commit("definitely-not-a-commit") is None


def test_extract_subject_variants():
    assert rn._extract_subject("<subject>Hello there</subject>rest") == "Hello there"
    # Tolerates a missing close tag: takes only the first line, not the email that follows.
    assert (
        rn._extract_subject("<subject>Just this line\n<email>not this</email>") == "Just this line"
    )
    assert rn._extract_subject("no tags at all") == ""


# --- Named releases: coin_release_name / prompt paragraph / db round-trip / Oliver's context ---


def _patch_naming_world(monkeypatch, *, complete):
    monkeypatch.setattr(
        rn.cr,
        "books",
        lambda: [
            {"title": "Blindsight", "isRead": True},
            {"title": "Quicksilver", "isRead": True},
            {"title": "Not Yet Read", "isRead": False},
        ],
    )
    monkeypatch.setattr(
        rn.db,
        "release_history",
        lambda: [
            {
                "name": "Quixotic Quicksilver",
                "commit": "aaa111",
                "subject": "s",
                "occurred_at": "t",
            },
            {"name": None, "commit": "bbb222", "subject": "s", "occurred_at": "t"},
        ],
    )
    monkeypatch.setattr(rn.oliver, "complete", complete)


def test_coin_release_name_prompt_and_cleanup(monkeypatch):
    calls = []

    def fake_complete(system, user, **kwargs):
        calls.append((system, user, kwargs))
        return '"Blistering Blindsight"'

    _patch_naming_world(monkeypatch, complete=fake_complete)
    assert rn.coin_release_name(MATERIAL) == "Blistering Blindsight"
    _, user, kwargs = calls[0]
    assert "- Blindsight" in user and "- Quicksilver" in user
    assert "Not Yet Read" not in user  # unread shelf entries aren't anchors
    assert "Quixotic Quicksilver" in user  # used names embedded so they're never reused
    assert "a shiny thing" in user  # the batch gist (merge lines) is in the prompt
    assert kwargs["model"] == rn.oliver.MODEL  # Sonnet, not the Opus-tier default
    assert kwargs["usage_channel"] == "release_notes:name"


def test_coin_release_name_tolerates_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("api down")

    _patch_naming_world(monkeypatch, complete=boom)
    assert rn.coin_release_name(MATERIAL) == ""

    _patch_naming_world(monkeypatch, complete=lambda *a, **k: "Two\nLines")
    assert rn.coin_release_name(MATERIAL) == ""

    _patch_naming_world(monkeypatch, complete=lambda *a, **k: "x" * 80)
    assert rn.coin_release_name(MATERIAL) == ""


def test_prompt_release_name_paragraph_toggle():
    named = rn.release_notes_prompt({**MATERIAL, "release_name": "Blistering Blindsight"})
    assert "RELEASE NAME" in named and 'christened "Blistering Blindsight"' in named
    nameless = rn.release_notes_prompt(dict(MATERIAL))
    assert "RELEASE NAME" not in nameless


def test_release_history_and_current_release_round_trip(fresh_db):
    assert fresh_db.current_release() is None
    fresh_db.record_release_notes_sent(
        "aaa111", scope="s1", subject="old", occurred_at="2026-06-01T00:00:00+00:00"
    )  # pre-naming
    fresh_db.record_release_notes_sent(
        "bbb222",
        scope="s2",
        subject="new",
        release_name="Blistering Blindsight",
        occurred_at="2026-07-04T01:00:00+00:00",
    )
    history = fresh_db.release_history()
    assert [r["commit"] for r in history] == ["bbb222", "aaa111"]  # newest first
    assert history[1]["name"] is None
    current = fresh_db.current_release()
    assert current["name"] == "Blistering Blindsight" and current["commit"] == "bbb222"


def test_current_release_skips_newer_unnamed_send(fresh_db):
    # A nameless send after a named one must not blank the current release.
    fresh_db.record_release_notes_sent(
        "aaa111",
        scope="s",
        subject="named",
        release_name="Quixotic Quicksilver",
        occurred_at="2026-07-01T00:00:00+00:00",
    )
    fresh_db.record_release_notes_sent(
        "bbb222", scope="s", subject="nameless", occurred_at="2026-07-02T00:00:00+00:00"
    )
    assert fresh_db.current_release()["name"] == "Quixotic Quicksilver"


def test_club_context_carries_current_release(fresh_db):
    from agent import context

    assert "release named" not in context.club_context()  # no named release → no line
    # 01:00 UTC on Jul 4 is the evening of Jul 3 in club time — the line must say the club day.
    fresh_db.record_release_notes_sent(
        "bbb222",
        scope="s",
        subject="new",
        release_name="Blistering Blindsight",
        occurred_at="2026-07-04T01:00:00+00:00",
    )
    ctx = context.club_context()
    assert 'running the release named "Blistering Blindsight"' in ctx
    assert "Friday, July 3" in ctx


# --- GitHub release (tag + gh) on a christened list send ---


def test_tag_slug():
    assert rn._tag_slug("Quiet Quicksilver") == "quiet-quicksilver"
    assert rn._tag_slug("Blistering — Blindsight!") == "blistering-blindsight"


def _gh_world(monkeypatch, *, tag_exists, view_rc, create_rc=0):
    calls = []
    monkeypatch.setattr(rn.publish, "_bin", lambda name: name)
    monkeypatch.setattr(
        rn.publish, "git_output", lambda args, **kw: "tagname" if tag_exists else ""
    )

    def fake_prun(cmd, timeout, cwd=None):
        calls.append(("run", cmd))

    monkeypatch.setattr(rn.publish, "_run", fake_prun)

    def fake_sub(cmd, **kw):
        calls.append(("sub", cmd))

        class R:
            pass

        r = R()
        if "view" in cmd:
            r.returncode = view_rc
            r.stdout = "https://github.com/x/releases/tag/t\n" if view_rc == 0 else ""
            r.stderr = ""
        else:
            r.returncode = create_rc
            r.stdout = "https://github.com/x/releases/tag/new\n" if create_rc == 0 else ""
            r.stderr = "boom" if create_rc else ""
        return r

    monkeypatch.setattr(rn.subprocess, "run", fake_sub)
    return calls


def test_github_release_creates_tag_and_release(monkeypatch, fresh_db):
    calls = _gh_world(monkeypatch, tag_exists=False, view_rc=1)
    url = rn.create_github_release(name="Quiet Quicksilver", commit="abc123", body="notes body")
    assert url == "https://github.com/x/releases/tag/new"
    run_cmds = [c for kind, c in calls if kind == "run"]
    assert any("tag" in c for c in run_cmds) and any("push" in c for c in run_cmds)


def test_github_release_reuses_existing(monkeypatch, fresh_db):
    calls = _gh_world(monkeypatch, tag_exists=True, view_rc=0)
    url = rn.create_github_release(name="Quiet Quicksilver", commit="abc123", body="x")
    assert url == "https://github.com/x/releases/tag/t"
    assert not [c for kind, c in calls if kind == "run"]  # no tag/push when the tag exists


def test_github_release_failure_is_swallowed(monkeypatch, fresh_db):
    _gh_world(monkeypatch, tag_exists=False, view_rc=1, create_rc=1)
    assert rn.create_github_release(name="Quiet Quicksilver", commit="abc123", body="x") is None
    acts = fresh_db.pending_activity(limit=5)
    assert any("GitHub release failed" in a["title"] for a in acts)
