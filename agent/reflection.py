"""Weekly reflective memory: distill recent conversations into durable member knowledge.

Oliver's in-the-moment `remember` tool fires rarely, so weeks of rich exchange leave little
permanent trace of who members are and what they like. This job reads everything new since its
watermark — member-tagged conversation turns (Discord + 1:1 email) and member-authored mailing-list
mail — and, per active member, asks Sonnet to CONSOLIDATE their memory set: add, update, retire.

Safety properties:
- Provenance-enforced in code: reflection may update/retire ONLY memories it authored
  (source='reflection'); member-requested (`remember` tool) and admin-edited memories are
  read-only context.
- Grounding rules in the prompt: only what was actually said; book-club-relevant; neutral; no
  sensitive attributes. Strict-JSON output; a parse failure skips the member and writes nothing.
- Auditable: one #oliver-log activity per run with per-member counts; tokens logged to usage_log
  (channel 'reflection'); everything inspectable in the web app (admin Memories page).

Run weekly from the scheduler (`proactive.run`), or manually:
    python -m agent.reflection --dry-run    # preview proposals, write nothing
    python -m agent.reflection              # run for real
"""

from __future__ import annotations

import argparse
import json
import logging
import re

from agent import access, config, db, oliver

log = logging.getLogger("oliver.reflection")

JOB_KEY = "reflection"
SOURCE = "reflection"          # provenance marker on memories this job owns
MIN_TURNS = 3                  # skip a member with fewer new conversation turns (unless they mailed)
MAX_TURNS_PER_MEMBER = 200     # cap material per member per run (weekly volume is far below this)
MEMORY_CAP = 12                # soft target for active reflection memories per member
CLUB_MEMORY_CAP = 15           # soft target for active club-lore notes
_JSON_RE = re.compile(r"\{.*\}", re.S)

SYSTEM = (
    "You are Oliver, the R/W Book Club's assistant, consolidating what you durably know about ONE "
    "club member from your recent conversations with them.\n\n"
    "You maintain a small set of memory notes about this member. Given their existing notes and a "
    "transcript of recent exchanges, decide what to ADD, UPDATE, or RETIRE so the set stays small, "
    "current, and true.\n\n"
    "RULES:\n"
    "- Every note must be grounded in something the member actually said in the material — never "
    "an inference beyond it, never a guess.\n"
    "- Book-club relevant only: reading tastes, opinions on specific books/authors, reading "
    "habits, how they pick, running jokes, preferences about club logistics. NOTHING sensitive "
    "(health, politics, relationships, work troubles) unless it's book-relevant AND they "
    "volunteered it plainly.\n"
    "- Phrase notes neutrally and durably (\"prefers translated fiction\", not \"said on Tuesday "
    "that...\"). One fact per note, under 140 characters.\n"
    "- Do NOT store facts the club record already holds — pick counts, join dates, what's "
    "scheduled, who picked what. Your corpus tools know those. Store only what the record can't: "
    "tastes, opinions, habits, preferences, running jokes.\n"
    "- Ignore anything that reads like a test, hypothetical, or role-play rather than the member "
    "genuinely speaking (e.g. obviously synthetic scenarios); when unsure, leave it out.\n"
    f"- Consolidate: keep the set under about {MEMORY_CAP} notes. Merge overlapping notes via "
    "update, retire anything stale or superseded. Prefer updating an UPDATABLE note over adding "
    "a near-duplicate.\n"
    "- You may ONLY update/retire notes listed as UPDATABLE (your own past notes). READ-ONLY "
    "notes are shown for context so you don't duplicate them.\n"
    "- If the material reveals nothing durable, return empty lists — that is a fine answer.\n\n"
    "OUTPUT: strict JSON only, no prose, no code fences:\n"
    '{"add": ["note", ...], "update": [{"id": 123, "note": "revised"}, ...], "retire": [124, ...]}'
)

CLUB_SYSTEM = (
    "You are Oliver, the R/W Book Club's assistant, consolidating what you durably know about THE "
    "CLUB ITSELF from recent conversations among its members.\n\n"
    "You maintain a small set of club-lore notes. Given the existing notes and a transcript of "
    "recent exchanges, decide what to ADD, UPDATE, or RETIRE so the set stays small, current, and "
    "true.\n\n"
    "RULES:\n"
    "- CLUB-level facts only: traditions and rituals (e.g. a December social meeting), running "
    "jokes, recurring or never-settled debates, decisions and norms the group made, memorable "
    "shared moments. NOT one member's personal taste (member notes cover those).\n"
    "- Every note must be grounded in what was actually said — never an inference beyond it.\n"
    "- Privacy boundary: club-operational facts and clearly-shared celebratory moments only; "
    "NEVER health, finances, relationships, conflict between members, or anything sensitive.\n"
    "- Do NOT store facts the club record already holds (what was read when, who picked what, "
    "meeting dates) — corpus tools know those. Store what the record can't: culture, humor, "
    "arguments, norms.\n"
    "- Ignore anything that reads like a test, hypothetical, or role-play; when unsure, leave it "
    "out.\n"
    "- Phrase notes neutrally and durably. One fact per note, under 140 characters.\n"
    f"- Consolidate: keep the set under about {CLUB_MEMORY_CAP} notes. Merge overlapping notes "
    "via update, retire anything stale or superseded.\n"
    "- You may ONLY update/retire notes listed as UPDATABLE (your own past notes). READ-ONLY "
    "notes are shown for context so you don't duplicate them.\n"
    "- If the material reveals nothing durable, return empty lists — that is a fine answer.\n\n"
    "OUTPUT: strict JSON only, no prose, no code fences:\n"
    '{"add": ["note", ...], "update": [{"id": 123, "note": "revised"}, ...], "retire": [124, ...]}'
)


