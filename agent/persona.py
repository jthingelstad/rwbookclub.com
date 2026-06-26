"""Oliver's charter — his definitional identity, loaded from agent/docs/.

SOUL/PURPOSE/PROCESS are the source of truth for who Oliver is, why he exists,
and how he runs the club's workflows. They are read once at import and assembled
into CHARTER, which leads Oliver's system prompt (see agent/oliver.py) — so
editing those files changes Oliver. The thin operating scaffolding the charter
doesn't cover (tool strategy, answer shapes, formatting) stays in oliver.py as
OPERATIONAL_PROMPT.

If a charter file is missing this raises at import: Oliver must never start
voiceless rather than silently shedding his identity.
"""

from __future__ import annotations

from pathlib import Path

_DOCS = Path(__file__).resolve().parent / "docs"

# (prompt heading, filename) in the order they lead the system prompt.
_CHARTER_FILES = [
    ("WHO YOU ARE", "SOUL.md"),
    ("WHY YOU EXIST", "PURPOSE.md"),
    ("HOW YOU OPERATE", "PROCESS.md"),
]


def _body(text: str) -> str:
    """Drop the file's own leading `# Title` line; we supply our own heading."""
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _load() -> str:
    parts = []
    for heading, name in _CHARTER_FILES:
        # read_text raises FileNotFoundError if the charter file is gone — intended.
        text = (_DOCS / name).read_text(encoding="utf-8")
        parts.append(f"# {heading}\n\n{_body(text)}")
    return "\n\n".join(parts)


CHARTER = _load()
