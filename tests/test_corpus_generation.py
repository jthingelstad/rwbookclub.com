"""Corpus writes must stay readable while the live scheduler reloads a generation."""

from __future__ import annotations

import json

from agent import corpus_gen


def test_json_writer_replaces_existing_document_atomically(tmp_path, monkeypatch):
    target = tmp_path / "books" / "example.json"
    target.parent.mkdir()
    target.write_text('{"title":"old"}\n', encoding="utf-8")
    observed_before_replace = []
    real_replace = corpus_gen.os.replace

    def observe_replace(source, destination):
        observed_before_replace.append(json.loads(target.read_text(encoding="utf-8")))
        real_replace(source, destination)

    monkeypatch.setattr(corpus_gen.os, "replace", observe_replace)

    corpus_gen._write_json(target, {"title": "new"})

    assert observed_before_replace == [{"title": "old"}]
    assert json.loads(target.read_text(encoding="utf-8")) == {"title": "new"}
