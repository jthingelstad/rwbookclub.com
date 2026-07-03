"""Release-notes email: Oliver tells the club, in his own voice, what new capabilities
he's recently gained.

Mirrors `club/meeting_emails.py` (the 2-day topic email): gather source material, build a
prompt, run it through `oliver.generate` (Opus/high, stateless tool loop), and pull the
subject + body out of `<subject>`/`<email>` tags. The body is returned UNSIGNED — the
signature is appended by `outbound.finalize`/`outbound.send`, exactly like the topic email.

Source material is the repo itself: git history over the look-back window (commit subjects,
bodies, and file stats, plus the merge lines that mark each shipped feature) and the
capability docs (ROADMAP — the de-facto changelog — and which docs changed in the window).
The grounding rule in the prompt is strict: describe only what's in the material, never
invent a capability.

Each release also gets a NAME — a simple alliteration on one title from the club's shelf
("Quixotic Quicksilver"), coined by a small Sonnet call (`coin_release_name`) and woven into the
email's opening. A list send stores it in the `release_notes_sent` event; `db.current_release()`
is how Oliver knows what release he's running (surfaced via `context.club_context`).

Scope is either a day window (`--days`, default 7) or everything since a commit (`--since <hash>`).
In the Discord command, the default scope (no day/since given) is everything since the LAST
release notes: a list send records HEAD as a `release_notes_sent` event in the club timeline
(`db.record_release_notes_sent`), and the next run scopes from `db.last_release_notes_commit()` —
so each shipped change is announced exactly once.

Build-time preview / test:
    python -m agent.club.release_notes --days 7               # print the draft
    python -m agent.club.release_notes --since 8cebec8        # scope to commits since that hash
    python -m agent.club.release_notes --days 7 --send a@b    # also deliver it to that address
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess

from agent import clock, db, oliver, publish
from agent import corpus_read as cr
from agent.club.meeting_emails import _extract_email
from agent.club.meeting_rules import friendly_date as _friendly_date
from agent.mail import outbound

# A dense rework day can produce dozens of commits; cap the detailed list so the prompt
# stays bounded, and tell the model (and the reader) when we truncated rather than dropping
# silently.
_COMMIT_CAP = 60
_ROADMAP_PATH = publish.REPO_ROOT / "agent" / "docs" / "ROADMAP.md"
_SUBJECT_TAG = re.compile(r"<subject>(.*?)</subject>", re.S | re.I)


def _extract_subject(text: str) -> str:
    """Pull the one-line subject from <subject>…</subject>, tolerating a missing close tag.

    Unlike the email body, the subject is a single line — so on an unclosed tag we take only
    the first non-empty line after <subject> (never the email block that follows it).
    """
    m = _SUBJECT_TAG.search(text)
    if m:
        return " ".join(m.group(1).split()).strip()
    opened = re.search(r"<subject>", text, re.I)
    if opened:
        rest = text[opened.end():].strip()
        first = rest.splitlines()[0] if rest else ""
        return re.sub(r"</?subject\s*>", "", first, flags=re.I).strip()
    return ""


def resolve_commit(ref: str) -> str | None:
    """The short hash for a commit-ish, or None if it doesn't resolve (used to validate `since:`)."""
    return publish.git_output(
        ["rev-parse", "--verify", "--short", f"{ref}^{{commit}}"]).strip() or None


def head_commit() -> str | None:
    """The current repo HEAD as a short hash — stored as the baseline when notes are sent, so the
    next release-notes scopes from here. None if git is unavailable."""
    return resolve_commit("HEAD")


def recent_changes(*, days: int | None = None, since_commit: str | None = None) -> dict:
    """Gather the git + docs source material, scoped EITHER to the last `days` days OR to everything
    since `since_commit` (a commit-ish). Pass one; days defaults to 7 if neither is given."""
    if since_commit:
        rev = [f"{since_commit}..HEAD"]
        info = publish.git_output(
            ["log", "-1", "--date=short", "--pretty=format:%h %ad %s", since_commit]).strip()
        window = f"since commit {info}" if info else f"since commit {since_commit}"
    else:
        days = days or 7
        rev = [f"--since={days} days ago"]
        window = f"the last {days} days"

    total = [ln for ln in publish.git_output(["log", *rev, "--oneline"]).splitlines() if ln]
    count = len(total)

    merges = publish.git_output(["log", *rev, "--merges", "--pretty=format:- %h %s"]).strip()
    commits = publish.git_output([
        "log", *rev, "-n", str(_COMMIT_CAP), "--no-merges", "--stat", "--date=short",
        "--pretty=format:%n### %h %ad %s%n%b",
    ]).strip()

    doc_lines = publish.git_output(
        ["log", *rev, "--name-only", "--pretty=format:", "--", "*.md"]
    ).splitlines()
    changed_docs = sorted({ln.strip() for ln in doc_lines if ln.strip().endswith(".md")})

    try:
        roadmap = _ROADMAP_PATH.read_text()
    except OSError:
        roadmap = ""

    return {
        "window": window,
        "days": days,
        "since_commit": since_commit,
        "count": count,
        "truncated": count > _COMMIT_CAP,
        "merges": merges,
        "commits": commits,
        "changed_docs": changed_docs,
        "roadmap": roadmap,
    }


