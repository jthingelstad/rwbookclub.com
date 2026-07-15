const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const clock = require("../lib/clock");
const cases = JSON.parse(
  fs.readFileSync(path.resolve(__dirname, "..", "..", "tests", "fixtures", "clock_cases.json"), "utf8")
);

test("Central calendar dates match the shared Python cases", () => {
  for (const item of cases.today) {
    assert.equal(clock.centralToday(new Date(item.now)), item.centralDate);
  }
});

test("meeting roll boundaries match the shared Python cases, including DST", () => {
  for (const item of cases.upcoming) {
    assert.equal(
      clock.isUpcoming(item.meetingDate, item.startTime, new Date(item.now)),
      item.expected,
      JSON.stringify(item)
    );
  }
});
