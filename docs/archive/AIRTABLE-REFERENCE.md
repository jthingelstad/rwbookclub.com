# Airtable reference (retired — historical import snapshot)

> **This is history, not current guidance.** Airtable was the club's original data store. It was
> retired after a one-time import (`python -m agent.script.archive.import_airtable`, which now reads the
> on-disk snapshot under `agent/script/_airtable_cache/`, not the API). **SQLite (`club_*` tables in
> `agent/oliver.db`) is authoritative** — see the repo `CLAUDE.md`. No code reads `AIRTABLE_*` env
> vars anymore. The table IDs, field shapes, row counts, and coverage numbers below are frozen at
> import time and explain how the data was sourced; they do **not** reflect the live DB. For current
> data conventions (picker vs host, local meeting time, topic taxonomy, etc.) see `CLAUDE.md`.

## Data Source (Airtable)

The schema below is kept to explain the field shapes the import read and the IDs (`Book ID` /
`Meeting ID` / `Member ID` / `Author ID` / `Review ID`) that became the integer primary keys.

- **Airtable base ID:** `appmiF5yLSzx0klJc`
- **Credentials (historical):** the import originally read `AIRTABLE_BASE_ID` and `AIRTABLE_PAT`. These are no longer read by any code.
- **REST API base:** `https://api.airtable.com/v0/{baseId}/{tableId}`
- **Pagination:** records endpoints return up to 100 per call; follow `offset` until empty
- **Batch writes:** PATCH/POST `/v0/{baseId}/{tableId}` accepts up to 10 records per call
- **PAT capabilities:** can read, write records, and create new fields and tables. **Cannot** PATCH single-select option lists on existing fields (returns 422). Workaround: send record writes with `"typecast": true` and Airtable will auto-create the new option.

## Schema

Six tables. IDs are stable; names can change.

### Books — `tblPqH96wIgGuUSXe` (176 rows)

Primary field is `Book` (title only; subtitle is separate).

| Field | Type | Notes |
|---|---|---|
| Book | text | Title; primary field |
| Subtitle | text | Part after the first colon. Empty for books with no subtitle. 119 of 176 have one. |
| Cover | attachment | 175 of 176. *The Devil in the White City* is the only one missing. |
| Authors | link → Authors | Multi |
| Meetings | link → Meetings | Multi. See "Books-to-Meetings is many-to-many" below. |
| Fiction | checkbox | 21 fiction, 155 non-fiction |
| Topic | single-select | 11 categories. All books assigned. See "Topic taxonomy". |
| ISBN-13 | text | 170 of 176. All are English-language editions (`978-0`, `978-1`, or `979-8` prefixes). 6 have no clean English edition on Open Library. |
| Synopsis | long text | 138 of 176. Sourced from Open Library. |
| Publication Year | number | 176 of 176. Year of first publication. |
| Page Count | number | 171 of 176. Median across editions per Open Library. |
| OL Key | text | Open Library Work key, e.g. `/works/OL17075811W`. Present for all 176. Use to refresh metadata or build links. |
| Date Read | rollup → Meetings.Meeting Date | |
| Year Read | formula | `YEAR({Date Read})` |
| Picked by | link → Members | Multi. The member(s) who picked the book. Empty for rare "group picks". Multiple pickers means the book spanned two meetings. Replaces the old approach of traversing Meetings.Host. |
| Review Count | count → Reviews | |
| Book ID | autonumber | Stable ID |

### Meetings — `tblJpQrukeCXaO0Uq` (181 rows)

Primary field is `Name` (formula: `MMMM YYYY: <Book>`).

| Field | Type | Notes |
|---|---|---|
| Name | formula | Auto-generated label |
| Meeting Date | dateTime | Range: 2003-04 → 2025-11 |
| Book | link → Books | Multi (rare). The Sept 2012 meeting links to two books. |
| Host | link → Members | **This is the book picker.** (Historical shorthand — the live model treats picker and host as distinct; see `CLAUDE.md`.) |
| Meeting Type | multi-select | Choices: Book, Spouses, Videos, Essay, Movie, Picking |
| Location | text | Sparse: 41 of 181 |
| Notes | long text | Sparse: 7 of 181 |
| Placeholder | checkbox | True if the date is approximate |
| Year | formula | `YEAR({Meeting Date})` |
| Meeting ID | autonumber | |

### Members — `tblsjVRbdj231zbwj` (12 rows)

Primary field is `Name`. 5 current, 7 former. No bio field, no joined/left dates (deferred).

| Field | Type | Notes |
|---|---|---|
| Name | text | |
| Photo | attachment | 10 of 12 |
| Current Member | checkbox | 5 true |
| Email Address | email | 6 of 12 |
| Mobile | phone | |
| Website | url | 3 of 12 |
| Picked Count | count → Meetings (Host) | Despite the name, this is `count(meetings where I am Host)`. The Host field is the picker. |
| Meetings | link → Meetings | |
| Reviews | link → Reviews | |

### Authors — `tblLkEUVXxLMynFtn` (175 rows)

Primary field is `Author`.

