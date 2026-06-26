# rwbookclub.com

A monorepo for the **R/W Book Club**, a group of technically minded readers who have been meeting in the Minneapolis-Saint Paul area since April 2003. The "R/W" stands for *Read / Write* — the members read, but they also write.

The club reads about eight books per year, mostly non-fiction (~88%), and members rotate picking the next book and hosting the discussion. This repo holds everything the club runs on — the public website, the shared knowledge corpus, and Oliver, the club's Discord agent.

## Layout

```
rwbookclub.com/
├── website/   # Eleventy 3 static site (rwbookclub.com) — consumes the corpus
├── corpus/    # Python: canonical club data (text files) + tooling
└── agent/     # Python: Oliver, the Discord agent — consumes the corpus
```

The three pieces share one repo and one root `.env`. The **corpus** is the shared
knowledge layer — **Git is the source of truth** (per-entity text files); Airtable
is a read-only cold backup. Both the website and Oliver read from the corpus.

## Quick start

```bash
npm install                 # installs the website workspace
npm run build               # build the site → website/_site
npm run serve               # local dev server
npm run covers              # backfill missing book covers from Open Library

pip install -r corpus/requirements.txt -r agent/requirements.txt
python -m agent.bot         # run Oliver (needs Discord + Anthropic keys in .env)
```

Club data is edited directly as text files in `corpus/data/` and committed; a push
to `main` rebuilds and deploys the site. All Python commands run from the repo root.
Copy `.env.example` to `.env` and fill in the secrets first.

## What's on the site

- **Reading journey** — the full chronological history, newest first, with a featured "currently reading" section
- **All books** — a cover grid of every book, linking to individual detail pages with metadata, synopses, and member reviews
- **Statistics** — charts and numbers: books per year, topic distribution, publication decades, fiction/non-fiction split, picker leaderboard, and superlatives
- **Member pages** — each current member's picks (as a cover grid), reviews, and to-review list
- **LLM context files** — [`/llms.txt`](https://rwbookclub.com/llms.txt) and [`/llms-full.txt`](https://rwbookclub.com/llms-full.txt) for pasting into AI chatbots
- **RSS feed** — the 20 most recent books

## How it's built

The club's data lives in **SQLite** (`agent/oliver.db`, the `club_*` tables — the source of truth). From it, `agent/corpus_gen.py` regenerates the **corpus**: per-entity text files in `corpus/data/` (`books/`, `members/`, `meetings/`, `authors/`, `reviews/`, `awards/`) — JSON records, Markdown reviews. The corpus and the machine-generated cover/portrait images are **gitignored, on-disk-only** (private, so they can hold sensitive context for Oliver). **Eleventy** (in `website/`) globs the corpus at build time.

**Build + deploy are local** (CI has no DB, so it can't build the real site): `python -m agent.publish` (`npm run deploy`) regenerates the corpus, builds, and force-pushes `website/_site` to the **`gh-pages` branch**, which GitHub Pages serves. Oliver runs it automatically after data writes; developers run it after template changes. `main` is pure source — Oliver never commits to it.

**Oliver** (`agent/`) is a separate long-running process — a discord.py bot that answers questions in the club's `#ask-oliver` channel via Claude, using the corpus as context. It runs on its own host, not in GitHub Actions. See [`agent/README.md`](agent/README.md).

See [`CLAUDE.md`](CLAUDE.md) for the data schema and conventions, [`corpus/README.md`](corpus/README.md) for the data layer, and [`agent/ROADMAP.md`](agent/ROADMAP.md) for where Oliver is headed.
