# CLAUDE.md — rwbookclub.com

Project-specific context for Claude Code. The site is live at rwbookclub.com (served by GitHub Pages from the **`gh-pages` branch**, built + deployed **locally** by Oliver — not from `main`/CI). As of May 2026 there are 179 books, 184 meetings, 177 authors, and 12 members (5 current).

## Project

rwbookclub.com is the home of the R/W Book Club, which has been meeting since April 2003. The club reads about 8 books per year, mostly non-fiction (about 88%), and rotates picking and hosting among members.

**SQLite is the source of truth** for club data — the `club_*` tables in `agent/oliver.db` hold the authoritative club record (books/meetings/members/authors/reviews/awards) under integer primary keys and real foreign keys. The Git corpus (`corpus/data/`) **and** the website are **generated** from it (`python -m agent.corpus_gen`). **Do not hand-edit `corpus/data/` — a regen will clobber it.** Change the data through Oliver's write tools / the DB. Airtable was the original home and was retired after a one-time import (`python -m agent.script.import_airtable`). See `docs/archive/MIGRATION-PLAN.md` and `docs/archive/MIGRATION-STATUS.md` for the full inversion history. (This inversion happened 2026-06-26; sections below that still say "Git is canonical / edit corpus directly" predate it and are kept for the Airtable schema reference only.)

## Monorepo layout

This repo is a flat, polyglot monorepo with three top-level concerns:

- **`website/`** — the Eleventy 3 static site (Node). Consumes the corpus.
- **`corpus/`** — the **generated** knowledge layer (Python). Per-entity text files in `corpus/data/` (`books/`, `members/`, `meetings/`, `authors/`, `reviews/`, `awards/`) are produced from the `club_*` SQLite tables by `agent/corpus_gen.py`, reproducing the normalized on-disk shape (`corpus/normalize.py`): each fact once, relationships by **slug** (slug = filename stem, an output detail — never identity in the DB), derived fields computed at build/read time. Records are JSON, reviews Markdown+frontmatter. `corpus/validate.py` checks reference integrity. `corpus/images.py` backfills covers from Open Library (reads OL ids from the DB). (The legacy Airtable→corpus modules `fetch.py`/`migrate.py`/`normalize.py` were **removed**; the one-time re-seed path is now `agent.script.import_airtable` → DB → `agent.corpus_gen`.) **Oliver now owns this**: writes land in the DB, then the corpus is regenerated on disk (it is **gitignored/private**, never committed) and the site is rebuilt + deployed locally to `gh-pages` (see "Site build + deploy").
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

## Data Source (Airtable — retired; historical reference)

SQLite (`club_*`) is now authoritative (see top). Airtable was the original store and the seed for the one-time `agent.script.import_airtable`; it is **no longer a live dependency**. The schema below is kept to explain the field shapes the import read and the IDs (`Book ID`/`Meeting ID`/`Member ID`/`Author ID`/`Review ID`) that became the integer primary keys.

- **Airtable base ID:** `appmiF5yLSzx0klJc`
- **Credentials:** the shared root `.env` holds `AIRTABLE_BASE_ID` and `AIRTABLE_PAT` (corpus), the `DISCORD_*` identifiers + `DISCORD_BOT_TOKEN` and `ANTHROPIC_API_KEY` (agent). See `.env.example` for the full list. Never commit `.env`. Never hard-code the PAT, bot token, or API key.
- **REST API base:** `https://api.airtable.com/v0/{baseId}/{tableId}`
- **Pagination:** records endpoints return up to 100 per call; follow `offset` until empty
- **Batch writes:** PATCH/POST `/v0/{baseId}/{tableId}` accepts up to 10 records per call
- **PAT capabilities:** can read, write records, and create new fields and tables. **Cannot** PATCH single-select option lists on existing fields (returns 422). Workaround: send record writes with `"typecast": true` and Airtable will auto-create the new option.

## Schema

Six tables. IDs are stable; names can change.

### Books — `tblPqH96wIgGuUSXe` (176 rows)

Primary field is `Book` (title only; subtitle is separate).

