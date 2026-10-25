const assert = require("node:assert/strict");
const test = require("node:test");

const { rfc822Date } = require("../lib/rss");

test("a meeting date keeps its calendar day in Central", () => {
  assert.equal(rfc822Date("2026-07-28"), "Tue, 28 Jul 2026 12:00:00 GMT");
  const central = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Chicago" });
  for (const day of ["2026-07-28", "2026-01-15"]) {
    assert.equal(central.format(new Date(rfc822Date(day))), day);
  }
});

test("a value with a time is kept, and an empty one is empty", () => {
  assert.equal(rfc822Date("2026-07-28T23:30:00Z"), "Tue, 28 Jul 2026 23:30:00 GMT");
  assert.equal(rfc822Date(""), "");
  assert.equal(rfc822Date(null), "");
});
