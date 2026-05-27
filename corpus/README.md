# corpus

The canonical knowledge corpus for the R/W Book Club. **Git is the source of
truth** — the data lives here as per-entity text files, edited directly (by
people via PRs, and by the agent via commits). Airtable was the original home
and is now a **cold backup only**.

## Layout

```
corpus/
├── airtable.py     # shared client (still used by the agent + cold-backup re-import)
├── images.py       # ensure book covers exist; fetch missing ones from Open Library
├── fetch.py        # COLD BACKUP: re-pull Airtable into grouped JSON (see below)
├── migrate.py      # COLD BACKUP: explode grouped JSON into per-entity files
├── requirements.txt
└── data/                       # canonical, committed
    ├── books/<slug>.json       # 179
    ├── members/<slug>.json     # 12
    ├── meetings/<date>--<id>.json   # 184 (first-class)
    ├── authors/<slug>.json
    ├── reviews/<book-slug>--<member-slug>.md   # YAML frontmatter + prose body
    └── awards/<year>-<slug>.json
```

Records are JSON; reviews are Markdown with YAML frontmatter (the body is the
review prose). Every entity keeps its original Airtable `rec…` id as the stable
join key. The website (`website/src/_data/*.js`) globs these directories and
aggregates them at build time.

## Editing

Edit the files directly and commit. A push to `main` rebuilds and deploys the
site (`.github/workflows/deploy.yml`). Git history is the club's audit log.

## Covers

```bash
python -m corpus.images    # from the repo root
```

Idempotent and self-healing: only fetches covers that are missing on disk,
sourcing them from Open Library via each book's `olKey`. Existing resized
variants in `website/src/assets/images/covers/` are left untouched. Member
photos are no longer fetched automatically (Airtable held those URLs) — add a
new member's photo file manually.

## Cold-backup re-import from Airtable (rarely needed)

Airtable is kept read-only as a safety net. To re-pull it (requires
`AIRTABLE_PAT` in the root `.env`):

```bash
python -m corpus.fetch      # Airtable → grouped JSON in corpus/data/raw/
python -m corpus.migrate    # grouped JSON → per-entity files (+ meetings from Airtable)
python -m corpus.images     # backfill any missing covers
```

Review the diff before committing — this overwrites the per-entity files with
Airtable's current state.

## Schema

The full Airtable schema (table IDs, fields, conventions) is documented in the
repo-root `CLAUDE.md`; the table IDs live in `airtable.py`.
