"""Validation at the boundary between external facts and reader-facing sidecars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from agent.enrich.wikidata import AuthorResolution


@dataclass(frozen=True)
class ValidatedAuthorFacts:
    birth_year: int | None
    death_year: int | None
    status: str
    warnings: tuple[str, ...]


def _preferred(primary: dict, secondary: dict, key: str) -> int | None:
    value = primary.get(key)
    return value if value is not None else secondary.get(key)


def validate_author_facts(
    openlibrary: dict,
    wikidata: dict,
    resolution: AuthorResolution,
    *,
    current_year: int | None = None,
) -> ValidatedAuthorFacts:
    """Merge date facts gap-first and quarantine impossible chronology."""
    year = current_year or datetime.now(timezone.utc).year
    birth = _preferred(openlibrary, wikidata, "birth_year")
    death = _preferred(openlibrary, wikidata, "death_year")
    warnings = list(resolution.warnings)

    if birth is not None and birth > year:
        warnings.append("birth_year_in_future")
        birth = None
    if death is not None and death > year:
        warnings.append("death_year_in_future")
        death = None
    if birth is not None and death is not None and death < birth:
        warnings.append("death_year_before_birth_year")
        death = None

    return ValidatedAuthorFacts(
        birth,
        death,
        "partial" if warnings else "accepted",
        tuple(dict.fromkeys(warnings)),
    )
