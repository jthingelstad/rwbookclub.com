// Sitemap <lastmod> per page path (see lib/lastmod.js for what each date means).
// `lastmod.of(path)` throws for a path with no date, so the sitemap can never
// ship a <url> without one.
const { execFileSync } = require("node:child_process");
const path = require("node:path");
const buildBooks = require("./books.js");
const loadReviews = require("./reviews.js");
const { pageDates } = require("../../lib/lastmod");

// The last commit touching a prose page's template, as a date. Oliver publishes
// from a full checkout; a shallow or missing history (CI) leaves it out, and the
// page keeps its club-record date.
function templateDate(file) {
  const cwd = path.join(__dirname, "..");
  try {
    if (execFileSync("git", ["rev-parse", "--is-shallow-repository"], { cwd, encoding: "utf8" }).trim() === "true") return null;
    return execFileSync("git", ["log", "-1", "--format=%cs", "--", file], { cwd, encoding: "utf8" }).trim() || null;
  } catch {
    return null;
  }
}

module.exports = function () {
  const dates = pageDates({
    books: buildBooks.readJsonDir("books"),
    meetings: buildBooks.readJsonDir("meetings"),
    reviews: loadReviews(),
    lists: buildBooks.readJsonDir("lists"),
    members: buildBooks.readJsonDir("members"),
    authors: buildBooks.readJsonDir("authors"),
    templateDates: { about: templateDate("about.njk"), oliver: templateDate("oliver.njk") },
  });
  return {
    of(pagePath) {
      const date = dates.get(pagePath);
      if (!date) throw new Error(`sitemap: no lastmod for ${pagePath}`);
      return date;
    },
  };
};
