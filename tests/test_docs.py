"""Repository documentation stays navigable as files are consolidated or archived."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")


def _markdown_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*.md")
        if not ({".git", ".venv", "venv", "node_modules", "_site"} & set(path.parts))
    ]


def test_repository_relative_markdown_links_resolve():
    broken: list[str] = []
    for document in _markdown_files():
        for match in LINK.finditer(document.read_text()):
            raw = match.group(1).strip().strip("<>")
            target = raw.split(maxsplit=1)[0]
            if target.startswith(("#", "/", "http://", "https://", "mailto:")):
                continue
            relative = unquote(target.split("#", 1)[0])
            if relative and not (document.parent / relative).exists():
                broken.append(f"{document.relative_to(ROOT)} -> {target}")
    assert not broken, "broken Markdown links:\n" + "\n".join(sorted(broken))
