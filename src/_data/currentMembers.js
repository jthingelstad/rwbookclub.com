// Members.json includes both current and former members so that the picker
// name can be resolved on book pages. The reading-journey site only generates
// individual pages for *current* members — this derived data file is what the
// member-page template paginates over.

const path = require("path");
const fs = require("fs");

module.exports = function () {
  const p = path.join(__dirname, "members.json");
  if (!fs.existsSync(p)) return [];
  const members = JSON.parse(fs.readFileSync(p, "utf8"));
  return members.filter((m) => m.isCurrent);
};
