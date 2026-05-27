// Reviews are canonical corpus data, stored one-per-file as Markdown with YAML
// frontmatter (the prose body is the review text). Reconstruct the record shape
// the templates expect ({ bookIds, memberIds, reviewers, rating, ... , review }).
// Sorted by record id ascending to match the original fetch order.

const fs = require("fs");
const path = require("path");
const matter = require("gray-matter");

const DIR = path.join(__dirname, "..", "..", "..", "corpus", "data", "reviews");

module.exports = function () {
  if (!fs.existsSync(DIR)) return [];
  return fs
    .readdirSync(DIR)
    .filter((f) => f.endsWith(".md"))
    .map((f) => {
      const { data, content } = matter(fs.readFileSync(path.join(DIR, f), "utf8"));
      return { ...data, review: content.trim() || null };
    })
    .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
};
