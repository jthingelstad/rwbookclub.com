# Disaster recovery — rebuilding Oliver on a new Mac

The goal: from a dead machine to a running Oliver in ~30 minutes. Everything below assumes the
worst case — the old Mac is gone and all you have is GitHub + iCloud + your password manager.

## What survives a dead Mac

| Thing | Where it lives |
|---|---|
| All code, site templates, docs | GitHub `jthingelstad/rwbookclub.com` (`main`) |
| **The club record + Oliver's memory** (`oliver.db`) | iCloud Drive → `Oliver/backups/oliver-<date>.db.gz` (daily, 14 kept) |
| Secrets (`.env` values) | Your password manager (names listed in `.env.example`) |
| The public site | Already live on GitHub Pages (`gh-pages`) — it keeps serving while you rebuild |
| Corpus, covers, author images | Regenerated from the DB (`corpus_gen`, `enrich`) — nothing to restore |

## Steps

1. **Clone + Python env**
   ```bash
   git clone https://github.com/jthingelstad/rwbookclub.com.git ~/Projects/rwbookclub.com
   cd ~/Projects/rwbookclub.com
   python3.13 -m venv venv
   venv/bin/pip install -c agent/constraints.txt -r agent/requirements.txt
   npm ci
   ```
2. **Secrets** — copy `.env.example` to `.env`, fill values from the password manager
   (Discord tokens/IDs, `ANTHROPIC_API_KEY`, `FASTMAIL_JMAP_TOKEN`, `WEBAPP_SECRET`).
3. **Restore the database** (newest snapshot from iCloud):
   ```bash
   gunzip -c "$(ls -t ~/Library/Mobile\ Documents/com~apple~CloudDocs/Oliver/backups/oliver-*.db.gz | head -1)" \
     > agent/oliver.db
   venv/bin/python -c "import sqlite3; print(sqlite3.connect('agent/oliver.db').execute('PRAGMA integrity_check').fetchone())"
   ```
4. **Regenerate derived state** — corpus + covers/portraits:
   ```bash
   venv/bin/python -m agent.corpus_gen
   venv/bin/python -m agent.enrich --books --authors   # refetches missing images (network)
   ```
5. **launchd** — the plist is in the repo:
   ```bash
   cp agent/ops/com.rwbookclub.oliver.plist ~/Library/LaunchAgents/
   # Edit paths inside if the username/checkout path differs, then:
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rwbookclub.oliver.plist
   ```
   Verify: `tail -f agent/logs/oliver.log` → "Oliver connected as RWBC - Oliver".
6. **Tailscale Funnel** (member web app) — install Tailscale, sign in, then the one-time bits:
   the tailnet ACL must grant the `funnel` nodeAttr, and HTTPS certs must be enabled
   (Tailscale admin console). Then `tailscale funnel <WEBAPP_PORT>` per the value in `.env`.
   The webapp works on localhost without this; only remote member access needs Funnel.
7. **GitHub auth for publish + releases** — `gh auth login`; confirm `git push` works
   (Oliver deploys the site by pushing `gh-pages` and cuts releases via `gh`).
8. **Smoke** — in Discord: `/oliver ping`, `/oliver admin status` (expect the current release
   name), and ask Oliver something in #ask-oliver. On Monday you'll get the health digest;
   its arrival is the "all clear."

## What you lose in the worst case

At most one day of Oliver's conversational state (since the last nightly snapshot). The club
record itself (books/meetings/reviews) changes slowly, so effectively nothing.

## Ongoing safety (already automated)

- **Daily**: gzipped snapshot → iCloud (`agent/backup.py`, scheduler-driven, 14 kept).
- **Weekly**: health digest email to the admin — a MISSING Monday email is the alarm.
- **On failure**: backup/enrichment problems post warnings to #oliver-log.
