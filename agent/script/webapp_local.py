"""Run the member web app locally and mint a session link — no Discord, no Funnel.

For development and verification: starts the same aiohttp app the bot serves (auth, CSRF,
templates, writers — everything identical) as a STANDALONE process on 127.0.0.1, mints a
single-use token for a member (default: Oliver, the club's agent member), and prints the URL.
Browse it on this Mac; Ctrl-C to stop. Writes hit the real oliver.db, and the Publish button
really deploys — treat it like the production editor, because it is.

    python -m agent.script.webapp_local                  # Oliver, member view
    python -m agent.script.webapp_local --admin          # Oliver, admin view
    python -m agent.script.webapp_local --member jamie   # someone else (careful: real identity)
    python -m agent.script.webapp_local --port 8791
"""

from __future__ import annotations

import argparse
import asyncio

from aiohttp import web

from agent import clubdb, config
from agent.webapp import server, sessions


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Serve the web app locally with a fresh session link.")
    parser.add_argument("--member", default=config.OLIVER_MEMBER_SLUG,
                        help=f"member slug to log in as (default: {config.OLIVER_MEMBER_SLUG})")
    parser.add_argument("--admin", action="store_true", help="mint an admin session")
    parser.add_argument("--port", type=int, default=8791,
                        help="local port (default 8791; avoid the bot's WEBAPP_PORT)")
    args = parser.parse_args()

    # Same fail-closed rule as the bot's instance: never sign sessions with the dev literal.
    if not config.WEBAPP_SECRET or config.WEBAPP_SECRET == config.WEBAPP_DEV_SECRET:
        raise SystemExit("Set WEBAPP_SECRET (or DISCORD_BOT_TOKEN) to a real secret first.")

    member_id = clubdb.lookup_member_id(args.member)
    if member_id is None:
        raise SystemExit(f"No member with slug {args.member!r}.")

    runner = web.AppRunner(server._build_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", args.port)
    await site.start()

    token = sessions.mint_token(member_id, is_admin=args.admin)
    print(f"Serving the web app on 127.0.0.1:{args.port} as {args.member!r}"
          f"{' (admin)' if args.admin else ''} — Ctrl-C to stop.")
    print(f"\n  http://127.0.0.1:{args.port}/webapp?t={token}\n")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
