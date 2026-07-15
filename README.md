# rwbookclub.com

A monorepo for the **R/W Book Club**, a group of technically minded readers who have been meeting in the Minneapolis-Saint Paul area since April 2003. The "R/W" stands for *Read / Write* — the members read, but they also write.

The club reads about eight books per year, mostly non-fiction (~88%), and members rotate picking the next book and hosting the discussion. This repo holds everything the club runs on — the public website, the shared knowledge corpus, Oliver (the club's Discord agent), and a private members' web app that Oliver serves for self-service editing.

## Layout

```
rwbookclub.com/
├── website/   # Eleventy 3 static site (rwbookclub.com) — consumes the corpus
├── corpus/    # Python: generated private knowledge layer + tooling
└── agent/     # Python: Oliver, the Discord agent — consumes the corpus
```

The three pieces share one repo and one root `.env`. The **`club_*` SQLite tables**
(`agent/oliver.db`) are the source of truth; the **corpus** is a private, gitignored
artifact generated from them that both the website build and Oliver read.

## Quick start

```bash
python3.13 -m venv venv
venv/bin/pip install -c agent/constraints.txt -r agent/requirements.txt
npm ci                      # install the website workspace from the lockfile

venv/bin/python -m agent.corpus_gen  # regenerate the private corpus from the DB
npm run build                      # build the site → website/_site
npm run serve                      # local dev server
npm run covers                     # backfill missing covers from Open Library
npm run deploy                     # regen + build + deploy to gh-pages
venv/bin/python -m agent.bot       # run Oliver (needs keys in .env)
```

Club data lives in the `club_*` SQLite tables; Oliver's write tools edit the DB and the
corpus is regenerated from it (don't hand-edit `corpus/data/` — a regen clobbers it). The
site is built + deployed locally to the `gh-pages` branch (`npm run deploy`), not by CI. All
Python commands run from the repo root. Copy `.env.example` to `.env` and fill in the secrets first.

## What's on the site

- **Reading journey** — the full chronological history, newest first, with a featured "currently reading" section
- **All books** — a cover grid of every book, linking to individual detail pages with metadata, synopses, and member reviews
- **Statistics** — charts and numbers: books per year, topic distribution, publication decades, fiction/non-fiction split, picker leaderboard, and superlatives
- **Member pages** — each current member's picks (as a cover grid), reviews, and to-review list
- **LLM context files** — [`/llms.txt`](https://rwbookclub.com/llms.txt) and [`/llms-full.txt`](https://rwbookclub.com/llms-full.txt) for pasting into AI chatbots
- **RSS feed** — the 20 most recent books

## How it's built

The club's data lives in **SQLite** (`agent/oliver.db`, the `club_*` tables — the source of truth). From it, `agent/corpus_gen.py` regenerates the **corpus**: per-entity text files in `corpus/data/` (`books/`, `members/`, `meetings/`, `authors/`, `reviews/`, `lists/`) — JSON records, Markdown reviews. The corpus and the machine-generated cover/portrait images are **gitignored, on-disk-only** (private, so they can hold sensitive context for Oliver). **Eleventy** (in `website/`) globs the corpus at build time.

**Build + deploy are local** because the real DB and corpus are private: `python -m agent.publish` (`npm run deploy`) regenerates the corpus, builds, and force-pushes `website/_site` to the **`gh-pages` branch**, which GitHub Pages serves. CI generates a PII-free fixture corpus and performs a clean-room site build, but never deploys it. Oliver deploys automatically after data writes; developers deploy after template changes. `main` is pure source — Oliver never commits to it.

**Oliver** (`agent/`) is a separate long-running process — a discord.py bot that answers questions in the club's `#ask-oliver` channel via Claude, using the corpus as context. It runs on its own host, not in GitHub Actions. See [`agent/README.md`](agent/README.md).

**The members' web app** (`agent/webapp/`) runs *inside* Oliver's process — a small aiohttp app reached over Tailscale Funnel, authed by a Discord-minted one-time link (`/oliver webapp`). Members rate/review books, manage lists, and edit their profile there; admins edit books, meetings, hosts/pickers, and members. It writes the same DB through the same writers Oliver uses. This is where "structured, deliberate editing" lives — Discord stays for conversation, attendance, and reading status.

See [`CLAUDE.md`](CLAUDE.md) for the data schema and conventions, [`corpus/README.md`](corpus/README.md) for the data layer, and [`agent/docs/ROADMAP.md`](agent/docs/ROADMAP.md) for where Oliver is headed.
