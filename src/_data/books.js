// Build-time enrichment for books.
//
// Reads the raw Airtable export and derives `coverWidths`/`hasCover` from
// whatever JPEGs actually exist on disk under src/assets/images/covers.
// The filesystem is the source of truth: if a cover file is there it
// renders; if not, the placeholder shows. This means fetch_airtable.py
// and process_images.py can be run in either order (or independently)
// without leaving the JSON and the asset folder out of sync.

const fs = require("fs");
const path = require("path");

const RAW_PATH = path.join(__dirname, "raw", "books.json");
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

module.exports = function () {
  if (!fs.existsSync(RAW_PATH)) return [];
  const books = JSON.parse(fs.readFileSync(RAW_PATH, "utf8"));
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
