// Groups books into year buckets for the reading-journey home page.
// books.json is already sorted most-recent first; this preserves that order
// inside each year and emits years in descending order.

const path = require("path");
const fs = require("fs");

module.exports = function () {
  const booksPath = path.join(__dirname, "books.json");
  if (!fs.existsSync(booksPath)) {
    // Fetch hasn't been run yet — return an empty journey so 11ty can still build.
    return { years: [], totalBooks: 0 };
  }
  const books = JSON.parse(fs.readFileSync(booksPath, "utf8"));

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
