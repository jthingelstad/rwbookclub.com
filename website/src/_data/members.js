// Build-time enrichment for members. Mirror of books.js — aggregates the
// per-entity member files and derives `photoWidths`/`hasPhoto` from JPEGs on
// disk so the build always matches what's in src/assets/images/members.

const fs = require("fs");
const path = require("path");

const MEMBERS_DIR = path.join(__dirname, "..", "..", "..", "corpus", "data", "members");
const PHOTOS_DIR = path.join(__dirname, "..", "assets", "images", "members");
const FILENAME_RE = /^(.+)-(\d+)\.jpg$/;

function buildWidthsBySlug() {
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

function loadMembers() {
  if (!fs.existsSync(MEMBERS_DIR)) return [];
  return fs
    .readdirSync(MEMBERS_DIR)
    .filter((f) => f.endsWith(".json"))
    .map((f) => JSON.parse(fs.readFileSync(path.join(MEMBERS_DIR, f), "utf8")));
}

module.exports = function () {
  const members = loadMembers();
  // Preserve the original (record-id ascending) order the fetch pipeline used.
  members.sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));

  const widthsBySlug = buildWidthsBySlug();
  return members.map((m) => {
    const widths = m.isCurrent ? widthsBySlug.get(m.slug) : undefined;
    const { photoUrl, ...rest } = m;
    return {
      ...rest,
      hasPhoto: Boolean(widths && widths.length),
      photoWidths: widths || null,
    };
  });
};
