// The S3 and CloudFront hosting: the viewer-request function (www to the apex, the slash form, index.html),
// how each file is typed and cached, the committed templates matching their source, and the gh-pages
// workflow that carries each Oliver publish to S3 riding on every build. `npm test` runs it before any
// build; CI runs it again after "Build website" with BUILT_SITE_REQUIRED set, against the real _site.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { DOMAIN, viewerRequestCode } from '../infra/viewer-request.mjs';
import { ACCESS_LOG_FIELDS, ALIASES, DEPLOY_SUBJECTS, certificate, delivery, site } from '../infra/templates.mjs';
import { cacheControlFor, contentTypeFor, isPublished, publishPlan } from '../scripts/publish-plan.mjs';

const root = new URL('../', import.meta.url);
const read = (path) => readFileSync(new URL(path, root), 'utf8');
const handler = new Function(viewerRequestCode + '\nreturn handler;')();
const request = (uri, { host = DOMAIN, querystring = {} } = {}) =>
  handler({ request: { uri, querystring, headers: { host: { value: host } } } });

test('a www. host is a 301 to the apex, keeping the path and the query', () => {
  const r = request('/books/the-martian/', { host: 'www.rwbookclub.com', querystring: { ref: { value: 'old' } } });
  assert.equal(r.statusCode, 301);
  assert.equal(r.headers.location.value, 'https://rwbookclub.com/books/the-martian/?ref=old');
  assert.equal(request('/', { host: 'WWW.rwbookclub.com' }).headers.location.value, 'https://rwbookclub.com/');
  assert.equal(request('/feed.xml', { host: 'www.rwbookclub.com' }).headers.location.value, 'https://rwbookclub.com/feed.xml');
  const multi = request('/about', {
    host: 'www.rwbookclub.com',
    querystring: { a: { value: '1', multiValue: [{ value: '1' }, { value: '2' }] }, b: { value: '' } }
  });
  assert.equal(multi.headers.location.value, 'https://rwbookclub.com/about?a=1&a=2&b');
});

test('directory URLs serve index.html; a bare path gets its slash; files pass', () => {
  assert.equal(request('/').uri, '/index.html');
  assert.equal(request('/books/the-martian/').uri, '/books/the-martian/index.html');
  const bare = request('/meetings', { querystring: { ref: { value: 'x' } } });
  assert.equal(bare.statusCode, 301);
  assert.equal(bare.headers.location.value, '/meetings/?ref=x');
  for (const file of ['/feed.xml', '/books/the-martian.txt', '/meetings.ics', '/assets/images/covers/x.jpg'])
    assert.equal(request(file).uri, file);
  // The CloudFront domain itself is served, so the site can be checked before DNS moves.
  assert.equal(request('/', { host: 'd111111abcdef8.cloudfront.net' }).uri, '/index.html');
});

test('every kind of file the build writes has its own type', () => {
  assert.equal(contentTypeFor('index.html'), 'text/html; charset=utf-8');
  assert.equal(contentTypeFor('feed.xml'), 'application/xml; charset=utf-8');
  assert.equal(contentTypeFor('sitemap.xml'), 'application/xml; charset=utf-8');
  assert.equal(contentTypeFor('meetings.ics'), 'text/calendar; charset=utf-8');
  assert.equal(contentTypeFor('books/the-martian.txt'), 'text/plain; charset=utf-8');
  assert.equal(contentTypeFor('books.json'), 'application/json; charset=utf-8');
  assert.equal(contentTypeFor('assets/images/covers/x.JPG'), 'image/jpeg');
  assert.equal(contentTypeFor('assets/fonts/fraunces-latin-wght-normal.woff2'), 'font/woff2');
  assert.equal(contentTypeFor('assets/favicon.svg'), 'image/svg+xml');
  assert.equal(contentTypeFor('mystery.bin'), undefined);
});

