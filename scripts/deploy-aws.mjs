// Publish Oliver's built site (a checkout of the gh-pages branch) to S3 behind CloudFront: the files
// that changed, the files the build no longer writes removed, then the cache cleared and the result read
// back through CloudFront.
//
// The gh-pages branch carries .github/workflows/deploy-aws.yml (website/gh-pages/deploy-aws.yml on main,
// copied into every build by website/.eleventy.js), so each push agent.publish makes runs this as the
// delivery role. It also runs from this Mac as the cloud-engineer identity. Either way it refuses to
// publish when the site stack's live template differs from infra/generated/site.json: infrastructure
// changes go out first, on their own (`npm run infra -- site`).
//
//   node scripts/deploy-aws.mjs --site <dir>          upload what changed (by MD5), remove what is gone
//   node scripts/deploy-aws.mjs --site <dir> --all    upload every file, after a change to publish-plan.mjs
import { CloudFormationClient, DescribeStacksCommand, GetTemplateCommand } from '@aws-sdk/client-cloudformation';
import { CloudFrontClient, CreateInvalidationCommand, waitUntilInvalidationCompleted } from '@aws-sdk/client-cloudfront';
import { DeleteObjectsCommand, ListObjectsV2Command, PutObjectCommand, S3Client } from '@aws-sdk/client-s3';
import { GetCallerIdentityCommand, STSClient } from '@aws-sdk/client-sts';
import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { isDeepStrictEqual } from 'node:util';
import { setTimeout as delay } from 'node:timers/promises';
import { publishPlan } from './publish-plan.mjs';

const REPO = new URL('../', import.meta.url);
const flag = process.argv.indexOf('--site');
if (flag === -1 || !process.argv[flag + 1]) throw new Error('Usage: node scripts/deploy-aws.mjs --site <gh-pages checkout> [--all]');
const SITE = resolve(process.argv[flag + 1]);
const all = process.argv.includes('--all');
const deployment = JSON.parse(readFileSync(new URL('infra/deployment.json', REPO), 'utf8'));
const region = deployment.region;

const identity = await new STSClient({ region }).send(new GetCallerIdentityCommand({}));
const allowed = [':assumed-role/ProjectsCloudEngineer/', ':assumed-role/rwbookclub-delivery-DeploymentRole-'];
if (identity.Account !== deployment.account || !allowed.some((role) => identity.Arn.includes(role)))
  throw new Error(`Unexpected AWS identity ${identity.Arn}`);

const cloudformation = new CloudFormationClient({ region });
const stack = (await cloudformation.send(new DescribeStacksCommand({ StackName: deployment.stacks.site }))).Stacks[0];
if (!['CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE'].includes(stack.StackStatus))
  throw new Error(`${deployment.stacks.site} is not ready: ${stack.StackStatus}`);
const live = (await cloudformation.send(new GetTemplateCommand({ StackName: deployment.stacks.site }))).TemplateBody;
if (!isDeepStrictEqual(JSON.parse(live), JSON.parse(readFileSync(new URL('infra/generated/site.json', REPO), 'utf8'))))
  throw new Error('The site stack differs from infra/generated/site.json. Deploy the infrastructure change first.');
const out = Object.fromEntries(stack.Outputs.map((o) => [o.OutputKey, o.OutputValue]));
const bucket = out.SiteBucket;
const preview = `https://${out.CloudFrontDomain}`;

// The same floor agent.publish guards with, and the core files: a partial build is not published.
const plan = publishPlan(SITE);
const keys = new Set(plan.map((file) => file.key));
const required = [
  'index.html', '404.html', 'feed.xml', 'sitemap.xml', 'robots.txt', 'llms.txt', 'meetings.ics',
  'books.json', 'next.json', 'about/index.html', 'meetings/index.html', 'assets/css/styles.css'
];
const absent = required.filter((key) => !keys.has(key));
const bookPages = plan.filter((file) => /^books\/[^/]+\/index\.html$/.test(file.key)).length;
if (absent.length || bookPages < 50)
  throw new Error(`The build is incomplete: ${absent.join(', ') || `${bookPages} book pages`}`);
const untyped = plan.filter((file) => !file.contentType).map((file) => file.key);
if (untyped.length) throw new Error(`No content type for ${untyped.slice(0, 5).join(', ')} (scripts/publish-plan.mjs)`);
if (!existsSync(resolve(SITE, '.github/workflows/deploy-aws.yml')))
  console.warn('This tree carries no .github/workflows/deploy-aws.yml; the next push from it would not reach S3.');

