// Authors are canonical corpus data — aggregate the per-entity files.
// Sorted by record id ascending to match the original fetch order.

const fs = require("fs");
const path = require("path");

const DIR = path.join(__dirname, "..", "..", "..", "corpus", "data", "authors");

module.exports = function () {
  if (!fs.existsSync(DIR)) return [];
  return fs
    .readdirSync(DIR)
    .filter((f) => f.endsWith(".json"))
    .map((f) => JSON.parse(fs.readFileSync(path.join(DIR, f), "utf8")))
    .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
};
