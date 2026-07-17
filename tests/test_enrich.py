"""Enrichment data layer: sidecar COALESCE/gap-fill, the merge logic in the loop
(with mocked sources — no network), and corpus_gen emission of enriched fields."""

from __future__ import annotations

import json

import pytest

from agent import clubdb, corpus_gen, db
from agent.enrich import loop, validation
from agent.enrich import openlibrary as ol
from agent.enrich import wikidata as wd
from agent.enrich import wikipedia as wp

pytestmark = pytest.mark.usefixtures("fresh_db")


def _make_book(conn, slug="enrich-test", title="Enrich Test", synopsis=None):
    bid = clubdb._next_id(conn, "club_books")
    conn.execute(
        "INSERT INTO club_books(id, slug, title, synopsis) VALUES (?,?,?,?)",
        (bid, slug, title, synopsis),
    )
    return bid


class TestSidecarCoalesce:
    def test_sidecar_fills_gap_and_net_new(self):
        with db.connect() as conn:
            bid = _make_book(conn, synopsis=None)
            clubdb.upsert_book_enrichment(
                conn, bid, {"synopsis": "from OL", "ratings_average": 4.1}
            )
            b = next(b for b in clubdb.all_books(conn) if b["id"] == bid)
            assert b["synopsis"] == "from OL"  # dual-source gap filled from sidecar
            assert b["ratings_average"] == 4.1  # net-new comes straight from sidecar

    def test_curated_core_wins(self):
        with db.connect() as conn:
            bid = _make_book(conn, slug="enrich-curated", synopsis="curated text")
            clubdb.upsert_book_enrichment(conn, bid, {"synopsis": "from OL"})
            b = next(b for b in clubdb.all_books(conn) if b["id"] == bid)
            assert b["synopsis"] == "curated text"  # core wins over sidecar mirror

    def test_upsert_merges_idempotently(self):
        with db.connect() as conn:
            bid = _make_book(conn, slug="enrich-merge")
            clubdb.upsert_book_enrichment(conn, bid, {"ratings_average": 4.0})
            clubdb.upsert_book_enrichment(conn, bid, {"ratings_count": 99})
            e = clubdb.book_enrichment(conn, bid)
            assert e["ratings_average"] == 4.0 and e["ratings_count"] == 99

    def test_author_bio_coalesce(self):
        with db.connect() as conn:
            aid = clubdb._author_id(conn, "Coalesce Author")  # bio NULL
            clubdb.upsert_author_enrichment(
                conn,
                aid,
                {
                    "bio": "enriched bio",
                    "nationality": "Canada",
                    "notable_works_json": json.dumps(["Book A"]),
                },
            )
            a = next(a for a in clubdb.all_authors(conn) if a["id"] == aid)
            assert a["bio"] == "enriched bio"
            assert a["nationality"] == "Canada"
            assert a["notable_works"] == ["Book A"]


class TestEnrichBook:
    def test_writes_sidecar_from_sources(self, monkeypatch):
        monkeypatch.setattr(
            ol,
            "book_facts",
            lambda *a, **k: {
                "ol_key": "/works/OLX",
                "ol_cover_id": 123,
                "edition_count": 5,
                "languages": ["eng"],
                "subjects": ["Space"],
                "synopsis": "syn",
                "publication_year": 2011,
                "page_count": 300,
                "isbn13": "9780000000000",
                "ratings_average": 4.2,
                "ratings_count": 50,
                "author_keys": [],
            },
        )
        monkeypatch.setattr(wd, "resolve_book", lambda *a, **k: {"id": "Q1"})
        monkeypatch.setattr(
            wd,
            "book_facts",
            lambda ent: {
                "wikidata_id": "Q1",
                "awards": ["Hugo Award"],
                "series": "A Series",
                "goodreads_id": "g1",
                "wikipedia_url": "https://wiki/x",
            },
        )
        with db.connect() as conn:
            bid = _make_book(conn, slug="enrich-book-src", synopsis=None)
            book = next(b for b in clubdb.all_books(conn) if b["id"] == bid)
            loop.enrich_book(conn, book, fetch_images=False)
            e = clubdb.book_enrichment(conn, bid)
        assert e["ol_cover_id"] == 123
        assert e["ratings_average"] == 4.2 and e["ratings_count"] == 50
        assert e["wikidata_id"] == "Q1"
        assert json.loads(e["awards_json"]) == ["Hugo Award"]
        assert json.loads(e["languages_json"]) == ["eng"]
        assert e["synopsis"] == "syn"  # core was empty → mirrored
        assert e["enriched_at"]

    def test_curated_synopsis_not_mirrored(self, monkeypatch):
        monkeypatch.setattr(
            ol, "book_facts", lambda *a, **k: {"synopsis": "OL synopsis", "author_keys": []}
        )
        monkeypatch.setattr(wd, "resolve_book", lambda *a, **k: None)
        with db.connect() as conn:
            bid = _make_book(conn, slug="enrich-book-curated", synopsis="curated")
            book = next(b for b in clubdb.all_books(conn) if b["id"] == bid)
            loop.enrich_book(conn, book, fetch_images=False)
            e = clubdb.book_enrichment(conn, bid)
        assert e.get("synopsis") is None  # core already had it; no mirror written


