// The club-wide lists (scope='club') — the ones featured in the nav + the /lists/ hub.
const buildLists = require("./lists.js");

module.exports = function () {
  return buildLists().filter((l) => l.scope === "club");
};
