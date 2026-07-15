"""Member + admin web app — an aiohttp server run inside the bot process, reached over Tailscale
Funnel, authed by a Discord-minted one-time token → signed session cookie. Public API used by the
bot/commands: `mint_token` (for `/oliver my-club`) and `ensure_running` (start the on-demand server).
"""

from agent.webapp.sessions import consume_token, mint_token, resolve_token
from agent.webapp.server import ensure_running

__all__ = ["mint_token", "resolve_token", "consume_token", "ensure_running"]
