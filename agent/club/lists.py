"""Write member/club book lists into the authoritative club record (SQLite).

The single list writer, called by the `/oliver list …` commands. Members own their lists; club
lists (scope='club') are admin-curated. Each op resolves + authorizes the list, mutates the DB,
regenerates the affected `lists/<slug>.json` corpus file, and validates. The site is rebuilt +
deployed separately by the publish step (the corpus is not committed to git).
"""

from __future__ import annotations

from corpus.paths import DATA_DIR
from corpus.validate import validate_data_dir
from agent import clubdb, corpus_gen, db
from agent import corpus_read as cr


class ListError(Exception):
    """A user-facing problem (bad input, unknown list/book, not authorized) — surfaced in Discord."""


def _validate_or_raise() -> None:
    errors = validate_data_dir(DATA_DIR)
    if errors:
        preview = "; ".join(errors[:3])
        more = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
        raise ListError(f"Corpus validation failed: {preview}{more}")


def _resolve_list(conn, ref: str, *, actor_slug: str | None, is_admin: bool) -> dict:
    """Find a list by slug (what autocomplete sends) or, failing that, by name within the lists the
    actor may act on. Raises ListError if nothing matches."""
    row = clubdb.list_by_slug(conn, ref)
    if row is None:
        found = cr.find_list(ref, owner_slug=None if is_admin else actor_slug)
        if found:
            row = clubdb.list_by_slug(conn, found["slug"])
    if row is None:
        raise ListError(f"I couldn't find a list called {ref!r}.")
    return row


def _authorize(row: dict, *, actor_slug: str | None, is_admin: bool) -> None:
    if row["scope"] == "club":
        if not is_admin:
            raise ListError("Only an admin can change club lists.")
    elif not is_admin and row.get("owner_slug") != actor_slug:
        raise ListError("That's not your list — you can only change your own.")


def create_list(name: str, description: str | None = None, *,
                owner_slug: str | None = None, scope: str = "member") -> dict:
    """Create a member list (owner = the caller) or, for admins, a club list (owner_slug=None,
    scope='club'). Returns {slug, name, scope}."""
    name = (name or "").strip()
    if not name:
        raise ListError("A list needs a name.")
    clubdb.ensure_schema()
    with db.connect() as conn:
        owner_id = None
        if scope == "member":
            owner_id = clubdb.member_id_for_slug(conn, owner_slug)
            if owner_id is None:
                raise ListError("You need to be a linked club member to make a list.")
        res = clubdb.create_list(conn, name=name, scope=scope, owner_id=owner_id,
                                 description=(description or "").strip() or None)
        corpus_gen.write_list_file(conn, res["id"], DATA_DIR)
    _validate_or_raise()
    return {"slug": res["slug"], "name": name, "scope": scope}


def add_book(list_ref: str, book_query: str, note: str | None = None, *,
             actor_slug: str | None, is_admin: bool) -> dict:
    clubdb.ensure_schema()
    book = cr.find_book(book_query)
    if not book:
        raise ListError(f"No book matching {book_query!r} in our corpus.")
    with db.connect() as conn:
        row = _resolve_list(conn, list_ref, actor_slug=actor_slug, is_admin=is_admin)
        _authorize(row, actor_slug=actor_slug, is_admin=is_admin)
        book_id = clubdb.book_id_for_slug(conn, book["slug"])
        added = clubdb.set_list_book(conn, row["id"], book_id, (note or "").strip() or None)
        corpus_gen.write_list_file(conn, row["id"], DATA_DIR)
    _validate_or_raise()
    return {"slug": row["slug"], "name": row["name"], "book": book["title"], "added": added}


def move_book(list_ref: str, book_query: str, *, up: bool, actor_slug: str | None,
              is_admin: bool) -> dict:
    """Move a book one step up/down within a list (preserving its note)."""
    clubdb.ensure_schema()
    book = cr.find_book(book_query)
    if not book:
        raise ListError(f"No book matching {book_query!r} in our corpus.")
    with db.connect() as conn:
        row = _resolve_list(conn, list_ref, actor_slug=actor_slug, is_admin=is_admin)
        _authorize(row, actor_slug=actor_slug, is_admin=is_admin)
        book_id = clubdb.book_id_for_slug(conn, book["slug"])
        moved = clubdb.move_list_book(conn, row["id"], book_id, up=up)
        corpus_gen.write_list_file(conn, row["id"], DATA_DIR)
    _validate_or_raise()
    return {"slug": row["slug"], "name": row["name"], "book": book["title"], "moved": moved}