class TestEnrichAuthor:
    def test_bio_gapfills_from_wikipedia_and_merges(self, monkeypatch):
        monkeypatch.setattr(loop, "_discover_ol_author_key", lambda *a, **k: "/authors/OLA")
        monkeypatch.setattr(ol, "author", lambda key: {"present": True})
        monkeypatch.setattr(
            ol,
            "author_facts",
            lambda rec: {
                "bio": None,
                "birth_year": 1970,
                "death_year": None,
                "website": "https://site",
                "wikidata_id": "Q2",
                "ol_photo_id": None,
            },
        )
        monkeypatch.setattr(
            wd,
            "resolve_author",
            lambda *a, **k: wd.AuthorResolution({"id": "Q2"}, "openlibrary", ("openlibrary_link",)),
        )
        monkeypatch.setattr(
            wd,
            "author_facts",
            lambda ent: {
                "wikidata_id": "Q2",
                "birth_year": 1970,
                "death_year": None,
                "nationality": "Canada",
                "notable_works": ["Book A"],
                "website": None,
                "image_filename": None,
                "wikipedia_url": "https://wiki/a",
                "wikipedia_title": "Author A",
            },
        )
        monkeypatch.setattr(
            wp,
            "summary",
            lambda *a, **k: {
                "extract": "Wikipedia bio",
                "thumbnail_url": None,
                "wikipedia_url": "https://wiki/a",
            },
        )
        with db.connect() as conn:
            aid = clubdb._author_id(conn, "Gapfill Author")  # bio NULL
            author = next(a for a in clubdb.all_authors(conn) if a["id"] == aid)
            loop.enrich_author(conn, author, fetch_images=False)
            e = clubdb.author_enrichment(conn, aid)
            a2 = next(a for a in clubdb.all_authors(conn) if a["id"] == aid)
        assert e["bio"] == "Wikipedia bio"  # OL had none → Wikipedia extract
        assert e["birth_year"] == 1970 and e["nationality"] == "Canada"
        assert json.loads(e["notable_works_json"]) == ["Book A"]
        assert e["validation_status"] == "accepted"
        assert json.loads(e["validation_warnings_json"]) == []
        assert a2["bio"] == "Wikipedia bio"  # surfaced via COALESCE
        assert a2["birth_year"] == 1970

    def test_invalid_chronology_is_quarantined_but_raw_provenance_remains(self, monkeypatch):
        monkeypatch.setattr(loop, "_discover_ol_author_key", lambda *a, **k: "/authors/OLA")
        monkeypatch.setattr(ol, "author", lambda key: {})
        monkeypatch.setattr(
            ol,
            "author_facts",
            lambda rec: {
                "bio": None,
                "birth_year": None,
                "death_year": None,
                "website": None,
                "wikidata_id": None,
                "ol_photo_id": None,
            },
        )
        monkeypatch.setattr(
            wd,
            "resolve_author",
            lambda *a, **k: wd.AuthorResolution({"id": "QBAD"}, "search", ("known_work_author",)),
        )
        monkeypatch.setattr(
            wd,
            "author_facts",
            lambda ent: {
                "wikidata_id": "QBAD",
                "birth_year": 1952,
                "death_year": 1900,
                "nationality": None,
                "notable_works": [],
                "website": None,
                "image_filename": None,
                "wikipedia_url": None,
                "wikipedia_title": None,
            },
        )
        monkeypatch.setattr(wp, "summary", lambda *a, **k: {})
        with db.connect() as conn:
            aid = clubdb._author_id(conn, "Chronology Author")
            clubdb.upsert_author_enrichment(conn, aid, {"death_year": 1900})
            author = next(a for a in clubdb.all_authors(conn) if a["id"] == aid)
            loop.enrich_author(conn, author, force=True, fetch_images=False)
            enriched = clubdb.author_enrichment(conn, aid)
            projected = next(a for a in clubdb.all_authors(conn) if a["id"] == aid)

        assert enriched["birth_year"] == 1952
        assert enriched["death_year"] is None
        assert enriched["validation_status"] == "partial"
        assert json.loads(enriched["validation_warnings_json"]) == ["death_year_before_birth_year"]
        raw = json.loads(enriched["enrichment_json"])
        assert raw["wikidata"]["death_year"] == 1900
        assert raw["validation"]["status"] == "partial"
        assert "deathYear" not in corpus_gen._author_doc(projected)


