"""Versioned models for the private generated corpus contract.

These models describe the files shared by Oliver's Python reader and the Eleventy website. They
validate structure only; cross-file slug relationships remain in :mod:`corpus.validate`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SCHEMA_VERSION = 1


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Manifest(ContractModel):
    schemaVersion: Literal[SCHEMA_VERSION]


class Book(ContractModel):
    bookId: int = Field(gt=0)
    title: str = Field(min_length=1)
    subtitle: str | None
    authors: list[str]
    topic: str | None
    fiction: bool
    publicationYear: int | None
    pageCount: int | None
    isbn13: str | None
    olKey: str | None
    synopsis: str | None
    picker: list[str]
    subjects: list[str] | None = None
    editionCount: int | None = None
    languages: list[str] | None = None
    ratingsAverage: float | None = None
    ratingsCount: int | None = None
    series: str | None = None
    awards: list[str] | None = None
    wikidataId: str | None = None
    wikipediaUrl: str | None = None
    goodreadsId: str | None = None


class Meeting(ContractModel):
    meetingId: int = Field(gt=0)
    date: str | None
    startTime: str | None
    books: list[str]
    host: list[str]
    type: list[str]
    location: str | None
    notes: str | None


class Website(ContractModel):
    url: str = Field(min_length=1)
    label: str | None = None


class Member(ContractModel):
    name: str = Field(min_length=1)
    isCurrent: bool
    joined: str | None
    websites: list[Website]


class Author(ContractModel):
    name: str = Field(min_length=1)
    bio: str | None = None
    birthYear: int | None = None
    deathYear: int | None = None
    nationality: str | None = None
    website: str | None = None
    wikipediaUrl: str | None = None
    notableWorks: list[str] | None = None
    photoCredit: str | None = None


class ListEntry(ContractModel):
    book: str = Field(min_length=1)
    note: str | None = None


class BookList(ContractModel):
    name: str = Field(min_length=1)
    scope: Literal["club", "member"]
    owner: str | None
    books: list[ListEntry]
    description: str | None = None


class Review(ContractModel):
    id: int = Field(gt=0)
    book: str = Field(min_length=1)
    member: str = Field(min_length=1)
    rating: int | None
    dnf: bool
    discussionQuality: int | None
    wouldRecommend: bool
    favoriteQuote: str | None
    createdAt: str


MODELS = {
    "books": Book,
    "meetings": Meeting,
    "members": Member,
    "authors": Author,
    "lists": BookList,
    "reviews": Review,
}


def validation_errors(kind: str, document: dict) -> list[str]:
    """Return stable, human-readable contract violations for one document."""
    model = MODELS[kind]
    try:
        model.model_validate(document)
    except ValidationError as exc:
        return [
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        ]
    return []
