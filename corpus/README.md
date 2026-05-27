# corpus

The canonical knowledge corpus for the R/W Book Club: denormalized JSON pulled
from the Airtable base, plus the shared Airtable client. Both the website and
the Discord agent (Oliver) consume this; Airtable remains the upstream source of
truth.

## Layout

```
corpus/
├── airtable.py     # shared client: session, list_all, slugify, env + paths
├── fetch.py        # pulls all tables → corpus/data/*.json
├── images.py       # downloads + resizes covers/photos into the website tree
├── requirements.txt
└── data/           # canonical JSON (committed)
    ├── raw/{books,members}.json   # enriched at build time by website/src/_data/*.js
    ├── authors.json
    ├── reviews.json
    └── awards.json
```

## Refresh

Run from the **repo root** (so the `corpus` package resolves) with the Airtable
credentials in the root `.env`:

```bash
python -m corpus.fetch     # rewrites corpus/data/*.json
python -m corpus.images    # repopulates website/src/assets/images/{covers,members}
```

`corpus.images` writes the responsive variants (240/480/960) directly into the
website's asset tree — those resized files are website presentation, not corpus
data, so they live with the site. The book/member JSON under `data/raw/` is
deliberately left un-enriched; `website/src/_data/books.js` and `members.js`
derive `hasCover`/`coverWidths` from the files actually on disk at build time,
so fetch and image steps can run in any order without drifting out of sync.

## Schema

The Airtable schema (table IDs, fields, conventions) is documented in the
repo-root `CLAUDE.md`. The table IDs live in `airtable.py`.