| Field | Type | Notes |
|---|---|---|
| Book | text | Title; primary field |
| Subtitle | text | Part after the first colon. Empty for books with no subtitle. 119 of 176 have one. |
| Cover | attachment | 175 of 176. *The Devil in the White City* is the only one missing. |
| Authors | link → Authors | Multi |
| Meetings | link → Meetings | Multi. See "Books-to-Meetings is many-to-many" below. |
| Fiction | checkbox | 21 fiction, 155 non-fiction |
| Topic | single-select | 11 categories. All books assigned. See "Topic taxonomy". |
| ISBN-13 | text | 170 of 176. All are English-language editions (`978-0`, `978-1`, or `979-8` prefixes). 6 have no clean English edition on Open Library. |
| Synopsis | long text | 138 of 176. Sourced from Open Library. |
| Publication Year | number | 176 of 176. Year of first publication. |
| Page Count | number | 171 of 176. Median across editions per Open Library. |
| OL Key | text | Open Library Work key, e.g. `/works/OL17075811W`. Present for all 176. Use to refresh metadata or build links. |
| Date Read | rollup → Meetings.Meeting Date | |
| Year Read | formula | `YEAR({Date Read})` |
| Picked by | link → Members | Multi. The member(s) who picked the book. Empty for rare "group picks". Multiple pickers means the book spanned two meetings. Replaces the old approach of traversing Meetings.Host. |
| Review Count | count → Reviews | |
| Book ID | autonumber | Stable ID |

### Meetings — `tblJpQrukeCXaO0Uq` (181 rows)

Primary field is `Name` (formula: `MMMM YYYY: <Book>`).

| Field | Type | Notes |
|---|---|---|
| Name | formula | Auto-generated label |
| Meeting Date | dateTime | Range: 2003-04 → 2025-11 |
| Book | link → Books | Multi (rare). The Sept 2012 meeting links to two books. |
| Host | link → Members | **This is the book picker.** See "Host = Picker". |
| Meeting Type | multi-select | Choices: Book, Spouses, Videos, Essay, Movie, Picking |
| Location | text | Sparse: 41 of 181 |
| Notes | long text | Sparse: 7 of 181 |
| Placeholder | checkbox | True if the date is approximate |
| Year | formula | `YEAR({Meeting Date})` |
| Meeting ID | autonumber | |

### Members — `tblsjVRbdj231zbwj` (12 rows)

Primary field is `Name`. 5 current, 7 former. No bio field, no joined/left dates (deferred).

| Field | Type | Notes |
|---|---|---|
| Name | text | |
| Photo | attachment | 10 of 12 |
| Current Member | checkbox | 5 true |
| Email Address | email | 6 of 12 |
| Mobile | phone | |
| Website | url | 3 of 12 |
| Picked Count | count → Meetings (Host) | Despite the name, this is `count(meetings where I am Host)`. The Host field is the picker. |
| Meetings | link → Meetings | |
| Reviews | link → Reviews | |

### Authors — `tblLkEUVXxLMynFtn` (175 rows)

Primary field is `Author`.

| Field | Type | Notes |
|---|---|---|
| Author | text | |
| Books | link → Books | |
| Bio | long text | 79 of 175. Sourced from Open Library, attribution cruft stripped. |
| Book Count | count → Books | |
| Last Read | rollup → Books.Date Read | |
| Author ID | autonumber | |

### Reviews — `tblxZR21gPDPYBfA1` (effectively empty as of launch)

Primary field is `Review Key` (formula: `<Member>|<Book>`).

Will be populated by member submissions after the site launches. Schema is ready.

| Field | Type | Notes |
|---|---|---|
| Review Key | formula | Composite key |
| Member | link → Members | |
| Book | link → Books | |
| Rating | rating 1-5 | Book quality |
| Review | long text | |
| DNF | checkbox | Did Not Finish |
| Discussion Quality | rating 1-5 | Quality of the book club discussion this book generated. Distinct from book rating. |
| Would Recommend | checkbox | Independent of rating |
| Favorite Quote | long text | |
| Created at | createdTime | |
| Review ID | autonumber | |

### Awards — `tblrIaGgMtA08xyJE` (empty)

Primary field is `Award Name` (free-form text). For tracking annual awards. Empty until used.

| Field | Type | Notes |
|---|---|---|
| Award Name | text | E.g., "2024 Book of the Year" |
| Year | number | |
| Award | single-select | Book of the Year, Runner-up, Honorable Mention, Worst Book, Most Discussed, Most Surprising |
| Book | link → Books | Currently allows multi-link. To switch to single, open the field in the Airtable UI and toggle "Limit selection to a single record". The API blocked this on creation. |
| Notes | long text | E.g., voting margin |
| Voted By | link → Members | Optional |

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

