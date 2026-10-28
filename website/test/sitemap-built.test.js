// The built sitemap: every <url> carries a W3C date <lastmod>, none in the future.
// CI runs this after "Build website", with SITEMAP_REQUIRED set; `npm test` runs
// before any build and skips it.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const clock = require("../lib/clock");

const built = path.join(__dirname, "..", "_site", "sitemap.xml");
const required = Boolean(process.env.SITEMAP_REQUIRED);

test("every sitemap url has a valid lastmod", { skip: !required && !fs.existsSync(built) && "no build" }, () => {
  const xml = fs.readFileSync(built, "utf8");
  const urls = [...xml.matchAll(/<url>([\s\S]*?)<\/url>/g)].map((m) => m[1]);
  assert.ok(urls.length > 9, "lists the section pages and more");
  const today = clock.centralToday();
  for (const url of urls) {
    const lastmod = /<lastmod>([^<]+)<\/lastmod>/.exec(url)?.[1];
    assert.ok(lastmod, `no lastmod in ${url.trim()}`);
    assert.match(lastmod, /^\d{4}-\d{2}-\d{2}$/);
    assert.ok(!Number.isNaN(Date.parse(lastmod)));
    assert.ok(lastmod <= today, `future lastmod ${lastmod}`);
  }
});
