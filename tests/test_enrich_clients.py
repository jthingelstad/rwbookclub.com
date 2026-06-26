"""Unit tests for the enrichment source clients — the verification/extraction logic that
test_enrich.py mocks away. HTTP is mocked at the boundary (entity/search/labels/_fetch/
SESSION.get); no network."""

from __future__ import annotations

from agent.enrich import http
from agent.enrich import loop
from agent.enrich import openlibrary as ol
from agent.enrich import wikidata as wd
from agent.enrich import wikipedia as wp


# ── Wikidata: claim parsing + extraction ─────────────────────────────────────
def _ent(qid, *, p31=(), p106=(), p569=None, p570=None, p27=None, p50=(),
         p800=(), p856=None, enwiki=None):
    def idclaims(ids):
        return [{"mainsnak": {"datavalue": {"value": {"id": i}}}} for i in ids]

    def timeclaim(t):
        return [{"mainsnak": {"datavalue": {"value": {"time": t}}}}]

    def strclaim(s):
        return [{"mainsnak": {"datavalue": {"value": s}}}]

    claims = {}
    for prop, ids in (("P31", p31), ("P106", p106), ("P50", p50), ("P800", p800)):
        if ids:
            claims[prop] = idclaims(ids)
    if p27:
        claims["P27"] = idclaims([p27])
    if p569:
        claims["P569"] = timeclaim(p569)
    if p570:
        claims["P570"] = timeclaim(p570)
    if p856:
        claims["P856"] = strclaim(p856)
    ent = {"id": qid, "claims": claims, "sitelinks": {}}
    if enwiki:
        ent["sitelinks"]["enwiki"] = {"title": enwiki}
    return ent


def test_year_from_time_handles_ad_and_bce():
    assert wd._year_from_time(_ent("Q1", p569="+1947-03-12T00:00:00Z"), "P569") == 1947
    assert wd._year_from_time(_ent("Q1", p569="-0470-01-01T00:00:00Z"), "P569") == -470
    assert wd._year_from_time(_ent("Q1"), "P569") is None


def test_author_facts_extracts_fields(monkeypatch):
    ent = _ent("Q42", p569="+1971-07-17T00:00:00Z", p27="Q16", p800=["QW1"],
               p856="https://example.com", enwiki="Cory Doctorow")
    monkeypatch.setattr(wd, "labels", lambda ids: {"Q16": "Canada", "QW1": "Little Brother"})
    facts = wd.author_facts(ent)
    assert facts["birth_year"] == 1971
    assert facts["nationality"] == "Canada"
    assert facts["notable_works"] == ["Little Brother"]
    assert facts["website"] == "https://example.com"
    assert facts["wikipedia_url"].endswith("Cory_Doctorow")


def test_resolve_author_trusts_ol_wikidata_link(monkeypatch):
    human = _ent("Q2", p31=["Q5"])
    monkeypatch.setattr(wd, "entity", lambda qid: human if qid == "Q2" else None)
    assert wd.resolve_author("Whoever", ol_wikidata="Q2") is human


def test_resolve_author_requires_writer_occupation_or_birth(monkeypatch):
    writer = _ent("Q10", p31=["Q5"], p106=["Q36180"])              # human + writer
    non_writer = _ent("Q11", p31=["Q5"], p106=["Q937857"])         # human, footballer
    monkeypatch.setattr(wd, "search", lambda name, limit=8: ["Q11", "Q10"])
    monkeypatch.setattr(wd, "entity", lambda qid: {"Q10": writer, "Q11": non_writer}[qid])
    # Q11 (no writer occ, no birth hint) is rejected; Q10 (writer) accepted.
    assert wd.resolve_author("Common Name") is writer
    # With only the non-writer candidate and no hint → None.
    monkeypatch.setattr(wd, "search", lambda name, limit=8: ["Q11"])
    assert wd.resolve_author("Common Name") is None


def test_resolve_book_rejects_substring_author_false_positive(monkeypatch):
    work = _ent("Q20", p31=["Q7725634"], p50=["QA"])
    monkeypatch.setattr(wd, "search", lambda title, limit=8: ["Q20"])
    monkeypatch.setattr(wd, "entity", lambda qid: work)
    # Author label "Bob Crawford"; our last name "ford" must NOT token-match "Crawford".
    monkeypatch.setattr(wd, "labels", lambda ids: {"QA": "Bob Crawford"})
    assert wd.resolve_book("Some Title", ["X Ford"]) is None
    # A genuine whole-word match resolves.
    monkeypatch.setattr(wd, "labels", lambda ids: {"QA": "Richard Ford"})
    assert wd.resolve_book("Some Title", ["X Ford"]) is work


