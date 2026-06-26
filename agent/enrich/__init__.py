"""Data-layer enrichment loop — fills the club_*_enrichment sidecars from external
sources (Open Library, Wikidata, Wikipedia) so a bare title + author becomes rich
data for Oliver and the website.

This package is the ONLY writer of the enrichment sidecars; it never touches the
curated core tables. Run it deliberately and online::

    python -m agent.enrich [--books] [--authors] [--force] [--limit N] [--slug X]

It is idempotent and gap-filling: already-enriched rows (``enriched_at`` set) are
skipped unless ``--force``. ``agent.corpus_gen`` stays network-free — it only reads
what this loop has already written into the DB.
"""
