// Authors are canonical corpus data (name + bio; slug is the filename). The club's
// books reference authors by name, so here we attach each author's read books
// (joined from the enriched books view) for the author detail/index pages.

const buildBooks = require("./books.js");

module.exports = function () {
  const authors = buildBooks.readJsonDir("authors"); // adds slug from filename
  const books = buildBooks(); // enriched (meetingDate, year, cover widths)

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
      return { ...a, books: theirBooks };
    })
    .sort((a, b) => (a.name || "").localeCompare(b.name || ""));
};
