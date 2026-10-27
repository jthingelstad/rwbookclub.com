// What a deploy to S3 publishes from the gh-pages tree, and how each file is served: its content type
// and Cache-Control. A file the table does not type fails the deploy (scripts/deploy-aws.mjs), so a new
// kind of file is a decision, not octet-stream by default.
import { readdirSync } from 'node:fs';
import { extname, join, relative, sep } from 'node:path';

// GitHub Pages bookkeeping and the workflow that rides on the branch; none of it is the site.
export const NOT_PUBLISHED = new Set(['CNAME', '.nojekyll', '.git']);
export const NOT_PUBLISHED_DIRS = ['.git/', '.github/'];

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  // The RSS feed and the sitemap, typed as GitHub Pages typed them (and CloudFront compresses it).
  '.xml': 'application/xml; charset=utf-8',
  '.ics': 'text/calendar; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.woff': 'font/woff',
  '.webmanifest': 'application/manifest+json'
};

// Extensions are matched in any case (a cover may arrive as .JPG). Undefined means untyped.
export function contentTypeFor(key) {
  return TYPES[extname(key).toLowerCase()];
}

// Browsers keep a file five minutes; CloudFront keeps it a day, and every deploy clears CloudFront.
// Fonts never change under their names, so browsers keep them a week.
export function cacheControlFor(key) {
  if (/\.woff2?$/.test(key)) return 'public, max-age=604800';
  return 'public, max-age=300, s-maxage=86400';
}

export function isPublished(key) {
  return !NOT_PUBLISHED.has(key) && !NOT_PUBLISHED_DIRS.some((dir) => key.startsWith(dir));
}

function walk(root, dir = root) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return walk(root, path);
    return entry.isFile() ? [relative(root, path).split(sep).join('/')] : [];
  });
}

// Every published file with its type and caching, in the order a deploy writes them: files first, then
// pages, the front page last, so no page goes out before what it loads.
export function publishPlan(root) {
  const rank = (key) => (key === 'index.html' ? 2 : key.endsWith('.html') ? 1 : 0);
  return walk(root)
    .filter(isPublished)
    .sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
    .map((key) => ({ key, path: join(root, key), contentType: contentTypeFor(key), cacheControl: cacheControlFor(key) }));
}
