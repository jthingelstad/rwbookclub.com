# rwbookclub.com

The public website for the **R/W Book Club**, a group of technically minded readers who have been meeting in the Minneapolis-Saint Paul area since April 2003. The "R/W" stands for *Read / Write* — the members read, but they also write.

The club reads about eight books per year, mostly non-fiction (~88%), and members rotate picking the next book and hosting the discussion. This site is the running ledger of everything the club has read — 179 books and counting.

## What's on the site

- **Reading journey** — the full chronological history, newest first, with a featured "currently reading" section
- **All books** — a cover grid of every book, linking to individual detail pages with metadata, synopses, and member reviews
- **Statistics** — charts and numbers: books per year, topic distribution, publication decades, fiction/non-fiction split, picker leaderboard, and superlatives
- **Member pages** — each current member's picks (as a cover grid), reviews, and to-review list
- **LLM context files** — [`/llms.txt`](https://rwbookclub.com/llms.txt) and [`/llms-full.txt`](https://rwbookclub.com/llms-full.txt) for pasting into AI chatbots
- **RSS feed** — the 20 most recent books

## How it's built

The canonical data lives in an **Airtable base** (books, meetings, members, authors, reviews, awards). A Python pipeline fetches every table, denormalizes the records into JSON, and downloads and resizes cover art. **Eleventy** renders the static site from Nunjucks templates. The JSON data and images are committed to the repo so everyday template edits deploy fast without touching Airtable.

Two GitHub Actions workflows handle deployment to **GitHub Pages**:

- **`deploy.yml`** — runs on every push to `main`. Pure build, no Python.
- **`refresh.yml`** — manual trigger. Pulls fresh data from Airtable, commits the diff, then builds and deploys.

See [`CLAUDE.md`](CLAUDE.md) for the full Airtable schema, data conventions, and API patterns.
