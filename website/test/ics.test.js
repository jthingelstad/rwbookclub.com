const assert = require("node:assert/strict");
const test = require("node:test");

const { fold } = require("../src/meetings.ics.11ty");

test("RFC 5545 folding preserves UTF-8 and limits every physical line to 75 octets", () => {
  const original = "DESCRIPTION:" + "Cræft — «patterns» · ".repeat(12);
  const folded = fold(original);
  const lines = folded.split("\r\n");
  for (const line of lines) {
    assert.ok(Buffer.byteLength(line, "utf8") <= 75, line);
    assert.equal(line.includes("�"), false);
  }
  const unfolded = lines.map((line, index) => (index ? line.slice(1) : line)).join("");
  assert.equal(unfolded, original);
});
