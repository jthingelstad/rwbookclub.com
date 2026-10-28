// The sitemap and llms.txt templates. CI has no corpus, so it cannot build the
// site; these read the templates themselves (open-web audit, 2026-10-25).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const read = (name) => fs.readFileSync(path.join(__dirname, "..", "src", name), "utf8");

test("every sitemap url carries a lastmod from lib/lastmod.js", () => {
  const sitemap = read("sitemap.xml.njk");
  const urls = sitemap.match(/<url>[\s\S]*?<\/url>/g) ?? [];
  assert.ok(urls.length >= 13);
  for (const url of urls) assert.match(url, /<lastmod>\{\{ lastmod\.of\("[^"]*"( \+ \w+\.slug \+ "\/")?\) \}\}<\/lastmod>/, url);
});

test("the sitemap lists the Oliver page", () => {
  assert.ok(read("sitemap.xml.njk").includes("<loc>{{ site.url }}/oliver/</loc>"));
});

test("llms.txt names every URL as a markdown link, never a bare or backticked one", () => {
  const llms = read("llms.txt.njk");
  const body = llms.slice(llms.indexOf("---", 3) + 3);
  for (const line of body.split("\n")) {
    assert.ok(!/^\s+\{\{ site\.url \}\}/.test(line), `bare URL line: ${line}`);
    assert.ok(!/^- `\{\{ site\.url \}\}/.test(line), `backticked URL item: ${line}`);
    assert.ok(!/^- \*\*/.test(line), `book entry without a link: ${line}`);
  }
  for (const target of ["/llms.txt", "/books/", "/oliver/", "/books.json", "/authors/", "/meetings/"])
    assert.ok(llms.includes(`]({{ site.url }}${target})`), `links ${target}`);
  assert.equal((llms.match(/\]\(\{\{ site\.url \}\}\/books\/\{\{ (book|current)\.slug \}\}\.txt\)/g) ?? []).length, 4);
});
