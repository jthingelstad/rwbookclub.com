// Build-time enrichment for members. Mirror of books.js — derives
// `photoWidths`/`hasPhoto` from JPEGs on disk so the build always matches
// what's actually in src/assets/images/members.

const fs = require("fs");
const path = require("path");

const RAW_PATH = path.join(__dirname, "raw", "members.json");
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

module.exports = function () {
  if (!fs.existsSync(RAW_PATH)) return [];
  const members = JSON.parse(fs.readFileSync(RAW_PATH, "utf8"));
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
