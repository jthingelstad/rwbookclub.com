// The built llms.txt, read the way the llmstxt.org reference parser (llms-txt)
// reads it: under each "##" every non-empty line must be one
// "- [name](url): notes" link, or the parser fails the whole file (open-web
// audit G21, 2026-10-25). CI runs this after "Build website", with
// LLMS_TXT_REQUIRED set; `npm test` runs before any build and skips it.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const built = path.join(__dirname, "..", "_site", "llms.txt");
const required = Boolean(process.env.LLMS_TXT_REQUIRED);

test("the built llms.txt parses as llmstxt.org lists", { skip: !required && !fs.existsSync(built) && "no build" }, () => {
  const text = fs.readFileSync(built, "utf8");
  const [head, ...sections] = text.split(/^##\s*/m);
  assert.match(head, /^# \S[^\n]*\n+> [^\n]+\n+\S/, "opens with # title, then > summary, then text");
  assert.ok(sections.length > 0, "has sections");
  for (const section of sections) {
    const [heading, ...lines] = section.split("\n");
    for (const line of lines) {
      if (line.trim()) assert.match(line, /^- \[[^\]]+\]\(https?:[^)\s]+\)(: \S.*)?$/, `under "## ${heading}": ${line}`);
    }
  }
});
