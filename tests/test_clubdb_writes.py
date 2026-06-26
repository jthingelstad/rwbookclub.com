"""The /oliver write path now persists to the authoritative club_* tables and
regenerates the corpus from them (Oliver manages the DB)."""
import json

from agent import clubdb, corpus_write, db


def _patch_git(monkeypatch):
    monkeypatch.setattr(corpus_write.gitwrite, "sync", lambda: None)
    monkeypatch.setattr(corpus_write.gitwrite, "commit_paths", lambda *_a, **_k: "sha")


def test_write_book_persists_to_db_and_regenerates_corpus(monkeypatch, tmp_path):
    data = tmp_path / "data"
    (data / "books").mkdir(parents=True)
    monkeypatch.setattr(corpus_write, "DATA_DIR", data)
    _patch_git(monkeypatch)

    out = corpus_write.write_book(
        {"title": "Test Driven Clubbing", "authors": ["Ada Lovelace"],
         "topic": "Technology", "fiction": False, "publicationYear": 2026}
    )

    # Authoritative row exists with an integer PK.
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM club_books WHERE slug = ?", (out["slug"],)).fetchone()
        link = conn.execute(
            "SELECT a.name FROM club_book_authors ba JOIN club_authors a ON a.id = ba.author_id "
            "JOIN club_books b ON b.id = ba.book_id WHERE b.slug = ?", (out["slug"],)
        ).fetchone()
    assert row is not None and row["title"] == "Test Driven Clubbing"
    assert isinstance(row["id"], int)
    assert link["name"] == "Ada Lovelace"

    # Corpus file was regenerated from the DB in the normalized shape.
    doc = json.loads((data / "books" / f"{out['slug']}.json").read_text())
    assert doc["title"] == "Test Driven Clubbing"
    assert doc["authors"] == ["Ada Lovelace"]
    assert doc["bookId"] == row["id"]
    author_doc = json.loads((data / "authors" / "ada-lovelace.json").read_text())
    assert author_doc == {"name": "Ada Lovelace"}   # bio omitted until set


def test_schedule_meeting_persists_picker_and_placeholder(monkeypatch, tmp_path):
    data = tmp_path / "data"
    for d in ("books", "meetings", "authors", "members"):
        (data / d).mkdir(parents=True)
    monkeypatch.setattr(corpus_write, "DATA_DIR", data)
    _patch_git(monkeypatch)

    # Seed a member (id+row) and its corpus file (validate needs the member file).
    clubdb.ensure_schema()
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO club_members(id, slug, name, is_current) VALUES (61, 'pat', 'Pat', 1)"
        )
    (data / "members" / "pat.json").write_text(
        json.dumps({"name": "Pat", "isCurrent": True, "website": None}) + "\n"
    )

    corpus_write.write_book({"title": "The Next Pick", "authors": ["Some One"]})
    monkeypatch.setattr(corpus_write.cr, "find_book",
                        lambda _q: {"slug": "the-next-pick", "title": "The Next Pick"})
    monkeypatch.setattr(corpus_write.cr, "find_member", lambda _q: {"slug": "pat", "name": "Pat"})

    res = corpus_write.schedule_meeting("The Next Pick", "2026-09-01", "Pat")
    assert res == {"book": "The Next Pick", "date": "2026-09-01", "picker": "Pat"}

    with db.connect() as conn:
        meeting = conn.execute(
            "SELECT m.id, m.placeholder, m.date FROM club_meetings m "
            "JOIN club_meeting_books mb ON mb.meeting_id = m.id "
            "JOIN club_books b ON b.id = mb.book_id WHERE b.slug = 'the-next-pick'"
        ).fetchone()
        picker = conn.execute(
            "SELECT bp.member_id FROM club_book_pickers bp JOIN club_books b ON b.id = bp.book_id "
            "WHERE b.slug = 'the-next-pick'"
        ).fetchone()
    assert meeting is not None and meeting["placeholder"] == 1
    assert picker["member_id"] == 61

    # Corpus meeting file regenerated, picker set on the book file.
    assert (data / "meetings" / f"2026-09-01--{meeting['id']}.json").exists()
    book_doc = json.loads((data / "books" / "the-next-pick.json").read_text())
    assert book_doc["picker"] == ["pat"]
