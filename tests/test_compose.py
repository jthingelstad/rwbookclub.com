"""oliver.compose() voices facts in Oliver's register, and degrades to a template."""
from agent import oliver


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


def test_compose_returns_model_text(monkeypatch):
    captured = {}

    class _Client:
        class messages:
            @staticmethod
            def create(**kwargs):
                captured.update(kwargs)
                return _Resp("Next up: Stiff on the 28th — see you there.")

    monkeypatch.setattr(oliver, "_get_client", lambda: _Client())
    out = oliver.compose("meeting reminder",
                         {"book": "Stiff", "date": "2026-07-28"},
                         fallback="TEMPLATE")
    assert out == "Next up: Stiff on the 28th — see you there."
    # No tools and the charter-rich system prompt are used; facts reach the prompt.
    assert "tools" not in captured
    assert "Stiff" in captured["messages"][0]["content"]
    assert captured["system"][0]["text"].startswith("# WHO YOU ARE")


def test_compose_email_medium_asks_for_greeting_and_signoff(monkeypatch):
    captured = {}

    class _Client:
        class messages:
            @staticmethod
            def create(**kwargs):
                captured.update(kwargs)
                return _Resp("Hi Tom, can you make the meeting? — Oliver")

    monkeypatch.setattr(oliver, "_get_client", lambda: _Client())
    oliver.compose("roll-call email", {"recipient name": "Tom"},
                   fallback="TEMPLATE", medium="email")
    prompt = captured["messages"][0]["content"]
    assert "sign off" in prompt.lower()
    assert "email" in prompt.lower()


def test_compose_falls_back_on_error(monkeypatch):
    class _Client:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("api down")

    monkeypatch.setattr(oliver, "_get_client", lambda: _Client())
    out = oliver.compose("meeting reminder", {"book": "Stiff"}, fallback="TEMPLATE")
    assert out == "TEMPLATE"


def test_compose_falls_back_on_empty_completion(monkeypatch):
    class _Client:
        class messages:
            @staticmethod
            def create(**kwargs):
                return _Resp("   ")

    monkeypatch.setattr(oliver, "_get_client", lambda: _Client())
    out = oliver.compose("review nudge", {"book": "Stiff"}, fallback="TEMPLATE")
    assert out == "TEMPLATE"