def reorder(list_ref: str, book_slugs: list[str], *, actor_slug: str | None,
            is_admin: bool) -> dict:
    """Set a list's order to the given sequence of book slugs (drag-and-drop)."""
    clubdb.ensure_schema()
    with db.connect() as conn:
        row = _resolve_list(conn, list_ref, actor_slug=actor_slug, is_admin=is_admin)
        _authorize(row, actor_slug=actor_slug, is_admin=is_admin)
        book_ids = [bid for bid in (clubdb.book_id_for_slug(conn, s) for s in book_slugs if s) if bid]
        clubdb.reorder_list_books(conn, row["id"], book_ids)
        corpus_gen.write_list_file(conn, row["id"], DATA_DIR)
    _validate_or_raise()
    return {"slug": row["slug"], "name": row["name"]}


def set_note(list_ref: str, book_query: str, note: str | None, *,
             actor_slug: str | None, is_admin: bool) -> dict:
    """Set (or clear) the note on a book already in the list. Errors if the book isn't present."""
    clubdb.ensure_schema()
    book = cr.find_book(book_query)
    if not book:
        raise ListError(f"No book matching {book_query!r} in our corpus.")
    with db.connect() as conn:
        row = _resolve_list(conn, list_ref, actor_slug=actor_slug, is_admin=is_admin)
        _authorize(row, actor_slug=actor_slug, is_admin=is_admin)
        book_id = clubdb.book_id_for_slug(conn, book["slug"])
        if not conn.execute("SELECT 1 FROM club_list_books WHERE list_id = ? AND book_id = ?",
                            (row["id"], book_id)).fetchone():
            raise ListError(f"{book['title']!r} isn't in this list.")
        clubdb.set_list_book(conn, row["id"], book_id, (note or "").strip() or None)
        corpus_gen.write_list_file(conn, row["id"], DATA_DIR)
    _validate_or_raise()
    return {"slug": row["slug"], "name": row["name"], "book": book["title"]}


def remove_book(list_ref: str, book_query: str, *, actor_slug: str | None, is_admin: bool) -> dict:
    clubdb.ensure_schema()
    book = cr.find_book(book_query)
    if not book:
        raise ListError(f"No book matching {book_query!r} in our corpus.")
    with db.connect() as conn:
        row = _resolve_list(conn, list_ref, actor_slug=actor_slug, is_admin=is_admin)
        _authorize(row, actor_slug=actor_slug, is_admin=is_admin)
        book_id = clubdb.book_id_for_slug(conn, book["slug"])
        removed = clubdb.remove_list_book(conn, row["id"], book_id)
        corpus_gen.write_list_file(conn, row["id"], DATA_DIR)
    _validate_or_raise()
    return {"slug": row["slug"], "name": row["name"], "book": book["title"], "removed": removed}


def edit_list(list_ref: str, *, name: str | None = None, description: str | None = None,
              actor_slug: str | None, is_admin: bool) -> dict:
    if name is not None and not name.strip():
        raise ListError("A list name can't be empty.")
    clubdb.ensure_schema()
    with db.connect() as conn:
        row = _resolve_list(conn, list_ref, actor_slug=actor_slug, is_admin=is_admin)
        _authorize(row, actor_slug=actor_slug, is_admin=is_admin)
        clubdb.update_list(conn, row["id"],
                           name=name.strip() if name else None,
                           description=description.strip() if description is not None else None)
        corpus_gen.write_list_file(conn, row["id"], DATA_DIR)
    _validate_or_raise()
    return {"slug": row["slug"], "name": (name.strip() if name else row["name"])}


def delete_list(list_ref: str, *, actor_slug: str | None, is_admin: bool) -> dict:
    clubdb.ensure_schema()
    with db.connect() as conn:
        row = _resolve_list(conn, list_ref, actor_slug=actor_slug, is_admin=is_admin)
        _authorize(row, actor_slug=actor_slug, is_admin=is_admin)
        slug, name = row["slug"], row["name"]
        clubdb.delete_list(conn, row["id"])
    corpus_gen.remove_list_file(slug, DATA_DIR)
    _validate_or_raise()
    return {"slug": slug, "name": name}
