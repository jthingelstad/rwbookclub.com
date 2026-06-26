# CLAUDE.md — rwbookclub.com

Project-specific context for Claude Code. The site is live at rwbookclub.com (served by GitHub Pages from the **`gh-pages` branch**, built + deployed **locally** by Oliver — not from `main`/CI). As of May 2026 there are 179 books, 184 meetings, 177 authors, and 12 members (5 current).

## Project

rwbookclub.com is the home of the R/W Book Club, which has been meeting since April 2003. The club reads about 8 books per year, mostly non-fiction (about 88%), and rotates picking and hosting among members.

**SQLite is the source of truth** for club data — the `club_*` tables in `agent/oliver.db` hold the authoritative club record (books/meetings/members/authors/reviews/awards) under integer primary keys and real foreign keys. The Git corpus (`corpus/data/`) **and** the website are **generated** from it (`python -m agent.corpus_gen`). **Do not hand-edit `corpus/data/` — a regen will clobber it.** Change the data through Oliver's write tools / the DB. Airtable was the original home and was retired after a one-time import (`python -m agent.script.import_airtable`). See `docs/archive/MIGRATION-PLAN.md` and `docs/archive/MIGRATION-STATUS.md` for the full inversion history (it happened 2026-06-26), and `docs/archive/AIRTABLE-REFERENCE.md` for the retired Airtable schema.

## Monorepo layout

This repo is a flat, polyglot monorepo with three top-level concerns:

- **`website/`** — the Eleventy 3 static site (Node). Consumes the corpus.
- **`corpus/`** — the **generated** knowledge layer (Python). Per-entity text files in `corpus/data/` (`books/`, `members/`, `meetings/`, `authors/`, `reviews/`, `awards/`) are produced from the `club_*` SQLite tables by `agent/corpus_gen.py`, reproducing the normalized on-disk shape: each fact once, relationships by **slug** (slug = filename stem, an output detail — never identity in the DB), derived fields computed at build/read time. Records are JSON, reviews Markdown+frontmatter. `corpus/validate.py` checks reference integrity. `corpus/images.py` backfills covers from Open Library (reads OL ids from the DB). (The legacy Airtable→corpus modules `fetch.py`/`migrate.py`/`normalize.py` were **removed**; the one-time re-seed path is now `agent.script.import_airtable` → DB → `agent.corpus_gen`.) **Oliver now owns this**: writes land in the DB, then the corpus is regenerated on disk (it is **gitignored/private**, never committed) and the site is rebuilt + deployed locally to `gh-pages` (see "Site build + deploy").
- **External enrichment** lives in **1:1 sidecar tables** (`club_book_enrichment`, `club_author_enrichment`) that the loop `agent/enrich/` owns exclusively — the curated `club_books`/`club_authors` core is never written by enrichment, so it can't be clobbered, and enrichment is regenerable (`DELETE` + re-run). Run it deliberately/online: `python -m agent.enrich [--books] [--authors] [--force] [--limit N] [--slug X]` (sources: Open Library + Wikidata + Wikipedia; Google Books stays blocked). Gap-filling + idempotent (skips rows with `enriched_at` set). `corpus_gen` stays network-free; `all_books`/`all_authors` `COALESCE` the dual-source fields (synopsis/bio/year/pages/isbn/subjects) **core-first**, while net-new fields (ratings/editions/dates/nationality/links/awards/notable works) come straight from the sidecar. Author portraits land in `website/src/assets/images/authors/`. `/oliver add-book` triggers inline enrichment (gated off in tests via `OLIVER_ENRICH_ON_WRITE=0`).
- **`agent/`** — Oliver, the club's Discord bot (Python). Consumes the corpus; answers questions in `#ask-oliver` via Claude.

A root `package.json` (npm workspace over `website`) provides `npm run build`/`serve`/`covers`. All Python runs from the repo root (`python -m corpus.images`, `python -m agent.bot`). One shared root `.env`.

