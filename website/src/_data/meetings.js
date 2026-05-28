// Meetings are canonical corpus data (first-class entities): date, the book(s)
// discussed, meeting type, location, notes. Here we resolve each book slug to a
// title for the timeline, and derive the year + the non-"Book" types (spouses
// nights, movie nights, picking sessions) that give the club its social texture.
// Sorted most-recent first.

const fs = require("fs");
const path = require("path");

const buildBooks = require("./books.js");

const DIR = path.join(__dirname, "..", "..", "..", "corpus", "data", "meetings");

module.exports = function () {
  if (!fs.existsSync(DIR)) return [];

  const bySlug = new Map(buildBooks().map((b) => [b.slug, b]));

  return fs
    .readdirSync(DIR)
    .filter((f) => f.endsWith(".json"))
    .map((f) => JSON.parse(fs.readFileSync(path.join(DIR, f), "utf8")))
    .map((m) => {
      const types = Array.isArray(m.type) ? m.type : m.type ? [m.type] : [];
      const bookRefs = (m.books || []).map((slug) => {
        const b = bySlug.get(slug);
        return b ? { slug: b.slug, title: b.title } : { slug, title: slug };
      });
      return {
        ...m,
        year: m.date ? Number(m.date.slice(0, 4)) : null,
        types,
        nonBookTypes: types.filter((t) => t !== "Book"),
        bookRefs,
      };
    })
    .sort((a, b) => {
      const ad = a.date || "";
      const bd = b.date || "";
      if (ad < bd) return 1;
      if (ad > bd) return -1;
      return (b.meetingId || 0) - (a.meetingId || 0);
    });
};
