// Groups books into year buckets for the reading-journey home page.
// Pulls from books.js (the enriched view) so cover widths reflect the
// JPEGs actually present on disk. The shared array is already sorted
// most-recent-first by the fetch script; this preserves that order
// inside each year and emits years in descending order.
//
// Placeholder books (future/tentative meetings) are separated out so
// the template can feature the current book and upcoming picks above
// the historical reading journey.

const buildBooks = require("./books.js");

module.exports = function () {
  const books = buildBooks();

  // Separate placeholder (future) books from the reading history
  const futureBooks = books.filter((b) => b.placeholder);
  const pastBooks = books.filter((b) => !b.placeholder);

  // Current book = placeholder with earliest meeting date
  // (futureBooks is sorted most-recent-first, so last item is earliest)
  const currentBook = futureBooks.length
    ? futureBooks[futureBooks.length - 1]
    : null;

  // Upcoming = remaining future books after the current one
  const upcomingBooks = futureBooks.length > 1
    ? futureBooks.slice(0, -1)
    : [];

  const byYear = new Map();
  for (const book of pastBooks) {
    if (!book.year) continue;
    if (!byYear.has(book.year)) byYear.set(book.year, []);
    byYear.get(book.year).push(book);
  }

  const years = [...byYear.entries()]
    .sort((a, b) => b[0] - a[0])
    .map(([year, books]) => ({ year, books }));

  return {
    years,
    totalBooks: books.length,
    currentBook,
    upcomingBooks,
  };
};
