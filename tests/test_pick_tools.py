"""The picking funnel: affinity_to_history, pick_fit, pick_prospects — and the doctrine."""

from __future__ import annotations

import json

from agent import corpus_read as cr
from agent import db, oliver, tools
from agent.tools import dispatch


def test_affinity_scores_author_over_subjects():
    hits = cr.affinity_to_history(["Science fiction"], ["Kazuo Ishiguro"], title="Klara")
    assert hits, "read-author candidate must find neighbors"
    assert "Kazuo Ishiguro" in hits[0]["reasons"][0]  # author match ranks first (+60)
    assert all(h["score"] >= 24 for h in hits)  # threshold holds
    assert hits[0]["yearRead"] and hits[0]["picker"]  # club framing fields present


def test_affinity_empty_inputs_degrade():
    assert cr.affinity_to_history([], [], title="") == []


def test_unread_notable_works_diffs_read_titles():
    leads = cr.unread_notable_works(limit=20)
    assert leads, "enriched authors with unread notable works exist in the fixture corpus"
    read_titles = {(b.get("title") or "").lower() for b in cr.books() if b.get("isRead")}
    for lead in leads:
        for w in lead["unreadNotableWorks"]:
            assert w.lower() not in read_titles  # genuinely unread
        assert lead["clubVerdicts"]  # annotated with club evidence


def _ol_doc(**over):
    doc = {
        "title": "Nexus",
        "author_name": ["Yuval Noah Harari"],
        "first_publish_year": 2024,
        "number_of_pages_median": 528,
        "subject": ["Artificial intelligence", "History"],
        "ratings_average": 3.9,
        "ratings_count": 1200,
        "key": "/works/OLX",
    }
    doc.update(over)
    return doc


def test_pick_fit_external_candidate(fresh_db, monkeypatch):
    from agent.enrich import openlibrary as enrich_ol

    monkeypatch.setattr(enrich_ol, "search_best_match", lambda t, a: _ol_doc())
    db.add_memory(
        "Skeptical of big-idea nonspecialist nonfiction like Harari",
        scope="member",
        subject="loren",
        source="reflection",
    )
    db.add_memory(
        "Likes systems books with a strong argument",
        scope="member",
        subject="jamie",
        source="reflection",
    )
    out = json.loads(
        dispatch(
            "pick_fit",
            {"title": "Nexus", "author": "Yuval Noah Harari"},
            {"member_slug": "jamie", "speaker": "Jamie", "channel_id": "123"},
        )
    )
    assert out["candidate"]["resolved"] == "openlibrary"
    assert out["candidate"]["pages"] == 528
    assert out["nearestInHistory"], "shared-subject neighbors on the shelf"
    assert out["lengthPrecedents"]
    assert "clubVerdict" in out["nearestInHistory"][0]  # ratings + discussionAverage carried
    assert out["memberLenses"]["loren"]["memories"] == []
    assert out["memberLenses"]["jamie"]["memories"] == []
    calibration = out["silentPrivateCalibration"]
    assert "Likes systems books" in " ".join(calibration["signals"])
    assert calibration["output_visibility"] == "silent_private_context"
    assert "likely holdout" in out["note"]
    assert "not evidence of club history or group reaction" in out["note"]
    assert "neutral criterion to check" in out["note"]
    assert out["coverage"]["topics"] and "note" in out
    # The consideration was recorded into the cloud, attributed via ctx.
    row = db.recent_book_cloud()[0]
    assert row["title"] == "Nexus" and row["reason_kind"] == "pick_candidate"
    assert row["mentioned_by"] == "jamie"


def test_pick_fit_long_candidate_surfaces_public_scale_precedent(fresh_db, monkeypatch):
    from agent.enrich import openlibrary as enrich_ol

    monkeypatch.setattr(
        enrich_ol,
        "search_best_match",
        lambda t, a: _ol_doc(title="The Power Broker", number_of_pages_median=1263),
    )
    out = json.loads(
        dispatch(
            "pick_fit",
            {"title": "The Power Broker", "author": "Robert Caro"},
            {"member_slug": "jamie", "speaker": "Jamie", "channel_id": "123"},
        )
    )
    team = next(row for row in out["lengthPrecedents"] if row["title"] == "Team of Rivals")
    assert team["pages"] == 1308
    assert team["ratingAverage"] == 5
    assert team["discussionAverage"] == 5
    assert "Use lengthPrecedents" in out["note"]


def test_pick_fit_already_read_headline(fresh_db, monkeypatch):
    from agent.enrich import openlibrary as enrich_ol

    monkeypatch.setattr(
        enrich_ol,
        "search_best_match",
        lambda t, a: (_ for _ in ()).throw(AssertionError("no OL for corpus hit")),
    )
    out = json.loads(
        dispatch("pick_fit", {"title": "Watchmen"}, {"member_slug": "erik", "channel_id": "123"})
    )
    assert out["candidate"]["resolved"] == "corpus"
    assert out["alreadyRead"]["yearRead"] == "2007"
    assert db.recent_book_cloud() == []  # re-reads aren't cloud candidates


