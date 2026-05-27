# rwbookclub.com

A monorepo for the **R/W Book Club**, a group of technically minded readers who have been meeting in the Minneapolis-Saint Paul area since April 2003. The "R/W" stands for *Read / Write* — the members read, but they also write.

The club reads about eight books per year, mostly non-fiction (~88%), and members rotate picking the next book and hosting the discussion. This repo holds everything the club runs on — the public website, the shared knowledge corpus, and Oliver, the club's Discord agent.

## Layout

```
rwbookclub.com/
├── website/   # Eleventy 3 static site (rwbookclub.com) — consumes the corpus
├── corpus/    # Python: Airtable client + fetch pipeline + canonical data
└── agent/     # Python: Oliver, the Discord agent — consumes the corpus
```

The three pieces share one repo and one root `.env`. The **corpus** is the shared
knowledge layer (Airtable is the upstream source of truth); both the website and
Oliver read from it.

## Quick start

```bash
npm install                 # installs the website workspace
npm run build               # build the site → website/_site
npm run serve               # local dev server
npm run fetch               # refresh the corpus from Airtable (Python)

pip install -r corpus/requirements.txt -r agent/requirements.txt
python -m agent.bot         # run Oliver (needs Discord + Anthropic keys in .env)
```

`npm run fetch` runs `python -m corpus.fetch && python -m corpus.images`; all
Python commands run from the repo root. Copy `.env.example` to `.env` and fill in
the secrets first.

## What's on the site

- **Reading journey** — the full chronological history, newest first, with a featured "currently reading" section
- **All books** — a cover grid of every book, linking to individual detail pages with metadata, synopses, and member reviews
- **Statistics** — charts and numbers: books per year, topic distribution, publication decades, fiction/non-fiction split, picker leaderboard, and superlatives
- **Member pages** — each current member's picks (as a cover grid), reviews, and to-review list
- **LLM context files** — [`/llms.txt`](https://rwbookclub.com/llms.txt) and [`/llms-full.txt`](https://rwbookclub.com/llms-full.txt) for pasting into AI chatbots
- **RSS feed** — the 20 most recent books

## How it's built

The canonical data lives in an **Airtable base** (books, meetings, members, authors, reviews, awards). The `corpus/` Python pipeline fetches every table, denormalizes the records into JSON under `corpus/data/`, and downloads and resizes cover art into the website's asset tree. **Eleventy** (in `website/`) renders the static site from Nunjucks templates, reading the corpus JSON. The JSON data and images are committed so everyday template edits deploy fast without touching Airtable.

Two GitHub Actions workflows deploy the website to **GitHub Pages** (the artifact is `website/_site`):

- **`deploy.yml`** — runs on every push to `main`. Pure build, no Python.
- **`refresh.yml`** — manual trigger. Pulls fresh data from Airtable (`python -m corpus.fetch`/`images`), commits the diff, then builds and deploys.

**Oliver** (`agent/`) is a separate long-running process — a discord.py bot that answers questions in the club's `#ask-oliver` channel via Claude, using the corpus as context. It runs on its own host, not in GitHub Actions. See [`agent/README.md`](agent/README.md).

See [`CLAUDE.md`](CLAUDE.md) for the full Airtable schema, data conventions, and API patterns, and [`corpus/README.md`](corpus/README.md) for the data layer.
