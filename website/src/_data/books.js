// Build-time enrichment for books.
//
// The corpus is normalized: book files hold intrinsic facts + picker (member
// slugs); meetings own date + book refs. Here we JOIN — derive each book's
// meeting date / picker names / placeholder etc. — and derive coverWidths from
// the JPEGs actually on disk. Downstream (journey, stats, templates) sees the
// same enriched fields as before.

const fs = require("fs");
const path = require("path");

const DATA = path.join(__dirname, "..", "..", "..", "corpus", "data");
const COVERS_DIR = path.join(__dirname, "..", "assets", "images", "covers");
const FILENAME_RE = /^(.+)-(\d+)\.jpg$/;

function readJsonDir(name) {
  const dir = path.join(DATA, name);
  if (!fs.existsSync(dir)) return [];
  return fs
    .readdirSync(dir)
    .filter((f) => f.endsWith(".json"))
    .map((f) => JSON.parse(fs.readFileSync(path.join(dir, f), "utf8")));
}

function reviewCountBySlug() {
  // Count review files per book ("<book-slug>--<member-slug>.md"); "--" is only the separator.
  const dir = path.join(DATA, "reviews");
  const counts = new Map();
  if (!fs.existsSync(dir)) return counts;
  for (const f of fs.readdirSync(dir)) {
    if (!f.endsWith(".md")) continue;
    const slug = f.slice(0, -3).split("--")[0];
    counts.set(slug, (counts.get(slug) || 0) + 1);
  }
  return counts;
}

function buildWidthsBySlug() {
  if (!fs.existsSync(COVERS_DIR)) return new Map();
  const widths = new Map();
  for (const name of fs.readdirSync(COVERS_DIR)) {
    const m = FILENAME_RE.exec(name);
    if (!m) continue;
    const [, slug, w] = m;
    if (!widths.has(slug)) widths.set(slug, []);
    widths.get(slug).push(Number(w));
  }
  for (const list of widths.values()) list.sort((a, b) => a - b);
  return widths;
}

// Exposed so members.js can reuse the same book→meeting join.
function earliestMeetingBySlug(meetings) {
  const map = new Map();
  for (const mt of meetings) {
    for (const bslug of mt.books || []) {
      const cur = map.get(bslug);
      if (!cur || (mt.date || "") < (cur.date || "")) map.set(bslug, mt);
    }
  }
  return map;
}

function enrich() {
  const books = readJsonDir("books");
  const meetings = readJsonDir("meetings");
  const members = readJsonDir("members");
  const memberBySlug = new Map(members.map((m) => [m.slug, m]));
  const meetingForBook = earliestMeetingBySlug(meetings);
  const widthsBySlug = buildWidthsBySlug();
  const reviewCounts = reviewCountBySlug();

  const enriched = books.map((b) => {
    const mt = meetingForBook.get(b.slug) || null;
    const meetingDate = mt ? mt.date || null : null;
    const pickerNames = [];
    const pickerSlugs = [];
    for (const ps of b.picker || []) {
      const m = memberBySlug.get(ps);
      if (!m) continue;
      pickerNames.push(m.name);
      pickerSlugs.push(m.isCurrent ? m.slug : null);
    }
    const widths = widthsBySlug.get(b.slug);
    const { picker, ...rest } = b;
    return {
      ...rest,
      meetingDate,
      year: meetingDate ? Number(meetingDate.slice(0, 4)) : null,
      pickerName: pickerNames[0] || null,
      pickerSlug: pickerSlugs.length ? pickerSlugs[0] : null,
      pickerNames: pickerNames.length ? pickerNames : null,
      pickerSlugs: pickerSlugs.length ? pickerSlugs : null,
      placeholder: mt ? Boolean(mt.placeholder) : false,
      meetingNotes: mt ? mt.notes || null : null,
      meetingLocation: mt ? mt.location || null : null,
      hasCover: Boolean(widths && widths.length),
      reviewCount: reviewCounts.get(b.slug) || 0,
      coverWidths: widths || null,
    };
  });

  // Most-recent first: meeting date desc, then Book ID desc.
  enriched.sort((a, b) => {
    const ad = a.meetingDate || "";
    const bd = b.meetingDate || "";
    if (ad < bd) return 1;
    if (ad > bd) return -1;
    return (b.bookId || 0) - (a.bookId || 0);
  });
  return enriched;
}

module.exports = enrich;
module.exports.readJsonDir = readJsonDir;
module.exports.earliestMeetingBySlug = earliestMeetingBySlug;