// Upload what changed. A single-part upload's ETag is its MD5.
const s3 = new S3Client({ region, maxAttempts: 5 });
const existing = new Map();
let token;
do {
  const page = await s3.send(new ListObjectsV2Command({ Bucket: bucket, ContinuationToken: token }));
  for (const object of page.Contents ?? []) existing.set(object.Key, object.ETag?.replaceAll('"', ''));
  token = page.IsTruncated ? page.NextContinuationToken : undefined;
} while (token);
const md5 = (path) => createHash('md5').update(readFileSync(path)).digest('hex');
const changed = plan.filter((file) => all || existing.get(file.key) !== md5(file.path));
// Files before pages and the front page last (publishPlan's order), a batch at a time.
for (let i = 0; i < changed.length; i += 16) {
  await Promise.all(
    changed.slice(i, i + 16).map((file) =>
      s3.send(
        new PutObjectCommand({
          Bucket: bucket,
          Key: file.key,
          Body: readFileSync(file.path),
          ContentType: file.contentType,
          CacheControl: file.cacheControl
        })
      )
    )
  );
}
console.log(`Uploaded ${changed.length} of ${plan.length} files.`);

// Remove what the build no longer writes. The bucket keeps old versions for 90 days.
const stale = [...existing.keys()].filter((key) => !keys.has(key));
for (let i = 0; i < stale.length; i += 1000) {
  await s3.send(
    new DeleteObjectsCommand({
      Bucket: bucket,
      Delete: { Objects: stale.slice(i, i + 1000).map((Key) => ({ Key })), Quiet: true }
    })
  );
}
console.log(`Removed ${stale.length} files the build no longer writes.`);

const cloudfront = new CloudFrontClient({ region: 'us-east-1' });
const invalidation = await cloudfront.send(
  new CreateInvalidationCommand({
    DistributionId: out.DistributionId,
    InvalidationBatch: {
      CallerReference: `${process.env.GITHUB_SHA ?? 'local'}-${Date.now()}`,
      Paths: { Quantity: 1, Items: ['/*'] }
    }
  })
);
await waitUntilInvalidationCompleted(
  { client: cloudfront, maxWaitTime: 900 },
  { DistributionId: out.DistributionId, Id: invalidation.Invalidation.Id }
);

// Read it back through the distribution: the front page as built, a book page, the feed and calendar
// with their types, the slash form and a missing page.
const sha = (bytes) => createHash('sha256').update(bytes).digest('hex');
const book = plan.find((file) => /^books\/[^/]+\/index\.html$/.test(file.key)).key.replace(/index\.html$/, '');
const typed = (path, type) => async () => {
  const r = await fetch(`${preview}${path}`);
  return (r.ok && r.headers.get('content-type')?.startsWith(type)) || `${path} is ${r.status} ${r.headers.get('content-type')}`;
};
const checks = [
  async () => {
    const r = await fetch(`${preview}/`);
    return (r.ok && sha(Buffer.from(await r.arrayBuffer())) === sha(readFileSync(resolve(SITE, 'index.html')))) || 'front page differs from the build';
  },
  typed(`/${book}`, 'text/html'),
  typed('/feed.xml', 'application/xml'),
  typed('/meetings.ics', 'text/calendar'),
  async () => {
    const r = await fetch(`${preview}/about?ref=check`, { redirect: 'manual' });
    return (r.status === 301 && r.headers.get('location') === '/about/?ref=check') || `/about is ${r.status} ${r.headers.get('location')}`;
  },
  async () => {
    const r = await fetch(`${preview}/books/not-a-book/`);
    return (r.status === 404 && r.headers.get('content-type')?.startsWith('text/html')) || `a missing page is ${r.status}`;
  }
];
let failures = [];
for (let attempt = 0; attempt < 8; attempt++) {
  failures = (await Promise.all(checks.map((check) => check().catch((error) => error.message)))).filter((r) => r !== true);
  if (!failures.length) break;
  await delay(10_000);
}
if (failures.length) throw new Error(`Published site check failed: ${failures.join('; ')}`);
console.log(`Verified ${process.env.GITHUB_SHA ?? 'local tree'} at ${preview}`);
