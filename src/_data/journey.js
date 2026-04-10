// Groups books into year buckets for the reading-journey home page.
// Pulls from books.js (the enriched view) so cover widths reflect the
// JPEGs actually present on disk. The shared array is already sorted
// most-recent-first by the fetch script; this preserves that order
// inside each year and emits years in descending order.

const buildBooks = require("./books.js");

module.exports = function () {
  const books = buildBooks();

  const byYear = new Map();
  for (const book of books) {
    if (!book.year) continue;
    if (!byYear.has(book.year)) byYear.set(book.year, []);
    byYear.get(book.year).push(book);
  }

  const years = [...byYear.entries()]
    .sort((a, b) => b[0] - a[0])
    .map(([year, books]) => ({ year, books }));

  return { years, totalBooks: books.length };
};
