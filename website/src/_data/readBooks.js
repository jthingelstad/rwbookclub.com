const buildBooks = require("./books.js");

module.exports = function () {
  return buildBooks().filter((b) => b.meetingDate && !b.isUpcoming);
};
