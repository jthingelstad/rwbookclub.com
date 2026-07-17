"""Best-effort backfill of conversations.member_slug for turns logged before member-tagging.

Going-forward, oliver.answer() tags each turn with the resolved member; this recovers what it can
from history so existing cross-medium threads are recallable too. Per channel, in id order: resolve
each user turn's speaker display-name to a member (by name/first-name token match against the
roster); assistant turns inherit the member of the nearest preceding user turn in that channel.
Rows that can't be resolved stay NULL. Idempotent — only writes rows where member_slug IS NULL.

    python -m agent.script.archive.backfill_conversation_members [--dry-run]
"""

from __future__ import annotations

import argparse

from agent import corpus_read as cr
from agent import db


def _norm(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def _member_index() -> dict[str, str]:
    """name / first-name / slug (normalized) -> member slug, for token matching a speaker."""
    idx: dict[str, str] = {}
    for m in cr.members():
        slug = m.get("slug")
        name = m.get("name") or ""
        keys = {slug or "", name, name.split()[0] if name else ""}
        for k in keys:
            nk = _norm(k)
            if nk:
                idx.setdefault(nk, slug)
    return idx


def _resolve(speaker: str | None, idx: dict[str, str]) -> str | None:
    n = _norm(speaker)
    if not n:
        return None
    if n in idx:  # exact (Discord display name == member name/slug)
        return idx[n]
    tokens = set(n.split())  # "jamie thingelstad" -> {jamie, thingelstad} -> member "jamie"
    for key, slug in idx.items():
        if key in tokens:
            return slug
    return None


def backfill(*, dry_run: bool = False) -> dict:
    idx = _member_index()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, channel_id, role, speaker, member_slug FROM conversations "
            "ORDER BY channel_id, id"
        ).fetchall()
        updates: list[tuple[str, int]] = []
        cache: dict[str, str | None] = {}
        last_by_channel: dict[str, str | None] = {}
        candidates = 0
        for r in rows:
            if r["member_slug"] is None:
                candidates += 1
            if r["role"] == "user":
                if r["member_slug"]:
                    slug = r["member_slug"]
                else:
                    name = r["speaker"]
                    if name not in cache:
                        cache[name] = _resolve(name, idx)
                    slug = cache[name]
                    if slug:
                        updates.append((slug, r["id"]))
                last_by_channel[r["channel_id"]] = slug
            elif r["member_slug"] is None:  # assistant inherits the channel's current member
                slug = last_by_channel.get(r["channel_id"])
                if slug:
                    updates.append((slug, r["id"]))
        if not dry_run and updates:
            conn.executemany("UPDATE conversations SET member_slug = ? WHERE id = ?", updates)
    return {"null_rows": candidates, "tagged": len(updates), "dry_run": dry_run}


if __name__ == "__main__":
    from agent import database

    database.initialize()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report counts without writing")
    print(backfill(dry_run=ap.parse_args().dry_run))
