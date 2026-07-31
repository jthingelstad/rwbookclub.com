"""The agent loop in oliver.answer() must always end with a composed reply.

These guard the round-cap termination path: when the loop hits MAX_TOOL_ROUNDS
while the model still wants tools, its pending tool_use blocks are unsatisfied,
so the loop must satisfy them and then force a tools-omitted final call — never
fall through to the generic "I'm not sure how to answer that one." fallback
(the regression fixed in the eval-tuning pass).
"""

from agent import oliver

FALLBACK = "I'm not sure how to answer that one."


class _Usage:
    input_tokens = output_tokens = 1
    cache_read_input_tokens = cache_creation_input_tokens = 0


class _Text:
    type = "text"

    def __init__(self, text):
        self.text = text


class _ToolUse:
    type = "tool_use"

    def __init__(self, tid="t1", name="find_books", tool_input=None):
        self.id = tid
        self.name = name
        self.input = tool_input or {}


class _Other:
    """A non-text, non-tool block (e.g. a thinking block) — _text_of ignores it."""

    def __init__(self, type_="thinking"):
        self.type = type_


class _Resp:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content
        self.usage = _Usage()


class _ScriptedClient:
    """Returns scripted responses in order; records each call's kwargs."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

        outer = self

        class _Messages:
            @staticmethod
            def create(**kwargs):
                outer.calls.append(kwargs)
                return outer._responses.pop(0)

        self.messages = _Messages()


def _isolate(monkeypatch, client, dispatched):
    """Patch out everything in answer() except the loop: client, dispatch, and
    the DB/corpus-touching prompt builders."""
    monkeypatch.setattr(oliver, "_get_client", lambda: client)
    # Echo the medium so tests can assert it's threaded through to every create() call.
    monkeypatch.setattr(
        oliver,
        "_system_blocks",
        lambda medium="discord": [{"type": "text", "text": f"SYS:{medium}"}],
    )
    monkeypatch.setattr(oliver, "_question_block", lambda *a, **k: "Q")
    monkeypatch.setattr(oliver, "_resolve_member", lambda *a, **k: None)
    monkeypatch.setattr(
        oliver, "dispatch", lambda name, tool_input, ctx: dispatched.append(name) or '{"ok": true}'
    )


def test_round_cap_forces_a_final_text_answer(monkeypatch):
    """Model wants a tool on every turn → hit MAX_TOOL_ROUNDS → forced final reply."""
    # One tool_use response per loop iteration up to the cap, then a text response
    # for the tools-omitted forced call.
    script = [_Resp("tool_use", [_ToolUse()]) for _ in range(oliver.MAX_TOOL_ROUNDS)]
    script.append(_Resp("end_turn", [_Text("Here's the best I can do with what I found.")]))
    client = _ScriptedClient(script)
    dispatched = []
    _isolate(monkeypatch, client, dispatched)

    reply = oliver.answer("who picked our worst book?", use_history=False, persist=False)

    assert reply == "Here's the best I can do with what I found."
    assert reply != FALLBACK
    # One create per round (MAX_TOOL_ROUNDS) plus the forced final call.
    assert len(client.calls) == oliver.MAX_TOOL_ROUNDS + 1
    # The final call must omit tools so the model can only answer in text.
    assert "tools" not in client.calls[-1]
    # Every prior in-loop call offered tools.
    assert all("tools" in c for c in client.calls[:-1])
    # All the in-loop tool calls (incl. the pending ones at the cap) were dispatched.
    assert len(dispatched) == oliver.MAX_TOOL_ROUNDS


def test_natural_stop_with_no_text_is_nudged(monkeypatch):
    """Model stops (end_turn) with no text after a tool round → nudge yields a reply."""
    script = [
        _Resp("tool_use", [_ToolUse()]),  # round 1: dispatch, continue
        _Resp("end_turn", [_Other("thinking")]),  # round 2: stops, but no text block
        _Resp("end_turn", [_Text("Right — here's the answer.")]),  # nudge reply
    ]
    client = _ScriptedClient(script)
    dispatched = []
    _isolate(monkeypatch, client, dispatched)

    reply = oliver.answer("anything by Gladwell?", use_history=False, persist=False)

    assert reply == "Right — here's the answer."
    assert reply != FALLBACK
    assert len(client.calls) == 3


def test_normal_answer_returns_in_one_call(monkeypatch):
    """Happy path: the model answers in text immediately, no tool loop."""
    client = _ScriptedClient([_Resp("end_turn", [_Text("Tuesday the 30th — see you there.")])])
    dispatched = []
    _isolate(monkeypatch, client, dispatched)

    reply = oliver.answer("when's the meeting?", use_history=False, persist=False)

    assert reply == "Tuesday the 30th — see you there."
    assert len(client.calls) == 1
    assert dispatched == []


def test_targeted_shared_pick_reception_forces_public_fit_first(monkeypatch):
    client = _ScriptedClient(
        [
            _Resp(
                "tool_use",
                [_ToolUse(name="pick_fit", tool_input={"title": "The Power Broker"})],
            )
        ]
    )
    dispatched = []
    _isolate(monkeypatch, client, dispatched)
    monkeypatch.setattr(
        oliver,
        "dispatch",
        lambda name, tool_input, ctx: (
            dispatched.append(name)
            or '{"candidate":{"pages":1263},"lengthPrecedents":['
            '{"title":"Team of Rivals","pages":1308,"ratingAverage":5,'
            '"discussionAverage":5}],"silentPrivateCalibration":{"signals":["secret"]}}'
        ),
    )

    reply = oliver.answer(
        "The Power Broker is enormous. Who here would resist it as a pick?",
        channel_id="shared-discord",
        use_history=False,
        persist=False,
    )

    assert reply == (
        "*Team of Rivals* was 1,308 pages and scored 5/5 overall with 5/5 discussion. "
        "At 1,263 pages, the question is whether we want that much reading runway; that's worth "
        "checking with everyone before committing."
    )
    assert "secret" not in reply
    assert dispatched == ["pick_fit"]
    assert len(client.calls) == 1
    assert client.calls[0]["tool_choice"] == {
        "type": "tool",
        "name": "pick_fit",
        "disable_parallel_tool_use": True,
    }
    assert client.calls[0]["thinking"] == {"type": "disabled"}
    assert "output_config" not in client.calls[0]


def test_targeted_private_pick_reception_keeps_normal_tool_choice(monkeypatch):
    client = _ScriptedClient([_Resp("end_turn", [_Text("I think you'd enjoy that one.")])])
    _isolate(monkeypatch, client, [])

    reply = oliver.answer(
        "Would I resist The Power Broker as a pick?",
        channel_id="email:direct-thread",
        use_history=False,
        persist=False,
    )

    assert reply == "I think you'd enjoy that one."
    assert "tool_choice" not in client.calls[0]
    assert client.calls[0]["thinking"] == {"type": "adaptive"}


def test_non_length_shared_reception_keeps_normal_tool_choice(monkeypatch):
    client = _ScriptedClient([_Resp("end_turn", [_Text("Ask the club about the political lane.")])])
    _isolate(monkeypatch, client, [])

    reply = oliver.answer(
        "Who would object to this book's politics as a pick?",
        channel_id="shared-discord",
        use_history=False,
        persist=False,
    )

    assert reply == "Ask the club about the political lane."
    assert "tool_choice" not in client.calls[0]


def test_medium_threads_to_every_create_call(monkeypatch):
    """The `medium` must reach _system_blocks on every model call in the loop — including the
    forced-final and nudge retries — so email replies never fall back to the Discord voice."""
    # tool_use → cap-forced final; exercises two create() calls.
    script = [_Resp("tool_use", [_ToolUse()]) for _ in range(oliver.MAX_TOOL_ROUNDS)]
    script.append(_Resp("end_turn", [_Text("done")]))
    client = _ScriptedClient(script)
    _isolate(monkeypatch, client, [])

    oliver.answer("q", use_history=False, persist=False, medium="email")

    systems = [c["system"][0]["text"] for c in client.calls]
    assert systems and all(
        s == "SYS:email" for s in systems
    )  # email on every call, incl. forced final


def test_medium_defaults_to_discord(monkeypatch):
    client = _ScriptedClient([_Resp("end_turn", [_Text("hi")])])
    _isolate(monkeypatch, client, [])
    oliver.answer("q", use_history=False, persist=False)
    assert client.calls[0]["system"][0]["text"] == "SYS:discord"
