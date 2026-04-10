# rwbookclub.com

The website for the **R/W Book Club** — meeting since April 2003. Static site
generated with [Eleventy](https://www.11ty.dev/) from data that lives in an
Airtable base, deployed to GitHub Pages.

## How it works

The Airtable base is the canonical source of truth, but the JSON data and the
resized cover/photo art are **committed to this repo**. That keeps the
everyday "edit a template, push, deploy" loop fast — no Python, no Airtable
round-trip on every push.

```
 Airtable  ──▶  scripts/fetch_airtable.py  ──▶  src/_data/*.json   ─┐
                scripts/process_images.py  ──▶  src/assets/images/  ─┤
                                                                      │
                              (committed to git)                      │
                                                                      ▼
                                                       Eleventy build (src → _site)
                                                                      │
                                                                      ▼
                                                          GitHub Pages deploy
```

There are two GitHub Actions workflows:

- **`deploy.yml`** runs on every push to `main`. Pure build: `npm ci`,
  `eleventy`, deploy. ~30 s. Use this for any template / CSS / copy edit.
- **`refresh.yml`** is **manual only** (Actions → "Refresh from Airtable" →
  Run workflow). Pulls fresh data from Airtable, regenerates resized images,
  commits the diff if anything changed, then builds and deploys. Use this when
  there's a new book, a new member, or any Airtable edit.

You can also refresh locally — see "Local development" below.

See `CLAUDE.md` for the full schema and data conventions.

## Local development

Requirements: Python 3.10+, Node 18+. (CI uses Python 3.14 and Node 24.)

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

# 4. Edit and preview
npm run serve    # eleventy --serve, opens at http://localhost:8080

# 5. (Occasionally) refresh data from Airtable
npm run fetch    # runs both Python scripts; updates src/_data/ + src/assets/images/
git add src/_data src/assets/images
git commit -m "Refresh data from Airtable"
git push
```

`npm run clean` removes `_site/` and the generated data and images entirely
(use it before a `npm run fetch` if you want a fully clean rebuild).

## Deployment

GitHub Actions handles all deploys.

### Required GitHub secrets

Only the **refresh** workflow needs Airtable access. In **Settings → Secrets
and variables → Actions**, add:

| Secret              | Value                                  |
|---------------------|----------------------------------------|
| `AIRTABLE_BASE_ID`  | `appmiF5yLSzx0klJc`                    |
| `AIRTABLE_PAT`      | The personal access token from `.env`  |

The everyday `deploy.yml` workflow doesn't read these.

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
├── .eleventy.js                  # 11ty config: filters, dirs, passthroughs
├── .github/workflows/
│   ├── deploy.yml                # On push: build + deploy (no Python)
│   └── refresh.yml               # Manual: Airtable fetch + commit + deploy
├── package.json                  # Node deps + npm scripts
├── requirements.txt              # Python deps
├── scripts/
│   ├── lib.py                    # Shared: env, slugify, paginated list_all
│   ├── fetch_airtable.py         # Airtable → src/_data/*.json
│   └── process_images.py         # Resize covers + photos with Pillow
├── src/
│   ├── CNAME                     # rwbookclub.com
│   ├── _data/
│   │   ├── site.json             # Static site config
│   │   ├── journey.js            # Groups books by year for the home page
│   │   ├── currentMembers.js     # Filters to current members for pages
│   │   ├── books.json            # ── refreshed by scripts/fetch_airtable.py
│   │   ├── members.json          # ──
│   │   ├── authors.json          # ──
│   │   └── reviews.json          # ──
│   ├── _includes/
│   │   ├── layouts/base.njk      # HTML shell
│   │   └── cover.njk             # Responsive cover image include
│   ├── assets/
│   │   ├── css/styles.css        # The whole visual layer
│   │   ├── fonts/                # Self-hosted Fraunces (variable WOFF2)
│   │   └── images/{covers,members}/  # ── refreshed by process_images.py
│   ├── books/
│   │   ├── book.njk              # Paginated per-book detail page
│   │   └── index.njk             # All-books cover grid
│   ├── members/member.njk        # Paginated per-current-member page
│   ├── about.njk
│   ├── feed.njk                  # RSS feed (latest 20 picks)
│   └── index.njk                 # The reading journey
└── _site/                        # 11ty output, gitignored
```
