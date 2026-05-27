// Authors are canonical corpus data — aggregate the per-entity files, sorted by name.

const fs = require("fs");
const path = require("path");

const DIR = path.join(__dirname, "..", "..", "..", "corpus", "data", "authors");

module.exports = function () {
  if (!fs.existsSync(DIR)) return [];
  return fs
    .readdirSync(DIR)
    .filter((f) => f.endsWith(".json"))
    .map((f) => JSON.parse(fs.readFileSync(path.join(DIR, f), "utf8")))
    .sort((a, b) => (a.name || "").localeCompare(b.name || ""));
};
