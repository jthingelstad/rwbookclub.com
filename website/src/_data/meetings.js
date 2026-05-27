// Meetings are canonical corpus data (first-class entities). Not yet rendered
// by any page, but exposed as global data for future use (e.g. a meetings
// timeline) and so the build validates the files. Sorted most-recent first.

const fs = require("fs");
const path = require("path");

const DIR = path.join(__dirname, "..", "..", "..", "corpus", "data", "meetings");

module.exports = function () {
  if (!fs.existsSync(DIR)) return [];
  return fs
    .readdirSync(DIR)
    .filter((f) => f.endsWith(".json"))
    .map((f) => JSON.parse(fs.readFileSync(path.join(DIR, f), "utf8")))
    .sort((a, b) => {
      const ad = a.date || "";
      const bd = b.date || "";
      if (ad < bd) return 1;
      if (ad > bd) return -1;
      return (b.meetingId || 0) - (a.meetingId || 0);
    });
};
