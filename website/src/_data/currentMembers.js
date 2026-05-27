// Members.json includes both current and former members so that the picker
// name can be resolved on book pages. The reading-journey site only generates
// individual pages for *current* members — this derived data file is what the
// member-page template paginates over. Pulls from members.js so photo widths
// match what's on disk.

const buildMembers = require("./members.js");

module.exports = function () {
  return buildMembers().filter((m) => m.isCurrent);
};
