// Build-time enrichment for books.
//
// The corpus is normalized: book files hold intrinsic facts + picker (member
// slugs); meetings own date + book refs. Here we JOIN — derive each book's
// meeting date / picker names / upcoming-or-past etc. — and derive coverWidths from
// the JPEGs actually on disk. Downstream (journey, stats, templates) sees the
// same enriched fields as before.

const fs = require("fs");
const path = require("path");
const clock = require("../../lib/clock");

const DATA = path.join(__dirname, "..", "..", "..", "corpus", "data");
const COVERS_DIR = path.join(__dirname, "..", "assets", "images", "covers");
const FILENAME_RE = /^(.+)-(\d+)\.jpg$/;

// OL subject tags are raw and noisy: catalog cruft, bestseller-list membership,
// machine tags (award:…, nyt:…=…), foreign-language dupes. Drop the noise so that
// a shared subject between two books actually means something thematic.
const SUBJECT_STOP = new Set([
  "new york times bestseller",
  "new york times reviewed",
  "large type books",
  "popular works",
  "general",
  "case studies",
  "accessible book",
  "protected daisy",
  "in library",
  "overdrive",
  "histoire",
  "geschichte",
]);

// Clean a raw subject list into ordered {key, label} pairs (key is the casefolded
// match key; label is the first-seen original casing). Used for both subject
// matching and any subject display.
function cleanSubjects(raw) {
  const out = [];
  const seen = new Set();
  for (const s of raw || []) {
    if (typeof s !== "string") continue;
    const label = s.trim();
    if (!label) continue;
    if (/[:=\/]/.test(label)) continue; // machine tags, "X: Y", BISAC codes ("SCIENCE / …")
    const key = label.toLowerCase();
    if (SUBJECT_STOP.has(key) || /^reading level/.test(key)) continue;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ key, label });
  }
  return out;
}

function tokenSet(book) {
  return new Set(
    `${book.title || ""} ${book.synopsis || ""}`
      .toLowerCase()
      .replace(/[^a-z0-9\s]/g, " ")
      .split(/\s+/)
      .filter((t) => t.length > 4)
  );
}

// Strongest single reason for a related-book match, for the caption under its cover.
// Some OL subjects are themselves comma-laden ("Fiction, science fiction, general"),
// which read badly inline — prefer single-concept tags and fall back to topic.
function reasonText({ author, subjects, topic }) {
  if (author) return "By the same author";
  const clean = (subjects || []).filter((s) => !s.includes(","));
  if (clean.length) return "Shared themes: " + clean.slice(0, 2).join(" · ");
  if (topic) return topic;
  if (subjects && subjects.length) return "Shared themes: " + subjects[0];
  return "Related";
}

// Build each book's `related` list (top matches by shared author / topic / cleaned
// subjects / title+synopsis language). Mirrors the agent's related_books scoring,
// but matches on cleaned subjects so list-membership noise doesn't dominate.
const RELATED_MIN_SCORE = 24; // ≥2 shared cleaned subjects, or same topic, or shared author
const RELATED_LIMIT = 6;

function attachRelated(books) {
  const meta = books.map((b) => {
    const cleaned = cleanSubjects(b.subjects);
    return {
      keys: new Set(cleaned.map((s) => s.key)),
      labels: new Map(cleaned.map((s) => [s.key, s.label])),
      authors: new Set(b.authors || []),
      tokens: tokenSet(b),
    };
  });

  for (let i = 0; i < books.length; i++) {
    const b = books[i];
    const bm = meta[i];
    const scored = [];
    for (let j = 0; j < books.length; j++) {
      if (i === j) continue;
      const o = books[j];
      const om = meta[j];
      let score = 0;
      const reason = {};

      const sharedAuthor = [...bm.authors].some((a) => om.authors.has(a));
      if (sharedAuthor) {
        score += 60;
        reason.author = true;
      }
      if (b.topic && b.topic === o.topic) {
        score += 35;
        reason.topic = b.topic;
      }
      const sharedSubs = [...bm.keys].filter((k) => om.keys.has(k));
      if (sharedSubs.length) {
        score += Math.min(sharedSubs.length * 12, 48);
        reason.subjects = sharedSubs.map((k) => bm.labels.get(k));
      }
      if (Boolean(b.fiction) === Boolean(o.fiction)) score += 5;
      let overlap = 0;
      for (const t of bm.tokens) if (om.tokens.has(t)) overlap++;
      if (overlap) score += Math.min(overlap * 3, 18);

      if (score >= RELATED_MIN_SCORE) {
        scored.push({ score, year: o.year || 0, slug: o.slug, reason });
      }
    }
    scored.sort((x, y) => y.score - x.score || y.year - x.year);
    b.related = scored
      .slice(0, RELATED_LIMIT)
      .map((s) => ({ slug: s.slug, reason: reasonText(s.reason) }));
  }
}

function readJsonDir(name) {
  const dir = path.join(DATA, name);
  if (!fs.existsSync(dir)) return [];
  return fs
    .readdirSync(dir)
    .filter((f) => f.endsWith(".json"))
    // slug is the filename — derive it, don't store it.
    .map((f) => ({ ...JSON.parse(fs.readFileSync(path.join(dir, f), "utf8")), slug: f.slice(0, -5) }));
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
  // Author files store name + bio; the slug is the filename. Books reference
  // authors by name, so map name → slug to link bylines to author pages.
  const authorSlugByName = new Map(
    readJsonDir("authors").map((a) => [a.name, a.slug])
  );
  const meetingForBook = earliestMeetingBySlug(meetings);
  const widthsBySlug = buildWidthsBySlug();
  const reviewCounts = reviewCountBySlug();

  const enriched = books.map((b) => {
    const mt = meetingForBook.get(b.slug) || null;
    const meetingDate = mt ? mt.date || null : null;
    // Upcoming vs past is derived from the meeting's local date+time (see website/lib/clock).
    const isUpcoming = Boolean(mt && clock.isUpcoming(meetingDate, mt.startTime));
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
      authorSlugs: (b.authors || []).map((name) => authorSlugByName.get(name) || null),
      // Drop comma-laden OL composites ("Fiction, historical, general") for display —
      // they read as separate tags once joined. Matching still uses the full set.
      subjectTags: cleanSubjects(b.subjects)
        .filter((s) => !s.label.includes(","))
        .map((s) => s.label),
      meetingDate,
      year: meetingDate ? Number(meetingDate.slice(0, 4)) : null,
      pickerName: pickerNames[0] || null,
      pickerSlug: pickerSlugs.length ? pickerSlugs[0] : null,
      pickerNames: pickerNames.length ? pickerNames : null,
      pickerSlugs: pickerSlugs.length ? pickerSlugs : null,
      isUpcoming,
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
  attachRelated(enriched);
  return enriched;
}

module.exports = enrich;
module.exports.readJsonDir = readJsonDir;
module.exports.earliestMeetingBySlug = earliestMeetingBySlug;
