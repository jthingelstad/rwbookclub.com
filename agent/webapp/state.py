"""Tiny shared state for the web app (imported by both the server and the route modules, so it
must not import either). Holds the deferred-publish 'dirty' flag: public-data writes set it, and the
site is rebuilt only on an explicit Publish or on idle shutdown."""

from __future__ import annotations

_dirty = False


def mark_dirty() -> None:
    global _dirty
    _dirty = True


def is_dirty() -> bool:
    return _dirty


def clear_dirty() -> None:
    global _dirty
    _dirty = False
