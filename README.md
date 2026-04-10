# rwbookclub.com

The website for the **R/W Book Club** — meeting since April 2003. Static site
generated with [Eleventy](https://www.11ty.dev/) from data that lives in an
Airtable base, deployed to GitHub Pages.

## How it works

```
 Airtable  ──▶  scripts/fetch_airtable.py  ──▶  src/_data/*.json
                scripts/process_images.py  ──▶  src/assets/images/{covers,members}/
                                                       │
                                                       ▼
                                            Eleventy build (src → _site)
                                                       │
                                                       ▼
                                              GitHub Pages deploy
```

The Airtable base is the canonical source of truth. The Python pipeline pulls
every table, denormalizes it into JSON for Eleventy to consume, and downloads /
resizes cover art and member photos. None of the generated artifacts are
committed — the GitHub Actions workflow regenerates them on every push.

See `CLAUDE.md` for the full schema and data conventions.

## Local development

Requirements: Python 3.10+, Node 18+.

```bash
# 1. Set up a virtualenv and install Python deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Install Node deps
npm install

# 3. Make sure .env has the Airtable credentials
#    AIRTABLE_BASE_ID=...
#    AIRTABLE_PAT=...

# 4. Pull data and build
npm run fetch    # runs both Python scripts
npm run serve    # eleventy --serve, opens at http://localhost:8080
```

`npm run clean` removes generated artifacts (`_site/`, fetched JSON, processed
images) if you want a fresh build.

## Deployment

GitHub Actions does everything. Every push to `main` triggers
`.github/workflows/build.yml`, which:

1. Installs Python deps and runs `fetch_airtable.py` + `process_images.py`
2. Installs Node deps and runs `eleventy`
3. Uploads `_site/` to GitHub Pages

There's also a manual `workflow_dispatch` trigger if you edit Airtable and want
the site to catch up without pushing a commit.

### Required GitHub secrets

In **Settings → Secrets and variables → Actions**, add:

| Secret              | Value                              |
|---------------------|------------------------------------|
| `AIRTABLE_BASE_ID`  | `appmiF5yLSzx0klJc`                |
| `AIRTABLE_PAT`      | The personal access token from `.env` |

### GitHub Pages settings

In **Settings → Pages**:

- **Source:** GitHub Actions
- **Custom domain:** `rwbookclub.com` (the CNAME file in `src/` is copied to
  the site root at build time)
- **Enforce HTTPS:** on (after DNS resolves)

### DNS records to add at the registrar

For an apex domain (`rwbookclub.com`) on GitHub Pages, point the apex at
GitHub's IPs and add a `www` CNAME:

```
A     @     185.199.108.153
A     @     185.199.109.153
A     @     185.199.110.153
A     @     185.199.111.153
AAAA  @     2606:50c0:8000::153
AAAA  @     2606:50c0:8001::153
AAAA  @     2606:50c0:8002::153
AAAA  @     2606:50c0:8003::153
CNAME www   jthingelstad.github.io.
```

After DNS propagates, GitHub will provision a Let's Encrypt certificate
automatically.

## Project layout

```
.
├── .eleventy.js              # 11ty config: filters, dirs, passthroughs
├── .github/workflows/build.yml
├── package.json              # Node deps + npm scripts
├── requirements.txt          # Python deps
├── scripts/
│   ├── lib.py                # Shared: env, slugify, paginated list_all
│   ├── fetch_airtable.py     # Airtable → src/_data/*.json
│   └── process_images.py     # Resize covers + photos with Pillow
├── src/
│   ├── CNAME                 # rwbookclub.com
│   ├── _data/
│   │   ├── site.json         # Static site config (committed)
│   │   ├── journey.js        # Groups books by year for the home page
│   │   ├── currentMembers.js # Filtered to is_current for member pages
│   │   ├── books.json        # ── generated, gitignored
│   │   ├── members.json      # ──
│   │   ├── meetings.json     # ──
│   │   ├── authors.json      # ──
│   │   └── reviews.json      # ──
│   ├── _includes/
│   │   ├── layouts/base.njk  # HTML shell
│   │   └── cover.njk         # Responsive cover image macro
│   ├── assets/
│   │   ├── css/styles.css    # The whole visual layer
│   │   ├── fonts/            # Self-hosted Fraunces (variable WOFF2)
│   │   └── images/{covers,members}/  # generated, gitignored
│   ├── books/book.njk        # Paginated per-book template
│   ├── members/member.njk    # Paginated per-current-member template
│   ├── about.njk
│   └── index.njk             # The reading journey
└── _site/                    # 11ty output, gitignored
```
