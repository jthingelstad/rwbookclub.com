// lib/lastmod.js: each page's date is the newest dated fact it shows, never the
// build time, and never a meeting that has not happened yet.
const assert = require("node:assert/strict");
const test = require("node:test");
const { centralDate, pageDates } = require("../lib/lastmod");

const now = new Date("2026-10-28T18:00:00Z");
const fixture = {
  now,
  books: [
    { slug: "old", authors: ["Ann"], picker: ["jo"] },
    { slug: "reviewed", authors: ["Ann"], picker: ["al"] },
    { slug: "next", authors: ["Bea"], picker: ["jo"] },
    { slug: "orphan", authors: ["Cy"], picker: [] },
  ],
  meetings: [
    { date: "2019-05-01", books: ["old"], host: ["jo"] },
    { date: "2026-09-29", books: ["reviewed"], host: ["al"] },
    { date: "2026-11-24", books: ["next"], host: ["jo"] },
  ],
  reviews: [{ book: "reviewed", member: "jo", createdAt: "2026-10-21T03:00:00+00:00" }],
  lists: [{ slug: "best", owner: "al", books: [{ book: "old" }] }],
  members: [
    { slug: "jo", isCurrent: true, joined: "2004-04-15" },
    { slug: "al", isCurrent: true, joined: "2003-04-01" },
    { slug: "gone", isCurrent: false, joined: "2005-01-01" },
  ],
  authors: [
    { slug: "ann", name: "Ann" },
    { slug: "bea", name: "Bea" },
    { slug: "cy", name: "Cy" },
  ],
  templateDates: { about: "2026-10-25", oliver: "2026-01-01" },
};

test("a review's UTC instant becomes the club's local date", () => {
  assert.equal(centralDate("2026-10-21T03:00:00+00:00"), "2026-10-20");
  assert.equal(centralDate(null), null);
});

test("pages take the newest dated fact they show", () => {
  const d = pageDates(fixture);
  assert.equal(d.get("/books/old/"), "2019-05-01");
  assert.equal(d.get("/books/reviewed/"), "2026-10-20"); // the review, after the meeting
  assert.equal(d.get("/books/next/"), "2026-09-29"); // not met yet: the last meeting held
  assert.equal(d.get("/books/orphan/"), "2026-09-29");
  assert.equal(d.get("/authors/ann/"), "2026-09-29"); // met on, not reviewed
  assert.equal(d.get("/lists/best/"), "2019-05-01");
  assert.equal(d.get("/members/jo/"), "2026-10-20"); // their review
  assert.equal(d.get("/members/al/"), "2026-09-29"); // hosted (and so picked)
  assert.equal(d.has("/members/gone/"), false);
  assert.equal(d.get("/meetings/"), "2026-09-29");
  assert.equal(d.get("/"), "2026-10-20");
  assert.equal(d.get("/about/"), "2026-10-25"); // its template changed later
  assert.equal(d.get("/oliver/"), "2026-10-20");
  assert.equal(d.get("/lists/"), "2019-05-01");
  for (const [page, date] of d) {
    assert.match(date, /^\d{4}-\d{2}-\d{2}$/, page);
    assert.ok(date <= "2026-10-28", page);
  }
});

test("nothing is dated in the future", () => {
  const d = pageDates({ ...fixture, templateDates: { about: "2030-01-01" }, reviews: [{ book: "old", member: "jo", createdAt: "2031-01-01T00:00:00Z" }] });
  assert.equal(d.get("/about/"), "2026-10-28");
  assert.equal(d.get("/books/old/"), "2026-10-28");
});
