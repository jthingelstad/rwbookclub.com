"""Pure resolver/cleaning helpers — author matching + subject cleaning.

These were salvaged from the retired ``corpus/openlibrary_subjects.py`` into the
live enrichment client ``agent/enrich/openlibrary.py`` when enrichment moved into
the data layer; the tests follow them there.
"""

from __future__ import annotations

from agent.enrich.openlibrary import (
    MAX_SUBJECTS,
    MAX_TAG_LEN,
    _author_matches,
    clean_subjects,
)


class TestAuthorMatches:
    def test_no_authors_no_constraint(self):
        # No constraint when our_authors is empty.
        assert _author_matches(["Anyone"], [])

    def test_last_name_match(self):
        # The fix that caught Michael Pollan's "A World Appears" being matched
        # to the quantum-mechanics monograph: require last-name overlap.
        assert _author_matches(["Michael Pollan", "Other"], ["Michael Pollan"])
        assert _author_matches(["Pollan, Michael"], ["Michael Pollan"])

    def test_no_overlap_rejects(self):
        assert not _author_matches(["Someone Else"], ["Michael Pollan"])

    def test_doc_has_no_authors_rejects(self):
        # If we have a constraint but the doc lists no authors, can't match.
        assert not _author_matches([], ["Michael Pollan"])
        assert not _author_matches(None, ["Michael Pollan"])


class TestCleanSubjects:
    def test_strips_generic_tags(self):
        out = clean_subjects(["Fiction", "Murder mystery", "English language", "Forensic science"])
        # 'Fiction' and 'English language' are in SKIP_TAGS.
        assert "Murder mystery" in out
        assert "Forensic science" in out
        assert "Fiction" not in out
        assert "English language" not in out

    def test_dedupes_case_insensitive(self):
        out = clean_subjects(["History", "history", "Politics"])
        # 'history' is in SKIP_TAGS too; only Politics survives.
        assert out == ["Politics"]

    def test_caps_at_max(self):
        many = [f"Topic {i}" for i in range(MAX_SUBJECTS + 5)]
        out = clean_subjects(many)
        assert len(out) == MAX_SUBJECTS

    def test_drops_too_long_tags(self):
        long_tag = "x" * (MAX_TAG_LEN + 10)
        out = clean_subjects([long_tag, "Normal tag"])
        assert long_tag not in out
        assert "Normal tag" in out

    def test_handles_non_strings(self):
        out = clean_subjects([None, 42, "Real tag", {"weird": "obj"}])
        assert out == ["Real tag"]

    def test_handles_none(self):
        assert clean_subjects(None) == []
        assert clean_subjects([]) == []
