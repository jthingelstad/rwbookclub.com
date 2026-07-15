"""Characterize the schema/registry/gate seams of capability-scoped tool dispatch."""

import hashlib
import inspect
import json

from agent import model_readers
from agent.tool_handlers import mail, meeting, memory, picking
from agent.tool_handlers.context import RequestContext
from agent.tools import TOOL_HANDLERS, TOOLS, dispatch


SCHEMA_SHA256 = "6d6598ea539da0836f5ba0a7251f3cb5f9932d96c6a2e24bf6b9805cad694a59"


def test_tool_schema_contract_is_unchanged():
    encoded = json.dumps(
        TOOLS, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    assert hashlib.sha256(encoded).hexdigest() == SCHEMA_SHA256


def test_registry_has_exactly_one_handler_for_every_client_tool():
    expected = {
        definition["name"] for definition in TOOLS
        if definition.get("type") != "web_search_20250305"
    }
    assert set(TOOL_HANDLERS) == expected
    capability_names = [meeting.NAMES, memory.NAMES, mail.NAMES, picking.NAMES]
    assert sum(len(names) for names in capability_names) == len(set().union(*capability_names))


def test_dispatch_passes_typed_trusted_context(monkeypatch):
    seen = {}

    def handler(name, tool_input, request):
        seen.update(name=name, tool_input=tool_input, request=request)
        return {"member": request.member_slug, "admin": request.actor.is_admin}

    monkeypatch.setitem(TOOL_HANDLERS, "find_books", handler)
    result = json.loads(dispatch(
        "find_books",
        {"query": "history", "member": "model-cannot-set-identity"},
        {"member_slug": "jamie", "speaker_user_id": "trusted-runtime-id"},
    ))
    assert isinstance(seen["request"], RequestContext)
    assert result == {"member": "jamie", "admin": False}


def test_authorization_gate_runs_before_capability_handler(monkeypatch):
    def must_not_run(*_args):
        raise AssertionError("unauthorized request reached handler")

    monkeypatch.setitem(TOOL_HANDLERS, "recall", must_not_run)
    result = json.loads(dispatch("recall", {}, {"speaker_user_id": "unlinked"}))
    assert result == {"error": "this tool requires a linked club-member identity"}


def test_model_reader_apis_require_an_actor_keyword():
    for name in (
        "recent_channel", "search_discussion", "search_mail", "mail_thread", "memories",
        "book_cloud_titles", "recent_book_cloud",
    ):
        signature = inspect.signature(getattr(model_readers, name))
        actor = signature.parameters["actor"]
        assert actor.kind is inspect.Parameter.KEYWORD_ONLY
        assert actor.default is inspect.Parameter.empty
