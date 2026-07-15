"""Oliver's tool surface: Anthropic tool schemas + a dispatcher.

Two kinds of tools live here:
- **Server-side** (Anthropic-hosted): `web_search` — the platform resolves it,
  `dispatch()` never sees it because the response blocks have type
  `server_tool_use`, not `tool_use`.
- **Client-side**: stable schemas and the single authorization/registry gate live here;
  capability implementations live in `tool_handlers/` and actor-scoped private readers in
  `model_readers.py`. `dispatch` is called by the agent loop with the tool
  name, model's input, and per-turn context; returns a compact JSON string.

Tool errors are caught and returned to the model as `{"error": ...}` so the
loop survives — but also `log.exception` so the operator can see what broke.
"""

from __future__ import annotations

import json
import logging

from agent import access
from agent.tool_catalog import TOOLS
from agent.tool_handlers import core, mail, meeting, memory, picking
from agent.tool_handlers.context import RequestContext

log = logging.getLogger("oliver.tools")


def _build_registry():
    registry = {}
    for capability in (core, meeting, memory, mail, picking):
        overlap = set(registry) & capability.NAMES
        if overlap:
            raise RuntimeError(f"duplicate tool handler registration: {sorted(overlap)}")
        registry.update({name: capability.handle for name in capability.NAMES})
    expected = {
        definition["name"] for definition in TOOLS
        if definition.get("type") != "web_search_20250305"
    }
    if set(registry) != expected:
        raise RuntimeError(
            f"tool registry/schema mismatch: missing={sorted(expected - set(registry))}, "
            f"extra={sorted(set(registry) - expected)}"
        )
    return registry


# The one auditable client-tool registry. Authorization remains centralized in dispatch below.
TOOL_HANDLERS = _build_registry()


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def dispatch(name: str, tool_input: dict, ctx: dict) -> str:
    """Authorize and dispatch one tool with identity resolved only from trusted runtime context."""
    try:
        actor = access.actor_from_ctx(ctx)
        if error := access.tool_access_error(name, actor):
            log.warning(
                "tool access denied: tool=%s member=%s admin=%s",
                name, actor.member_slug, actor.is_admin,
            )
            return _dump({"error": error})
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return _dump({"error": f"unknown tool {name}"})
        request = RequestContext.from_runtime(ctx, actor=actor)
        return _dump(handler(name, tool_input, request))
    except Exception as exc:
        log.exception("tool %s failed (input=%r)", name, tool_input)
        return _dump({"error": f"{type(exc).__name__}: {exc}"})