## Site build + deploy (local — the corpus is private, not in git)

The corpus (`corpus/data/`) and the machine-generated images (`assets/images/{covers,authors}/`)
are **gitignored, on-disk-only artifacts regenerated from the DB** — so they can hold sensitive
context for Oliver, and CI (which has no DB) no longer builds the site. **`main` is pure source**;
Oliver writes nothing to it. The site is built + deployed **locally** to the **`gh-pages` branch**.

- **Generator:** Eleventy 3, in `website/` (`npm run build`, `npm run serve` from the repo root).
- **Deploy:** `python -m agent.publish` (or `npm run deploy`) = regen corpus from the DB → `npm run build`
  → force-push `website/_site` (built HTML + all images + `CNAME` + `.nojekyll`) as a clean orphan to
  `gh-pages`. Refuses to deploy an empty site (guards on `_site/index.html` + `_site/CNAME`). Oliver
  runs this in the background after every data write (`commands.schedule_publish`); a developer runs it
  after a template/code change. A startup `publish.ensure_corpus()` (bot `on_ready`) regenerates the
  corpus so on-disk mirrors the DB.
- **Covers:** `npm run covers` (`python -m corpus.images`) backfills missing covers from Open Library
  (OL ids read from the DB). The enrichment loop (`python -m agent.enrich`) also fetches covers + author
  portraits. All land in the gitignored `assets/images/{covers,authors}/`.
- **Input:** `website/src/` Nunjucks templates; `website/src/_data/*.js` glob `corpus/data/`. **Private-data
  boundary:** the `_data` modules only read `books/ meetings/ members/ authors/ reviews/ awards/`, so any
  future sensitive corpus fields must live outside those subtrees (and never render into `_site`).
- **Output:** `website/_site/` → force-pushed to `gh-pages`, served by GitHub Pages.
  **One-time manual setting:** Settings → Pages → Source → *Deploy from a branch* → `gh-pages` / `(root)`.
- **Pages:** `/` (home), `/books/<slug>/`, `/members/<slug>/`, `/about/`, `/stats/`, `/feed.xml`, `/llms.txt`, `/llms-full.txt`, `/robots.txt`, `/sitemap.xml`

### Nunjucks whitespace gotcha

Loops and `{% set %}` tags emit a newline per iteration by default. When a loop's only job is to build up a variable (not render output), use `{%-` / `-%}` on every tag in the block, otherwise a 179-iteration loop dumps ~360 blank lines into the output. The `futureBooks` setup block in `website/src/llms*.txt.njk` is the canonical example.

## Data Source

**SQLite (`club_*`) is authoritative** (see top). Airtable was the original store and the seed for
the one-time `agent.script.import_airtable`; it is **no longer a live dependency** and no code reads
`AIRTABLE_*` env vars. The retired Airtable schema (table IDs, field shapes, import-time row/coverage
counts, REST/API patterns) is preserved for reference in
[`docs/archive/AIRTABLE-REFERENCE.md`](docs/archive/AIRTABLE-REFERENCE.md) — those IDs became the
integer primary keys. `DISCORD_*` + `DISCORD_BOT_TOKEN` and `ANTHROPIC_API_KEY` live in the shared
root `.env` (see `.env.example`); never commit `.env` or hard-code the bot token or API key.

## Conventions and Gotchas

### Picker (book) vs host (meeting) — distinct, usually the same

These are **two distinct relationships** (don't conflate them, despite the Airtable field
named `Host` and the old "Host = Picker" shorthand):

- **picker** = who *chose the book* — a **book-level** relationship (`club_book_pickers`,
  M:N, ordered). A book can have **multiple pickers** (e.g. a long book split first-half /
  second-half between two members). Surfaced as `book.picker[]` in the corpus.
- **host** = who *ran/hosted the meeting* (and sets its location + time) — a **meeting-level**
  relationship (`club_meeting_hosts`, M:N, ordered). Surfaced as `meeting.host[]`.