def coin_release_name(material: dict) -> str:
    """Coin this release's name: a simple alliteration on ONE title from the club's shelf
    ("Blistering Blindsight"). A small Sonnet call — the name is a garnish, not worth Opus-tier
    tokens — and failure-tolerant: any error or malformed output returns "" and the release notes
    ship nameless rather than blocked."""
    try:
        shelf = sorted(b["title"] for b in cr.books() if b.get("isRead"))
        used = [r["name"] for r in db.release_history() if r["name"]]
        used_block = ("\n".join(f"- {n}" for n in used)
                      if used else "(none yet — this is the first named release)")
        user = (
            "Coin the name for this release of Oliver (the R/W Book Club's agent software). "
            "Rules:\n"
            "- Pick ONE title from the club's shelf below whose spirit fits this batch of changes.\n"
            "- The name is: one alliterative adjective + that title's distinctive word, title word "
            "LAST — if the shelf held \"Middlemarch\" you might coin \"Merry Middlemarch\". The "
            "adjective MUST start with the same letter/sound as the title word (that's the "
            "alliteration). Two or three words total; never append extra words after the title "
            "word.\n"
            "- Never reuse a previously used name OR its anchor title.\n"
            "- Output ONLY the name on a single line. No quotes, no explanation.\n\n"
            "--- What shipped in this batch (merge commits) ---\n"
            f"{material.get('merges') or '(no merge lines — small batch)'}\n\n"
            "--- Previously used release names ---\n"
            f"{used_block}\n\n"
            "--- The club's shelf (pick your anchor title from these) ---\n"
            + "\n".join(f"- {t}" for t in shelf)
        )
        name = oliver.complete(
            "You name software releases for a book club's agent. You answer with the name only.",
            user, model=oliver.MODEL, max_tokens=4000, effort="low",
            usage_channel="release_notes:name",
        ).strip().strip('"').strip("'")
        if not name or "\n" in name or len(name) > 60:
            return ""
        return name
    except Exception:
        return ""


def release_notes_prompt(material: dict) -> str:
    window = material["window"]
    trunc = (
        f"\n(NOTE: {material['count']} commits landed in this window; only the {_COMMIT_CAP} "
        "most recent are shown in full below. Say so if it matters.)"
        if material["truncated"] else ""
    )
    docs = "\n".join(f"- {d}" for d in material["changed_docs"]) or "(no docs changed)"
    name = material.get("release_name") or ""
    naming = (
        f"RELEASE NAME: this release has been christened \"{name}\" — every release of your "
        "software is named with an alliteration on a title from the club's shelf. Your OPEN "
        "framing sentence must introduce the release by this name; a short clause on why that "
        "book fits this batch is welcome. The subject MAY carry the name too, if it lands "
        "naturally.\n\n"
        if name else ""
    )
    return (
        "Write a short email to the R/W Book Club announcing the new capabilities YOU — Oliver — "
        f"have gained. This batch covers {window}. This is you, in first person, telling the club "
        "what you can now do and what changed under the hood.\n\n"
        "VOICE: first person throughout (\"I can now…\", \"I rebuilt…\", \"I learned…\"). "
        "Technically strong and specific — name the actual mechanism, don't be hand-wavey. Share "
        "it with genuine fun and a real desire to teach: this club is technical and several "
        "members are interested in how agents are built, so the internals are a feature, not "
        "noise. Wherever you can, give a sentence of background on WHY the change is good — what "
        "it improves, what it prevents, what it makes possible.\n\n"
        "GROUNDING (important): describe ONLY changes that appear in the material below. Do not "
        "invent features, numbers, or capabilities. If something is unclear, leave it out. Where "
        "a change is internal plumbing, it's fine — explain it honestly and make it interesting.\n\n"
        "OPEN: before the first section (no header), write ONE short framing sentence so a reader "
        f"knows immediately what this is and the window it covers — that it's your under-the-hood "
        f"update for {window}. Don't dive straight into the story.\n\n"
        f"{naming}"
        "STRUCTURE — exactly three '## ' sections, in this order:\n\n"
        "## The story\n"
        "A short narrative (2-4 sentences, prose) of what's genuinely interesting in this batch — "
        "the throughline, what you were working toward, why it matters to the club.\n\n"
        "## Features\n"
        "A bulleted list of the things members should actually notice and use — the changes that "
        "touch their experience. Lead each bullet with the capability, then a sentence on why it's "
        "good. Keep it to what a member cares about.\n\n"
        "## Release Notes\n"
        "A terse changelog — MANY short, specific entries, one concrete fact per bullet (what "
        "changed, named precisely), NOT paragraphs. Same factual texture as Features but more of "
        "them and lower-level: prefer a dozen one-line facts over three explanations. Each bullet "
        "is a fact, not a story; no prose lead-in, just the list.\n\n"
        "CLOSE: after the last section (no header), end with one short, warm sign-off sentence in "
        "your voice — wrap up and/or point them to #ask-oliver. Do NOT add your name or a signature "
        "block; a signature is appended automatically right after your sign-off.\n\n"
        "FORMAT: this renders as an HTML email, so use markdown — a '## ' header for each section, "
        "bulleted lists, *italics* for things like file or feature names, and **bold** sparingly on "
        "a key phrase. Separate every paragraph and bullet group with a fully blank line, and leave "
        "a blank line after each '## ' header. Do NOT use '---' or horizontal rules.\n\n"
        "OUTPUT: first write a single-line subject — fun, fitting, and a little bit clever for this "
        "audience — between <subject> and </subject>. Then write the ENTIRE email between <email> "
        "and </email> tags, with NOTHING outside the two tag pairs (no preamble, no notes).\n\n"
        f"=== SOURCE MATERIAL ({window}) ==={trunc}\n\n"
        "--- Shipped features (merge commits) ---\n"
        f"{material['merges'] or '(none)'}\n\n"
        "--- Commits in detail (subject, body, files changed) ---\n"
        f"{material['commits'] or '(none)'}\n\n"
        "--- Docs changed in this window ---\n"
        f"{docs}\n\n"
        "--- Current ROADMAP.md (the running feature log, for context on why things matter) ---\n"
        f"{material['roadmap'] or '(unavailable)'}\n"
    )


