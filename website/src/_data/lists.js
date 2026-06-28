// Build-time enrichment for book lists. Each list's entries reference books by slug + an optional
// note; resolve those to the full cover-enriched book records (mirrors members.js). `ownerName` is
// the member's display name (null for club lists).
const buildBooks = require("./books.js");

module.exports = function () {
  const lists = buildBooks.readJsonDir("lists");
  const books = buildBooks(); // enriched: title, slug, hasCover, coverWidths, year
  const bookBySlug = new Map(books.map((b) => [b.slug, b]));
  const members = buildBooks.readJsonDir("members");
  const nameBySlug = new Map(members.map((m) => [m.slug, m.name]));
  const currentSlugs = new Set(members.filter((m) => m.isCurrent).map((m) => m.slug));
  return lists.map((l) => ({
    ...l,
    ownerName: l.owner ? nameBySlug.get(l.owner) || l.owner : null,
    // Member pages exist only for current members — don't link a former member's list back to a 404.
    ownerCurrent: l.owner ? currentSlugs.has(l.owner) : false,
    entries: (l.books || [])
      .map((e) => {
        const b = bookBySlug.get(e.book);
        return b ? { ...b, note: e.note || null } : null;
      })
      .filter(Boolean), // drop entries pointing at unknown books
  }));
};
