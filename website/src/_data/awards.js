// Awards are canonical corpus data — aggregate the per-entity files.
// Sorted most-recent year first, then by award category (matches fetch order).

const fs = require("fs");
const path = require("path");

const DIR = path.join(__dirname, "..", "..", "..", "corpus", "data", "awards");

const ORDER = {
  "Book of the Year": 0,
  "Runner-up": 1,
  "Honorable Mention": 2,
  "Most Discussed": 3,
  "Most Surprising": 4,
  "Worst Book": 5,
};

module.exports = function () {
  if (!fs.existsSync(DIR)) return [];
  return fs
    .readdirSync(DIR)
    .filter((f) => f.endsWith(".json"))
    .map((f) => JSON.parse(fs.readFileSync(path.join(DIR, f), "utf8")))
    .sort((a, b) => {
      const y = (b.year || 0) - (a.year || 0);
      if (y) return y;
      return (ORDER[a.award] ?? 99) - (ORDER[b.award] ?? 99);
    });
};