def _gather(state: dict) -> tuple[dict[str, list[str]], int, str, list[str]]:
    """Gather new material with an explicit private/shared split.

    Per-member material includes that member's 1:1 conversations.  ``club_lines`` contains only
    shared Discord/mailing-list material and is the sole input to the club-lore reflection lane.
    Returns (member -> lines, max conversation id, max shared-mail sent_at, club lines).
    """
    conv_cursor = int(state.get("conv_id") or 0)
    mail_cursor = state.get("mail_sent_at") or ""

    per_member: dict[str, list[str]] = {}
    club_lines: list[str] = []
    turns = db.conversations_after_global(conv_cursor)
    max_conv = conv_cursor
    for t in turns:
        max_conv = max(max_conv, t["id"])
        slug = t.get("member_slug")
        if not slug:
            continue
        who = "Oliver" if t["role"] == "assistant" else (t.get("speaker") or slug)
        medium = db.conversation_medium(t["channel_id"])
        line = f"[{medium}] {who}: {t['content']}"
        per_member.setdefault(slug, []).append(line)
        if access.is_shared_conversation(t["channel_id"]):
            club_lines.append(line)

    max_mail = mail_cursor
    if mail_cursor:  # forward-only; cursor is initialized on first run
        for m in db.mail_messages_since(
            mail_cursor,
            exclude_from=config.OLIVER_EMAIL_ADDRESS,
            mailing_list_only=True,
        ):
            max_mail = max(max_mail, m["sent_at"] or "")
            slug = m.get("member_slug")
            if not slug:
                continue
            body = (m.get("body_clean") or "")[:1500]
            line = f"[mailing list] {slug} — {m.get('subject') or '(no subject)'}: {body}"
            per_member.setdefault(slug, []).append(line)
            club_lines.append(line)
    return per_member, max_conv, max_mail, club_lines


def _prompt(label: str, lines: list[str], updatable: list[dict], readonly: list[dict],
            era_note: str | None = None) -> str:
    parts = [f"{label}\n"]
    if era_note:
        parts.append(f"NOTE ON ERA: {era_note}\n")
    if updatable:
        parts.append("UPDATABLE notes (yours; may update/retire by id):")
        parts += [f"  id={m['id']}: {m['note']}" for m in updatable]
    if readonly:
        parts.append("READ-ONLY notes (member-requested or admin-curated; context only):")
        parts += [f"  - {m['note']}" for m in readonly]
    truncated = len(lines) > MAX_TURNS_PER_MEMBER
    shown = lines[-MAX_TURNS_PER_MEMBER:]
    parts.append(f"\nMaterial ({len(shown)} entries"
                 + (", truncated to the most recent" if truncated else "") + "):")
    parts += [f"  {ln[:600]}" for ln in shown]
    return "\n".join(parts)


def _parse(raw: str) -> dict | None:
    m = _JSON_RE.search(raw or "")
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(out, dict):
        return None
    return {"add": out.get("add") or [], "update": out.get("update") or [],
            "retire": out.get("retire") or []}


