// Build-time enrichment for books.
//
// Aggregates the per-entity book files from the corpus (the source of truth)
// and derives `coverWidths`/`hasCover` from whatever JPEGs actually exist on
// disk under src/assets/images/covers. The filesystem is the source of truth
// for covers: if a file is there it renders; if not, the placeholder shows.
// This means corpus.fetch/migrate and corpus.images can run in any order
// without leaving the data and the asset folder out of sync.

const fs = require("fs");
const path = require("path");

const BOOKS_DIR = path.join(__dirname, "..", "..", "..", "corpus", "data", "books");
const COVERS_DIR = path.join(__dirname, "..", "assets", "images", "covers");
const FILENAME_RE = /^(.+)-(\d+)\.jpg$/;

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

function loadBooks() {
  if (!fs.existsSync(BOOKS_DIR)) return [];
  return fs
    .readdirSync(BOOKS_DIR)
    .filter((f) => f.endsWith(".json"))
    .map((f) => JSON.parse(fs.readFileSync(path.join(BOOKS_DIR, f), "utf8")));
}

module.exports = function () {
  const books = loadBooks();
  // Most-recent-first: by meeting date desc, then Book ID desc (matches the
  // order the old fetch pipeline wrote, so the reading journey is unchanged).
  books.sort((a, b) => {
    const ad = a.meetingDate || "";
    const bd = b.meetingDate || "";
    if (ad < bd) return 1;
    if (ad > bd) return -1;
    return (b.bookId || 0) - (a.bookId || 0);
  });

  const widthsBySlug = buildWidthsBySlug();
  return books.map((b) => {
    const widths = widthsBySlug.get(b.slug);
    const { coverUrl, ...rest } = b;
    return {
      ...rest,
      hasCover: Boolean(widths && widths.length),
      coverWidths: widths || null,
    };
  });
};
