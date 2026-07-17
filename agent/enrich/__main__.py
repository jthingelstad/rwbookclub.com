"""CLI for the enrichment loop:  python -m agent.enrich [flags]."""

from __future__ import annotations

import argparse

from agent.enrich.loop import run


def main() -> None:
    from agent import database

    database.initialize()
    ap = argparse.ArgumentParser(description="Fill the club_*_enrichment sidecars.")
    ap.add_argument("--books", action="store_true", help="enrich books")
    ap.add_argument("--authors", action="store_true", help="enrich authors")
    ap.add_argument("--force", action="store_true", help="re-fetch already-enriched rows")
    ap.add_argument("--limit", type=int, help="cap the number of entities")
    ap.add_argument("--slug", help="enrich only this slug")
    ap.add_argument("--no-images", action="store_true", help="skip image fetching")
    args = ap.parse_args()
    # Default to both when neither flag is given.
    do_books = args.books or not (args.books or args.authors)
    do_authors = args.authors or not (args.books or args.authors)
    counts = run(
        do_books=do_books,
        do_authors=do_authors,
        force=args.force,
        limit=args.limit,
        slug=args.slug,
        fetch_images=not args.no_images,
    )
    print(f"\nenriched {counts['books']} books, {counts['authors']} authors")


if __name__ == "__main__":
    main()