| Field | Type | Notes |
|---|---|---|
| Author | text | |
| Books | link → Books | |
| Bio | long text | 79 of 175. Sourced from Open Library, attribution cruft stripped. |
| Book Count | count → Books | |
| Last Read | rollup → Books.Date Read | |
| Author ID | autonumber | |

### Reviews — `tblxZR21gPDPYBfA1` (effectively empty as of launch)

Primary field is `Review Key` (formula: `<Member>|<Book>`).

Will be populated by member submissions after the site launches. Schema is ready.

| Field | Type | Notes |
|---|---|---|
| Review Key | formula | Composite key |
| Member | link → Members | |
| Book | link → Books | |
| Rating | rating 1-5 | Book quality |
| Review | long text | |
| DNF | checkbox | Did Not Finish |
| Discussion Quality | rating 1-5 | Quality of the book club discussion this book generated. Distinct from book rating. |
| Would Recommend | checkbox | Independent of rating |
| Favorite Quote | long text | |
| Created at | createdTime | |
| Review ID | autonumber | |

### Awards — `tblrIaGgMtA08xyJE` (empty)

Primary field is `Award Name` (free-form text). For tracking annual awards. Empty until used.

| Field | Type | Notes |
|---|---|---|
| Award Name | text | E.g., "2024 Book of the Year" |
| Year | number | |
| Award | single-select | Book of the Year, Runner-up, Honorable Mention, Worst Book, Most Discussed, Most Surprising |
| Book | link → Books | Currently allows multi-link. To switch to single, open the field in the Airtable UI and toggle "Limit selection to a single record". The API blocked this on creation. |
| Notes | long text | E.g., voting margin |
| Voted By | link → Members | Optional |

## Airtable-specific gotchas (retired)

### Single-select choices cannot be modified via the meta API

`PATCH /meta/bases/{baseId}/tables/{tableId}/fields/{fieldId}` returns 422 when modifying choices on an existing single-select. To add a new choice, send a record write with `"typecast": true` and Airtable auto-creates it.

### Airtable attachment URLs expire

Cover URLs in the `Cover` field are signed and rotate. For a static site that's fine if you re-fetch at build time. For a long-lived cache, mirror the bytes locally or use Open Library cover URLs. (The live site no longer uses Airtable covers — they come from committed files / Open Library.)

## Coverage cheat sheet (at import time, Airtable denominators)

| Field | Populated |
|---|---|
| Books.Cover | 175/176 |
| Books.ISBN-13 | 170/176 |
| Books.Topic | 176/176 |
| Books.Synopsis | 138/176 |
| Books.Publication Year | 176/176 |
| Books.Page Count | 171/176 |
| Books.OL Key | 176/176 |
| Books.Subtitle | 119 (others have no subtitle) |
| Books.Authors link | 175/176 (Devil in the White City) |
| Authors.Bio | 79/175 |
| Members.Photo | 10/12 |
| Meetings.Host | 170/181 |
| Meetings.Location | 41/181 |
| Meetings.Notes | 7/181 |
| Reviews.* | 2 records total (will grow after launch) |
| Awards.* | 0 records (empty until used) |

## API patterns (retired)

### List all records (paginated)

```python
def list_all(table_id):
    records, offset = [], None
    while True:
        url = f"https://api.airtable.com/v0/{BASE}/{table_id}?pageSize=100"
        if offset:
            url += f"&offset={offset}"
        d = requests.get(url, headers={"Authorization": f"Bearer {PAT}"}).json()
        records.extend(d["records"])
        offset = d.get("offset")
        if not offset:
            break
    return records
```

### Common filterByFormula examples

```
{Year Read}=2024
AND({Topic}="Technology", {Fiction}=FALSE())
NOT({ISBN-13}="")
{Current Member}=TRUE()
```

### Batch PATCH with typecast (for new single-select options)

```python
body = {
    "records": [{"id": rec_id, "fields": {"Topic": "New Category"}}],
    "typecast": True,
}
requests.patch(f"{base_url}/{books_table}", json=body, headers=auth)
```

## Open follow-ups (point-in-time, from the Airtable launch)

These were captured at launch; the still-binding ones (topic re-categorization needs Jamie's
per-book approval; member bios/dates deliberately out of schema) are enforced in `CLAUDE.md`'s
"Things not to do". Enrichment (Wikipedia bio fallback) has since shipped.

1. ~~*The Devil in the White City* has no cover.~~ Resolved — covers are complete for all 179 books.
2. Awards table Book field allows multi-link; toggle to single in the Airtable UI.
3. 88 authors matched to Open Library but have no bio there. Wikipedia fallback would help (no clean API; use the first paragraph of the article).
4. 8 authors did not match Open Library at all (Strugatsky brothers, Frederick P. Brooks Jr., Michael J. Casey, Andrei Lankov, Bruce White, Christopher Vaughan, David Gibbons). Looser name matching would catch most.
5. 6 books have no ISBN-13: Shaping Things, The Complexity of Cooperation, The Success of Open Source, Divorce Among the Gulls, The Rise and Fall of American Growth.
6. The 4 newest Topic categories are sparsely populated. A sweep over previously-classified books would surface candidates for re-categorization. Do not do this without Jamie's approval per book.
7. Member bios and joined/left dates are deliberately not in the schema. Don't add them without asking.
