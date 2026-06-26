// Authors are canonical corpus data (name + bio + external enrichment: dates,
// nationality, links, notable works; slug is the filename). The club's books
// reference authors by name, so here we attach each author's read books (joined
// from the enriched books view) plus portrait widths derived from the JPEGs on
// disk (mirrors members.js), for the author detail/index pages.

const fs = require("fs");
const path = require("path");

const buildBooks = require("./books.js");

const PHOTOS_DIR = path.join(__dirname, "..", "assets", "images", "authors");
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
  const authors = buildBooks.readJsonDir("authors"); // adds slug from filename
  const books = buildBooks(); // enriched (meetingDate, year, cover widths)
  const widthsBySlug = photoWidthsBySlug();

  return authors
    .map((a) => {
      const theirBooks = books
        .filter((b) => Array.isArray(b.authors) && b.authors.includes(a.name))
        .map((b) => ({
          slug: b.slug,
          title: b.title,
          year: b.year,
          meetingDate: b.meetingDate,
          hasCover: b.hasCover,
          coverWidths: b.coverWidths,
        }))
        .sort((x, y) => (y.meetingDate || "").localeCompare(x.meetingDate || ""));
      const widths = widthsBySlug.get(a.slug);
      const lifespan = a.birthYear
        ? a.deathYear
          ? `${a.birthYear}–${a.deathYear}`
          : `b. ${a.birthYear}`
        : null;
      return {
        ...a,
        books: theirBooks,
        lifespan,
        hasPhoto: Boolean(widths && widths.length),
        photoWidths: widths || null,
      };
    })
    .sort((a, b) => (a.name || "").localeCompare(b.name || ""));
};