**Default:** a meeting's host is the picker of the book discussed. But they can diverge — one
host can run a meeting that discusses **two books with two different pickers**. They agree in
all current historical data, but the model stores them independently.

### Meeting date + time are LOCAL (America/Chicago)

`club_meetings.date` is the **local** meeting date `YYYY-MM-DD` and `start_time` is the local
`HH:MM` (the club is single-timezone, Minneapolis). The original import stored Airtable's UTC
instant, which displayed the wrong day for evening meetings (6-7pm local rolls past midnight
UTC in winter) — `clubdb._migrate_club` normalized them to local. This is what an iCal feed
(`DTSTART;TZID=America/Chicago`) builds on, so members can subscribe to meeting times.

### Books-to-Meetings is many-to-many

Most books map to one meeting, but `Books.Meetings` is a multi-link. As of April 2026 the only historical case is *A Canticle for Leibowitz* and *The Devil in the White City*, which both link to the September 2012 meeting (the club discussed both that month). Render code should handle the multi case.

### Topic taxonomy

11 single-select choices:

- Brain & Psychology
- Current Events & People
- Essays & Literature
- Health & Medicine
- History & Economics
- Philosophy & Religion
- Politics & Social Sciences
- Science and Math
- Science Fiction & Fiction
- Technology
- Travel & Memoir

The four newest (Essays & Literature, Health & Medicine, Philosophy & Religion, Travel & Memoir) were added in April 2026 and are sparsely populated. They contain only books that were untopic'd at the time of the backfill. Books in the older categories may be candidates for re-categorization but have not been touched.

### Subtitle was split from Book in April 2026

Books with colons in their titles had the part before the colon kept in `Book` and the part after moved to `Subtitle`. Render code should display title prominently and subtitle smaller. One historical book (*The Florentines: From Dante to Galileo: The Transformation of Western Civilization*) had a multi-colon title and was split on the first colon, so its Subtitle still contains a colon.

### ISBN-13 prefixes

All ISBN-13s in the database are English-language editions: `978-0`, `978-1`, or `979-8`. The backfill explicitly filtered out foreign editions (Spanish, German, Japanese, Polish, Turkish, French) that came back from Open Library on the first pass. Do not assume `978-0` only.

### OL Key is the long-term metadata anchor

Every book has an Open Library Work key. Use it to:
- Refresh Synopsis, Publication Year, Page Count cheaply
- Build canonical Open Library links: `https://openlibrary.org{OL Key}`
- Fetch a stable cover via `https://covers.openlibrary.org/b/id/{cover_id}-L.jpg` (the cover_id comes from the work record)
- Walk to author records: `/works/{key}.json` → `authors[].author.key` → `/authors/{key}.json`

### Special characters in titles

Some titles contain non-ASCII characters that must round-trip cleanly: `Cræft`, `Freedom™`. Make sure URL slugs handle these (transliterate or strip, don't crash).

## Things not to do

- Don't reintroduce Airtable as a live dependency, and **don't hand-edit `corpus/data/`** — **SQLite (`club_*`) is authoritative** and the corpus is generated (regen clobbers hand edits). Change data via Oliver's write tools / the DB, then `python -m agent.corpus_gen`.
- Don't change the corpus file schema/shape (fields, file layout) without asking first — it's the contract both the website and `corpus_read.py` consume, and the generator (`agent/corpus_gen.py`) must keep reproducing it.
- Don't re-categorize books across the Topic field without per-book confirmation from Jamie.
- Don't fetch metadata from Google Books. The unauthenticated daily quota is exhausted on this network. Use Open Library.
- Don't commit `.env` or hard-code the PAT, bot token, or API key.
- Cover images are gitignored, regenerated from Open Library (via `olKey`) into `assets/images/covers/` and served from `gh-pages` — not committed, and not from Airtable attachment URLs (which expire).