def release_notes_email(*, days: int | None = None, since_commit: str | None = None) -> dict | None:
    """Build the release-notes email. Returns {subject, body, window, release_name} (body
    unsigned; release_name may be "" if the naming call failed), or None if there were no changes
    in the window (caller should report that plainly)."""
    material = recent_changes(days=days, since_commit=since_commit)
    if material["count"] == 0:
        return None
    material["release_name"] = coin_release_name(material)
    out = oliver.generate(release_notes_prompt(material))
    body = _extract_email(out)
    subject = _extract_subject(out) or f"Under my hood: what changed — {_friendly_date(clock.club_today_iso())}"
    return {"subject": subject, "body": body, "window": material["window"],
            "release_name": material["release_name"]}


log = logging.getLogger("oliver.release_notes")


def _tag_slug(name: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def create_github_release(*, name: str, commit: str, body: str) -> str | None:
    """Tag `commit` and publish a GitHub release for a christened release — the permanent code
    reference for what shipped. Returns the release URL, or None on any failure: the club email
    has already gone out by the time this runs, so GitHub is strictly best-effort (log + activity
    warning, never raise). An existing tag is reused (e.g. one cut by hand); an existing release
    for the tag is returned as-is."""
    tag = _tag_slug(name) if name else f"release-{clock.club_today_iso()}"
    title = name or tag
    try:
        git = publish._bin("git")
        gh = publish._bin("gh")
        if not publish.git_output(["tag", "-l", tag]).strip():
            publish._run([git, "tag", "-a", tag, commit, "-m", f"{title} — Oliver release"], timeout=15)
            publish._run([git, "push", "origin", tag], timeout=60)
        view = subprocess.run([gh, "release", "view", tag, "--json", "url", "-q", ".url"],
                              cwd=publish.REPO_ROOT, capture_output=True, text=True,
                              timeout=30, env=publish._ENV)
        if view.returncode == 0 and view.stdout.strip():
            return view.stdout.strip()  # already published (e.g. cut by hand) — reuse
        made = subprocess.run([gh, "release", "create", tag, "--title", title, "--notes-file", "-"],
                              cwd=publish.REPO_ROOT, capture_output=True, text=True,
                              timeout=60, env=publish._ENV, input=body)
        if made.returncode != 0:
            raise RuntimeError((made.stderr or made.stdout or "")[-500:])
        return made.stdout.strip() or None
    except Exception as e:  # noqa: BLE001 — best-effort by contract
        log.exception("GitHub release for %r failed", tag)
        db.add_activity("warning", "GitHub release failed",
                        f"Tag: {tag}\nCommit: {commit}\n{type(e).__name__}: {e}")
        return None


def _main() -> None:
    parser = argparse.ArgumentParser(description="Preview (and optionally send) Oliver's release-notes email.")
    parser.add_argument("--days", type=int, default=None, help="look back this many days (default 7)")
    parser.add_argument("--since", metavar="COMMIT", help="scope to changes since this commit-ish (overrides --days)")
    parser.add_argument("--send", metavar="EMAIL", help="also deliver the draft to this address")
    args = parser.parse_args()

    email = release_notes_email(days=args.days, since_commit=args.since)
    if email is None:
        scope = f"since {args.since}" if args.since else f"the last {args.days or 7} days"
        print(f"No changes {scope} — nothing to announce.")
        return
    if email.get("release_name"):
        print(f"Release name: {email['release_name']}")
    print(f"Subject: {email['subject']}\n")
    print(outbound.finalize(email["body"]))  # show exactly what would be sent (incl. signature)

    if args.send:
        outbound.send(to=[args.send], subject=email["subject"], body=email["body"])
        print(f"\n[sent to {args.send}]")


if __name__ == "__main__":
    _main()
