// Build-time enrichment for members. Member files hold identity only; a member's
// picks are derived from the books they picked (book.picker), and photo widths
// come from the JPEGs on disk. Re-emits pickedBooks/pickedCount like before.

const fs = require("fs");
const path = require("path");

const buildBooks = require("./books.js");

const MEMBERS_DIR = path.join(__dirname, "..", "..", "..", "corpus", "data", "members");
const PHOTOS_DIR = path.join(__dirname, "..", "assets", "images", "members");
const FILENAME_RE = /^(.+)-(\d+)\.jpg$/;

function photoWidthsBySlug() {
  if (!fs.existsSync(PHOTOS_DIR)) return new Map();
  const widths = new Map();
  for (const name of fs.readdirSync(PHOTOS_DIR)) {
    const m = FILENAME_RE.exec(name);
    if (!m) continue;
    const [, slug, w] = m;
    if (!widths.has(slug)) widths.set(slug, []);
    widths.get(slug).push(Number(w));
  }
  for (const list of widths.values()) list.sort((a, b) => a - b);
  return widths;
}

module.exports = function () {
  const members = buildBooks.readJsonDir("members");
  const books = buildBooks(); // enriched (has pickerNames, meetingDate, year)
  const meetings = buildBooks.readJsonDir("meetings"); // carry meeting.host[] (slugs)
  const widthsBySlug = photoWidthsBySlug();

  // member slug → count of meetings they hosted (meeting-level, ≠ picks).
  const hostedBySlug = new Map();
  for (const mt of meetings) {
    for (const slug of mt.host || []) {
      hostedBySlug.set(slug, (hostedBySlug.get(slug) || 0) + 1);
    }
  }

  members.sort((a, b) => (a.name || "").localeCompare(b.name || ""));

  return members.map((m) => {
    const picked = books.filter(
      (b) => Array.isArray(b.pickerNames) && b.pickerNames.includes(m.name)
    );
    const pickedBooks = m.isCurrent
      ? picked
          .map((b) => ({ slug: b.slug, title: b.title, year: b.year, date: b.meetingDate }))
          .sort((a, b) => (b.date || "").localeCompare(a.date || ""))
      : [];
    const widths = m.isCurrent ? widthsBySlug.get(m.slug) : undefined;
    return {
      ...m,
      pickedCount: picked.length,
      pickedBooks,
      hostedCount: hostedBySlug.get(m.slug) || 0,
      hasPhoto: Boolean(widths && widths.length),
      photoWidths: widths || null,
    };
  });
};