# ── Open Library: pure helpers + composition ─────────────────────────────────
def test_isbn13_and_year():
    assert ol._isbn13(["0-8041-3902-1", "9780804139021"]) == "9780804139021"
    assert ol._isbn13(["nope"]) is None
    assert ol._year("first published 2011 by ...") == 2011
    assert ol._year("no digits") is None


def test_book_facts_composes_from_work_and_doc(monkeypatch):
    monkeypatch.setattr(ol, "search_best_match", lambda t, a: {
        "key": "/works/OLX", "cover_i": 99, "ratings_average": 4.37,
        "ratings_count": 10, "edition_count": 7, "first_publish_year": 2011,
        "number_of_pages_median": 300, "isbn": ["9780804139021"], "subject": ["Mars"]})
    monkeypatch.setattr(ol, "work", lambda key: {
        "description": "A novel.", "subjects": ["Survival"], "covers": [12345],
        "authors": [{"author": {"key": "/authors/OLA"}}]})
    monkeypatch.setattr(ol, "editions", lambda key, limit=50: {"edition_count": 7, "languages": ["eng"]})
    f = ol.book_facts("The Martian", ["Andy Weir"], None, None)
    assert f["ol_key"] == "/works/OLX"
    assert f["ol_cover_id"] == 12345               # work cover wins over doc cover_i
    assert f["synopsis"] == "A novel."
    assert f["ratings_average"] == 4.37
    assert f["isbn13"] == "9780804139021"
    assert f["author_keys"] == ["/authors/OLA"]


def test_resolve_author_key_disambiguates_by_last_name(monkeypatch):
    work_one = {"authors": [{"author": {"key": "/authors/OL1"}}]}
    assert ol.resolve_author_key(work_one, "Solo Author") == "/authors/OL1"
    work_many = {"authors": [{"author": {"key": "/authors/OL1"}},
                             {"author": {"key": "/authors/OL2"}}]}
    monkeypatch.setattr(ol, "author", lambda key: {"/authors/OL1": {"name": "Jane Other"},
                                                   "/authors/OL2": {"name": "Andy Weir"}}[key])
    assert ol.resolve_author_key(work_many, "Andy Weir") == "/authors/OL2"


# ── Wikipedia: override + disambiguation handling ────────────────────────────
def test_summary_honors_override(monkeypatch):
    fetched = []

    def fake_fetch(title):
        fetched.append(title)
        return {"type": "standard", "extract": "YC essayist."} if "programmer" in title else None

    monkeypatch.setattr(wp, "_fetch", fake_fetch)
    out = wp.summary("paul-graham", "Paul Graham")          # OVERRIDES → _(programmer)
    assert out and out["extract"] == "YC essayist."
    assert any("programmer" in t for t in fetched)


def test_summary_none_override_skips(monkeypatch):
    monkeypatch.setattr(wp, "_fetch", lambda title: {"type": "standard", "extract": "x"})
    assert wp.summary("steve-weber", "Steve Weber") is None   # OVERRIDES → None


def test_usable_rejects_disambiguation_and_empty():
    assert wp._usable({"type": "disambiguation", "extract": "x"}) is None
    assert wp._usable({"type": "standard", "extract": "   "}) is None
    assert wp._usable({"type": "standard", "extract": "Real bio."})["extract"] == "Real bio."


# ── HTTP boundary: non-fatal on any error ────────────────────────────────────
def test_get_json_returns_none_on_error(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda *_a: None)

    class _Resp:
        ok = False
        def json(self):
            raise AssertionError("must not parse a non-ok response")

    monkeypatch.setattr(http.SESSION, "get", lambda *a, **k: _Resp())
    assert http.get_json("https://x") is None

    def boom(*a, **k):
        raise ConnectionError("network down")

    monkeypatch.setattr(http.SESSION, "get", boom)
    assert http.get_json("https://x") is None


# ── Loop: gap-fill selection ─────────────────────────────────────────────────
def test_select_gap_fills_and_filters():
    items = [{"id": 1, "slug": "a"}, {"id": 2, "slug": "b"}, {"id": 3, "slug": "c"}]
    done = {1}
    assert [e["id"] for e in loop._select(items, done, id_key="id", force=False, slug=None, limit=None)] == [2, 3]
    assert [e["id"] for e in loop._select(items, done, id_key="id", force=True, slug=None, limit=None)] == [1, 2, 3]
    assert [e["id"] for e in loop._select(items, done, id_key="id", force=True, slug="b", limit=None)] == [2]
    assert len(loop._select(items, set(), id_key="id", force=True, slug=None, limit=2)) == 2