def test_pick_fit_unresolved_degrades(fresh_db, monkeypatch):
    from agent.enrich import openlibrary as enrich_ol

    monkeypatch.setattr(enrich_ol, "search_best_match", lambda t, a: None)
    out = json.loads(
        dispatch(
            "pick_fit",
            {"title": "A Book Nobody Indexed"},
            {"member_slug": "tom", "channel_id": "123"},
        )
    )
    assert out["candidate"]["resolved"] == "unresolved"
    assert out["memberLenses"] and "note" in out  # lenses/lore still useful
    assert db.recent_book_cloud()[0]["title"] == "A Book Nobody Indexed"


def test_pick_fit_surfaces_cloud_history(fresh_db, monkeypatch):
    from agent.enrich import openlibrary as enrich_ol

    monkeypatch.setattr(enrich_ol, "search_best_match", lambda t, a: _ol_doc())
    db.add_book_cloud_entry(
        title="Nexus",
        reason="Nick compared it to The Master Algorithm",
        surface="mailing_list",
        mentioned_by="nick",
        created_at="2024-11-01 00:00:00",
    )
    out = json.loads(
        dispatch("pick_fit", {"title": "Nexus"}, {"member_slug": "jamie", "channel_id": "123"})
    )
    assert out["cloudHistory"]["mentioners"] == ["nick"]
    assert out["cloudHistory"]["first_mentioned"].startswith("2024-11-01")


def test_pick_prospects_defaults_to_asker_and_splits_cloud(fresh_db):
    db.add_memory(
        "Prefers translated fiction", scope="member", subject="jamie", source="reflection"
    )
    db.add_book_cloud_entry(
        title="The Power Broker",
        reason="Jamie floated it as a systems book",
        surface="discord",
        mentioned_by="jamie",
    )
    db.add_book_cloud_entry(
        title="Piranesi",
        reason="Loren suggested as light fiction",
        surface="discord",
        mentioned_by="loren",
    )
    db.add_book_cloud_entry(
        title="Watchmen",
        reason="already-read mention",
        surface="discord",
        book_slug="watchmen",
        mentioned_by="tom",
    )
    out = json.loads(dispatch("pick_prospects", {}, {"member_slug": "jamie", "channel_id": "123"}))
    assert out["member"] == "jamie"  # ctx default, no input needed
    assert out["memberTaste"]["memories"] == []
    assert "Prefers translated fiction" in out["silentPrivateCalibration"]["signals"]
    yours = [r["title"] for r in out["cloudProspects"]["yours"]]
    orbit = [r["title"] for r in out["cloudProspects"]["clubOrbit"]]
    assert yours == ["The Power Broker"] and "Piranesi" in orbit
    assert "Watchmen" not in yours + orbit  # read books filtered from prospects
    assert out["lovedAuthorsUnread"] and out["searchAngles"]
    assert any("new book by" in a for a in out["searchAngles"])


def test_pick_prospects_private_reply_may_reference_own_taste(fresh_db):
    db.add_memory(
        "Prefers translated fiction", scope="member", subject="jamie", source="reflection"
    )
    out = json.loads(
        dispatch(
            "pick_prospects",
            {},
            {"member_slug": "jamie", "channel_id": "email:private-thread"},
        )
    )
    assert out["memberTaste"]["memories"] == ["Prefers translated fiction"]
    assert "silentPrivateCalibration" not in out


def test_pick_prospects_direction_angles_lead(fresh_db):
    ctx = {"member_slug": "jamie", "channel_id": "123"}
    out = json.loads(dispatch("pick_prospects", {"direction": "urban history"}, ctx))
    assert "urban history" in out["searchAngles"][0]  # direction angles come FIRST
    assert sum("urban history" in a for a in out["searchAngles"]) >= 3
    assert "direction drives" in out["note"]  # fresh-first guidance
    plain = json.loads(dispatch("pick_prospects", {}, ctx))
    assert "fresh candidates" in plain["note"].lower() or "fresh" in plain["note"]


def test_doctrines_present_in_system_prompt():
    p = oliver.OPERATIONAL_PROMPT
    assert "BOOK CLOUD." in p and "not a queue" in p
    assert "PICK HELP." in p and "THE MEETING THE BOOK WOULD PRODUCE" in p
    assert "TOPIC-FIRST" in p and "ASK BEFORE ADVISING" in p  # ask where they want to go, first
    assert "2-7 days old" in p and "older than a week, start fresh" in p  # thread staleness ladder
    assert "seasoning, not the meal" in p  # cloud/known-author leads demoted
    assert "would LEARN" in p and "never invent a reaction" in p
    assert "SHARED OUTPUT PRIVACY." in p
    assert "likely holdout" in p and "neutral criterion to check" in p
    assert "NOT evidence of club history" in p
    assert "Never pluralize one private signal" in p
    assert "fault lines" in p
    assert "do not repeat those labels even to deny them" in p
    assert "has or lacks a precedent" in p
    assert "Do not echo the question's who/resist/holdout wording" in p
    assert "density may split the room" not in p
    names = {t.get("name") for t in tools.TOOLS}
    assert {"book_cloud_add", "book_cloud_recent", "pick_fit", "pick_prospects"} <= names