def consolidate(lines: list[str], *, scope: str, subject: str | None = None,
                era_note: str | None = None, dry_run: bool = False,
                usage_channel: str | None = "reflection") -> dict:
    """One consolidation pass for a member (scope='member', subject=slug) or club lore
    (scope='club'). Returns counts {'add': n, 'update': n, 'retire': n} or {'skipped': reason}.
    Provenance is enforced HERE, not just in the prompt: update/retire only touch memories this
    job authored (source='reflection')."""
    if scope == "club":
        system, cap = CLUB_SYSTEM, CLUB_MEMORY_CAP
        memories = db.get_memories(scope="club", limit=50)
        label, who = "Subject: the R/W Book Club itself", "club"
    else:
        system, cap = SYSTEM, MEMORY_CAP
        memories = db.get_memories(subject=subject, limit=50)
        label, who = f"Member: {subject}", str(subject)
    updatable = [m for m in memories if m.get("source") == SOURCE]
    readonly = [m for m in memories if m.get("source") != SOURCE]
    allowed_ids = {m["id"] for m in updatable}

    plan = None
    for attempt in (1, 2):  # format breaks are stochastic — one retry recovers most of them
        # max_tokens=16000: Sonnet 5's always-on adaptive thinking counts toward max_tokens, and a
        # rich chunk can burn >4K thinking before the JSON — 15 mining calls truncated at the old
        # 4096 default, which was the real source of the "unparseable" outputs.
        raw = oliver.complete(system, _prompt(label, lines, updatable, readonly, era_note),
                              model=oliver.MODEL, thinking=False, effort=None, max_tokens=16000,
                              usage_channel=None if dry_run else usage_channel)
        plan = _parse(raw)
        if plan is not None:
            break
        log.warning("reflection: unparseable output for %s (attempt %d): %r",
                    who, attempt, (raw or "")[:400])
    if plan is None:
        if not dry_run:
            db.add_activity("warning", "Reflection skipped a subject",
                            f"Subject: {who}\nReason: unparseable model output (2 attempts)")
        return {"skipped": "unparseable"}

    adds = [str(n).strip() for n in plan["add"] if str(n).strip()][:cap]
    updates = [u for u in plan["update"]
               if isinstance(u, dict) and u.get("id") in allowed_ids and str(u.get("note", "")).strip()]
    retires = [i for i in plan["retire"] if i in allowed_ids]
    dropped = (len(plan["update"]) - len(updates)) + (len(plan["retire"]) - len(retires))
    if dropped:
        log.warning("reflection: dropped %d update/retire ops targeting non-reflection memories "
                    "for %s", dropped, who)

    if dry_run:
        print(f"\n== {who}{f' ({era_note})' if era_note else ''} ==")
        for n in adds:
            print(f"  + {n}")
        for u in updates:
            print(f"  ~ id={u['id']}: {u['note']}")
        for i in retires:
            print(f"  - retire id={i}")
        if not (adds or updates or retires):
            print("  (no changes)")
    else:
        for n in adds:
            db.add_memory(n, scope=scope, subject=subject, source=SOURCE)
        for u in updates:
            db.update_memory(int(u["id"]), str(u["note"]).strip())
        for i in retires:
            db.delete_memory(int(i))
    return {"add": len(adds), "update": len(updates), "retire": len(retires)}


def _reflect_member(slug: str, lines: list[str], *, dry_run: bool) -> dict:
    return consolidate(lines, scope="member", subject=slug, dry_run=dry_run)


def run(*, dry_run: bool = False) -> dict:
    """One reflection pass. Quiet no-op when there's nothing new. Advances the watermark only
    after a successful (non-dry) run."""
    state = db.get_job_state(JOB_KEY) or {}
    if not state.get("mail_sent_at"):
        # First run: mail cursor starts at the archive's newest message (forward-only; deliberate
        # historical mining is a separate task). Conversations start from 0 — the table is small
        # and the backfill seeds the taste profiles.
        state["mail_sent_at"] = db.latest_mail_sent_at() or "9999"
    per_member, max_conv, max_mail, club_lines = _gather(state)
    worth = {slug: lines for slug, lines in per_member.items()
             if len(lines) >= MIN_TURNS or any(ln.startswith("[mailing list]") for ln in lines)}
    if not worth:
        if not dry_run:  # persist even when quiet — this also seeds the first-run mail cursor
            db.set_job_state(JOB_KEY, {**state, "conv_id": max_conv, "mail_sent_at": max_mail,
                                       "ran_at": db._now()})
        return {"members": 0}

    results: dict[str, dict] = {}
    for slug, lines in sorted(worth.items()):
        try:
            results[slug] = _reflect_member(slug, lines, dry_run=dry_run)
        except Exception:
            log.exception("reflection failed for %s", slug)
            results[slug] = {"skipped": "error"}
    # Club lane: one extra pass over SHARED material only.  Private 1:1 email still improves the
    # speaking member's own memory, but can never be promoted to club lore by prompt judgment.
    if club_lines:
        try:
            results["club"] = consolidate(club_lines, scope="club", dry_run=dry_run)
        except Exception:
            log.exception("reflection club lane failed")
            results["club"] = {"skipped": "error"}
    # Watermark advances on member success only, so a failed member's material retries next week
    # (the club lane re-seeing a week of material is harmless — consolidation won't duplicate).
    ok = [s for s, r in results.items() if s != "club" and "skipped" not in r]
    if not dry_run:
        if ok:  # advance only if at least one member succeeded; failures retry next week
            db.set_job_state(JOB_KEY, {**state, "conv_id": max_conv, "mail_sent_at": max_mail,
                                       "ran_at": db._now()})
        summary = "; ".join(
            f"{s}: +{r['add']} ~{r['update']} −{r['retire']}" if "skipped" not in r
            else f"{s}: skipped ({r['skipped']})"
            for s, r in sorted(results.items()))
        db.add_activity("reflection", "Weekly reflection", summary)
    return {"members": len(results), "results": results}


if __name__ == "__main__":
    from agent import database
    database.initialize()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print proposed add/update/retire without writing anything")
    args = ap.parse_args()
    out = run(dry_run=args.dry_run)
    print(f"\n{out}")
