"""Regression tests for the generated corpus's versioned file contract."""

from __future__ import annotations

import json

from corpus.schema import SCHEMA_VERSION, validation_errors
from corpus.validate import validate_data_dir


def test_manifest_requires_the_supported_schema_version(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"schemaVersion": SCHEMA_VERSION + 1}))

    errors = validate_data_dir(tmp_path)

    assert any("manifest:" in error and "Input should be 1" in error for error in errors)


def test_contract_rejects_missing_and_unknown_book_fields():
    errors = validation_errors("books", {"bookId": 1, "unexpected": True})

    assert any(error.startswith("title: Field required") for error in errors)
    assert "unexpected: Extra inputs are not permitted" in errors


def test_validator_reports_malformed_documents_without_crashing(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"schemaVersion": SCHEMA_VERSION}))
    books = tmp_path / "books"
    books.mkdir()
    (books / "broken.json").write_text("{not-json")

    errors = validate_data_dir(tmp_path)

    assert len(errors) == 1
    assert errors[0].startswith("books/broken: invalid JSON:")