### Single-select choices cannot be modified via the meta API

`PATCH /meta/bases/{baseId}/tables/{tableId}/fields/{fieldId}` returns 422 when modifying choices on an existing single-select. To add a new choice, send a record write with `"typecast": true` and Airtable auto-creates it.

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

### Airtable attachment URLs expire

Cover URLs in the `Cover` field are signed and rotate. For a static site that's fine if you re-fetch at build time. For a long-lived cache, mirror the bytes locally or use Open Library cover URLs.

### Special characters in titles

Some titles contain non-ASCII characters that must round-trip cleanly: `Cræft`, `Freedom™`. Make sure URL slugs handle these (transliterate or strip, don't crash).

## Coverage cheat sheet

| Field | Populated |
|---|---|
| Books.Cover | 175/176 |
| Books.ISBN-13 | 170/176 |
| Books.Topic | 176/176 |
| Books.Synopsis | 138/176 |
| Books.Publication Year | 176/176 |
| Books.Page Count | 171/176 |
| Books.OL Key | 176/176 |
| Books.Subtitle | 119 (others have no subtitle) |
| Books.Authors link | 175/176 (Devil in the White City) |
| Authors.Bio | 79/175 |
| Members.Photo | 10/12 |
| Meetings.Host | 170/181 |
| Meetings.Location | 41/181 |
| Meetings.Notes | 7/181 |
| Reviews.* | 2 records total (will grow after launch) |
| Awards.* | 0 records (empty until used) |

## API patterns

### List all records (paginated)

```python
def list_all(table_id):
    records, offset = [], None
    while True:
        url = f"https://api.airtable.com/v0/{BASE}/{table_id}?pageSize=100"
        if offset:
            url += f"&offset={offset}"
        d = requests.get(url, headers={"Authorization": f"Bearer {PAT}"}).json()
        records.extend(d["records"])
        offset = d.get("offset")
        if not offset:
            break
    return records
```

### Common filterByFormula examples

```
{Year Read}=2024
AND({Topic}="Technology", {Fiction}=FALSE())
NOT({ISBN-13}="")
{Current Member}=TRUE()
```

### Batch PATCH with typecast (for new single-select options)

```python
body = {
    "records": [{"id": rec_id, "fields": {"Topic": "New Category"}}],
    "typecast": True,
}
requests.patch(f"{base_url}/{books_table}", json=body, headers=auth)
```

## Open follow-ups (not blocking site work)

1. ~~*The Devil in the White City* has no cover.~~ Resolved — covers are complete for all 179 books.
2. Awards table Book field allows multi-link; toggle to single in the Airtable UI.
3. 88 authors matched to Open Library but have no bio there. Wikipedia fallback would help (no clean API; use the first paragraph of the article).
4. 8 authors did not match Open Library at all (Strugatsky brothers, Frederick P. Brooks Jr., Michael J. Casey, Andrei Lankov, Bruce White, Christopher Vaughan, David Gibbons). Looser name matching would catch most.
5. 6 books have no ISBN-13: Shaping Things, The Complexity of Cooperation, The Success of Open Source, Divorce Among the Gulls, The Rise and Fall of American Growth.
6. The 4 newest Topic categories are sparsely populated. A sweep over previously-classified books would surface candidates for re-categorization. Do not do this without Jamie's approval per book.
7. Member bios and joined/left dates are deliberately not in the schema. Don't add them without asking.

## Things not to do

- Don't reintroduce Airtable as a live dependency, and **don't hand-edit `corpus/data/`** — **SQLite (`club_*`) is authoritative** and the corpus is generated (regen clobbers hand edits). Change data via Oliver's write tools / the DB, then `python -m agent.corpus_gen`.
- Don't change the corpus file schema/shape (fields, file layout) without asking first — it's the contract both the website and `corpus_read.py` consume, and the generator (`agent/corpus_gen.py`) must keep reproducing it.
- Don't re-categorize books across the Topic field without per-book confirmation from Jamie.
- Don't fetch metadata from Google Books. The unauthenticated daily quota is exhausted on this network. Use Open Library.
- Don't commit `.env` or hard-code the PAT, bot token, or API key.
- Cover images come from committed files / Open Library (via `olKey`), not Airtable attachment URLs (which expire).