test('GitHub Pages bookkeeping and the workflow are not published', () => {
  for (const key of ['CNAME', '.nojekyll', '.git', '.github/workflows/deploy-aws.yml', '.git/HEAD']) assert.ok(!isPublished(key), key);
  assert.ok(isPublished('index.html'));
  assert.equal(cacheControlFor('index.html'), 'public, max-age=300, s-maxage=86400');
  assert.equal(cacheControlFor('assets/fonts/fraunces-latin-wght-normal.woff2'), 'public, max-age=604800');
});

test('the certificate and the distribution cover the apex and www', () => {
  const cert = certificate.Resources.Certificate.Properties;
  assert.equal(cert.DomainName, 'rwbookclub.com');
  assert.deepEqual(cert.SubjectAlternativeNames, ['www.rwbookclub.com']);
  assert.deepEqual(ALIASES, ['rwbookclub.com', 'www.rwbookclub.com']);
  assert.deepEqual(site.Resources.Distribution.Properties.DistributionConfig.Aliases['Fn::If'][1], ALIASES);
});

test('the delivery role trusts the gh-pages branch and nothing else', () => {
  const condition = delivery.Resources.DeploymentRole.Properties.AssumeRolePolicyDocument.Statement[0].Condition.StringEquals;
  assert.deepEqual(condition['token.actions.githubusercontent.com:sub'], DEPLOY_SUBJECTS);
  for (const subject of DEPLOY_SUBJECTS) assert.match(subject, /:ref:refs\/heads\/gh-pages$/);
  assert.equal(condition['token.actions.githubusercontent.com:aud'], 'sts.amazonaws.com');
});

test('access logs keep 24 months with no client IP and no query string', () => {
  assert.equal(site.Resources.AccessLogGroup.Properties.LogGroupName, '/rwbookclub/web-access');
  assert.equal(site.Resources.AccessLogGroup.Properties.RetentionInDays, 731);
  assert.equal(site.Resources.AccessLogDestination.Properties.OutputFormat, 'json');
  assert.ok(!ACCESS_LOG_FIELDS.includes('c-ip') && !ACCESS_LOG_FIELDS.includes('cs-uri-query'));
});

test('the committed templates are what infra/templates.mjs writes', () => {
  for (const [name, body] of Object.entries({ certificate, site, delivery }))
    assert.deepEqual(JSON.parse(read(`infra/generated/${name}.json`)), body, `run npm run infra:generate (${name})`);
});

test('every build carries the gh-pages workflow, which publishes each push to S3', () => {
  const workflow = read('website/gh-pages/deploy-aws.yml');
  assert.match(workflow, /^on:\n  push:\n    branches: \[gh-pages\]\n/m);
  assert.match(workflow, /if: github\.ref == 'refs\/heads\/gh-pages'/);
  assert.match(workflow, /id-token: write/);
  assert.match(workflow, /role-to-assume: \$\{\{ vars\.AWS_DEPLOY_ROLE_ARN \}\}/);
  assert.match(workflow, /ref: main\n\s+path: source/);
  assert.match(workflow, /run: node scripts\/deploy-aws\.mjs --site \.\.\/site/);
  // agent.publish pushes website/_site as an orphan commit, so the copy into the build is what keeps it there.
  assert.match(
    read('website/.eleventy.js'),
    /addPassthroughCopy\(\{ "gh-pages\/deploy-aws\.yml": "\.github\/workflows\/deploy-aws\.yml" \}\)/
  );
  // Nothing on main is a workflow for this: a push to gh-pages runs only the workflows on gh-pages.
  assert.ok(!existsSync(new URL('.github/workflows/deploy-aws.yml', root)));
});

const built = new URL('website/_site/', root);
const required = Boolean(process.env.BUILT_SITE_REQUIRED);
test('the built site carries the workflow and types every file', { skip: !required && !existsSync(new URL('index.html', built)) && 'no build' }, () => {
  assert.equal(read('website/_site/.github/workflows/deploy-aws.yml'), read('website/gh-pages/deploy-aws.yml'));
  const plan = publishPlan(built.pathname);
  assert.ok(plan.length > 0);
  assert.deepEqual(plan.filter((file) => !file.contentType).map((file) => file.key), []);
  assert.ok(!plan.some((file) => file.key.startsWith('.github/') || file.key === 'CNAME'));
});
