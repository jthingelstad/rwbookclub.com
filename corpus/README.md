# corpus

The knowledge corpus for the R/W Book Club — per-entity text files consumed by the
website build and by Oliver at runtime. **It is generated from the authoritative
`club_*` SQLite tables** (`agent/corpus_gen.py`), not hand-edited, and is
**gitignored/private** (regenerated on disk, never committed — so it can hold
sensitive context for Oliver). Airtable was the original home, now a cold backup.

## Layout

```
corpus/
├── images.py       # ensure book covers exist; fetch missing ones from Open Library (OL ids from the DB)
├── validate.py     # referential-integrity check (every slug reference resolves)
├── paths.py        # filesystem paths + the slug helper
├── requirements.txt
└── data/                       # GENERATED from the club_* tables; gitignored (private, on-disk only)
    ├── books/<slug>.json       # 179
    ├── members/<slug>.json     # 12
    ├── meetings/<date>--<id>.json   # 184 (first-class)
    ├── authors/<slug>.json
    ├── reviews/<book-slug>--<member-slug>.md   # YAML frontmatter + prose body
    └── awards/<year>-<slug>.json
```

**Normalized — each fact is stored once.** Book files hold intrinsic fields plus
`picker` (member slugs); **meetings own the date + book refs**; members/authors hold
identity only. Relationships are **slug references** (the readable "foreign key").
Everything derivable — a book's meeting date, a member's picks, review counts, names
behind slugs — is **computed at build/read time**, not stored, so there's nothing to
keep in sync. The website (`website/src/_data/*.js`) and the agent
(`agent/corpus_read.py`) each do these joins. Records are JSON; reviews are Markdown +
YAML frontmatter (body = the prose). `corpus/validate.py` checks every reference resolves.

## Editing

Don't hand-edit these files (a regen clobbers them) — edit the DB via Oliver's
write tools, then `python -m agent.publish` regenerates the corpus, builds, and
deploys the site to the `gh-pages` branch.

## Covers

```bash
python -m corpus.images    # from the repo root
```

Idempotent and self-healing: only fetches covers that are missing on disk,
sourcing them from Open Library via each book's `olKey`. Existing resized
variants in `website/src/assets/images/covers/` are left untouched. Member
photos are no longer fetched automatically (Airtable held those URLs) — add a
new member's photo file manually.

## Regenerating

The corpus is rebuilt from the DB; it is never hand-edited:

```bash
python -m agent.corpus_gen   # club_* tables → corpus/data/* (full regen + prune)
python -m corpus.validate    # check every reference resolves
python -m corpus.images      # backfill any missing covers
```

One-time re-seed of the DB from the original Airtable base (requires `AIRTABLE_PAT`):
`python -m agent.script.import_airtable` (the table IDs live in that script).

## Schema

The full club schema (tables, fields, conventions) is documented in the repo-root `CLAUDE.md`.
