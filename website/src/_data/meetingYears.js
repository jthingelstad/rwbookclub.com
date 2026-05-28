// Groups the enriched meetings into year buckets (descending) for the
// /meetings/ timeline, mirroring journey.js for the reading history.

const buildMeetings = require("./meetings.js");

module.exports = function () {
  const meetings = buildMeetings();
  const byYear = new Map();
  for (const m of meetings) {
    if (!m.year) continue;
    if (!byYear.has(m.year)) byYear.set(m.year, []);
    byYear.get(m.year).push(m);
  }
  return [...byYear.entries()]
    .sort((a, b) => b[0] - a[0])
    .map(([year, meetings]) => ({ year, meetings }));
};