def test_validate_author_facts_preserves_bce_chronology():
    result = validation.validate_author_facts(
        {},
        {"birth_year": -525, "death_year": -456},
        wd.AuthorResolution({"id": "Q1"}, "search", ("known_work_author",)),
        current_year=2026,
    )

    assert (result.birth_year, result.death_year) == (-525, -456)
    assert result.status == "accepted"


class TestCorpusGenEmitsEnrichment:
    BASE_BOOK = {
        "id": 1,
        "title": "T",
        "subtitle": None,
        "author_names": ["A"],
        "topic": "X",
        "fiction": 1,
        "publication_year": 2000,
        "page_count": 100,
        "isbn13": None,
        "ol_key": None,
        "synopsis": None,
        "picker_slugs": [],
        "subjects_json": None,
        "subjects": [],
    }

    def test_book_doc_emits_present_fields_only(self):
        b = {
            **self.BASE_BOOK,
            "edition_count": 7,
            "languages": ["eng"],
            "ratings_average": 4.5,
            "ratings_count": 12,
            "series": "S",
            "awards": ["Hugo"],
            "wikidata_id": "Q9",
            "wikipedia_url": "https://w",
            "goodreads_id": "g",
        }
        doc = corpus_gen._book_doc(b)
        assert doc["editionCount"] == 7
        assert doc["ratingsAverage"] == 4.5
        assert doc["awards"] == ["Hugo"]
        assert doc["wikipediaUrl"] == "https://w"

    def test_book_doc_omits_empty_enrichment(self):
        b = {
            **self.BASE_BOOK,
            "edition_count": None,
            "languages": [],
            "ratings_average": None,
            "ratings_count": None,
            "series": None,
            "awards": [],
            "wikidata_id": None,
            "wikipedia_url": None,
            "goodreads_id": None,
        }
        doc = corpus_gen._book_doc(b)
        for absent in (
            "editionCount",
            "languages",
            "ratingsAverage",
            "awards",
            "wikidataId",
            "wikipediaUrl",
            "goodreadsId",
            "series",
        ):
            assert absent not in doc

    def test_author_doc_emits_enrichment(self):
        a = {
            "name": "A",
            "bio": "b",
            "birth_year": 1900,
            "death_year": 1980,
            "nationality": "France",
            "website": "https://s",
            "wikipedia_url": "https://w",
            "notable_works": ["W1"],
            "photo_credit": "Wikimedia Commons",
        }
        doc = corpus_gen._author_doc(a)
        assert doc["birthYear"] == 1900 and doc["deathYear"] == 1980
        assert doc["nationality"] == "France"
        assert doc["notableWorks"] == ["W1"]
        assert doc["photoCredit"] == "Wikimedia Commons"

    def test_author_doc_omits_empty(self):
        a = {
            "name": "A",
            "bio": None,
            "birth_year": None,
            "death_year": None,
            "nationality": None,
            "website": None,
            "wikipedia_url": None,
            "notable_works": [],
            "photo_credit": None,
        }
        assert corpus_gen._author_doc(a) == {"name": "A"}
