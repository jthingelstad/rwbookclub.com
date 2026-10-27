// Deploy one of the three stacks in infra/generated/ from this Mac, as the cloud-engineer identity
// (it refuses any other), through a change set it prints before executing.
//
//   npm run infra -- certificate   the certificate; prints both validation CNAMEs (apex and www) for Namecheap
//   npm run infra -- site          the site; gives the distribution rwbookclub.com and www.rwbookclub.com
//                                  once the certificate is ISSUED
//   npm run infra -- delivery      the role the gh-pages workflow assumes; its ARN is the AWS_DEPLOY_ROLE_ARN
//                                  repository variable
//   add --plan to print the change set and stop
//
// Each run records the stack's outputs in infra/deployment.json.
import { ACMClient, DescribeCertificateCommand } from '@aws-sdk/client-acm';
import {
  CloudFormationClient,
  CreateChangeSetCommand,
  DeleteChangeSetCommand,
  DescribeChangeSetCommand,
  DescribeStackResourceCommand,
  DescribeStacksCommand,
  ExecuteChangeSetCommand,
  waitUntilChangeSetCreateComplete,
  waitUntilStackCreateComplete,
  waitUntilStackUpdateComplete
} from '@aws-sdk/client-cloudformation';
import { GetCallerIdentityCommand, STSClient } from '@aws-sdk/client-sts';
import { readFileSync, writeFileSync } from 'node:fs';
import { setTimeout as delay } from 'node:timers/promises';

const ROOT = new URL('../', import.meta.url);
const CONFIG = new URL('infra/deployment.json', ROOT);
const config = JSON.parse(readFileSync(CONFIG, 'utf8'));
const [name] = process.argv.slice(2).filter((arg) => !arg.startsWith('--'));
const planOnly = process.argv.includes('--plan');
if (!['certificate', 'site', 'delivery'].includes(name)) throw new Error('Usage: npm run infra -- certificate|site|delivery [--plan]');

const region = config.region;
const identity = await new STSClient({ region }).send(new GetCallerIdentityCommand({}));
if (identity.Account !== config.account || !identity.Arn.includes(':assumed-role/ProjectsCloudEngineer/'))
  throw new Error(`Run as the cloud-engineer identity, not ${identity.Arn}`);

const cfn = new CloudFormationClient({ region, maxAttempts: 5 });
const StackName = config.stacks[name];
const describe = async (stackName) =>
  (await cfn.send(new DescribeStacksCommand({ StackName: stackName })).catch(() => ({ Stacks: [] }))).Stacks[0];
const outputs = (stack) => Object.fromEntries((stack?.Outputs ?? []).map((o) => [o.OutputKey, o.OutputValue]));

let parameters = [];
if (name === 'site') {
  let arn = '';
  if (config.certificateArn) {
    const cert = await new ACMClient({ region }).send(new DescribeCertificateCommand({ CertificateArn: config.certificateArn }));
    arn = cert.Certificate.Status === 'ISSUED' ? config.certificateArn : '';
    console.log(`Certificate ${cert.Certificate.Status}${arn ? '' : ': the distribution stays on its CloudFront domain'}`);
  }
  parameters = [{ ParameterKey: 'CertificateArn', ParameterValue: arn }];
}
if (name === 'delivery') {
  const site = outputs(await describe(config.stacks.site));
  parameters = Object.entries({
    SiteStack: config.stacks.site,
    SiteBucket: site.SiteBucket,
    DistributionId: site.DistributionId
  }).map(([ParameterKey, ParameterValue]) => ({ ParameterKey, ParameterValue }));
}

// A plan run against a stack that does not exist yet leaves a REVIEW_IN_PROGRESS placeholder, which
// only a CREATE change set can use. A first create that failed leaves ROLLBACK_COMPLETE, which nothing
// can update: the stack has to be deleted first.
const found = await describe(StackName);
if (found?.StackStatus === 'ROLLBACK_COMPLETE')
  throw new Error(`${StackName} is ROLLBACK_COMPLETE from a failed first create. Delete the stack, then rerun.`);
const existing = found?.StackStatus === 'REVIEW_IN_PROGRESS' ? undefined : found;
const ChangeSetName = `cloud-engineer-${Date.now()}`;
await cfn.send(
  new CreateChangeSetCommand({
    StackName,
    ChangeSetName,
    ChangeSetType: existing ? 'UPDATE' : 'CREATE',
    TemplateBody: readFileSync(new URL(`infra/generated/${name}.json`, ROOT), 'utf8'),
    Parameters: parameters,
    Capabilities: name === 'delivery' ? ['CAPABILITY_IAM'] : [],
    Tags: Object.entries(config.stackTags).map(([Key, Value]) => ({ Key, Value }))
  })
);
try {
  await waitUntilChangeSetCreateComplete({ client: cfn, maxWaitTime: 300 }, { StackName, ChangeSetName });
} catch {
  const failed = await cfn.send(new DescribeChangeSetCommand({ StackName, ChangeSetName }));
  if (/didn't contain changes|No updates/.test(failed.StatusReason ?? '')) {
    await cfn.send(new DeleteChangeSetCommand({ StackName, ChangeSetName }));
    console.log(`${StackName}: no changes`);
  } else throw new Error(`${StackName}: ${failed.StatusReason}`);
}
const set = await cfn.send(new DescribeChangeSetCommand({ StackName, ChangeSetName })).catch(() => null);
if (set) {
  for (const { ResourceChange: c } of set.Changes ?? [])
    console.log(`${c.Action} ${c.LogicalResourceId} (${c.ResourceType})${c.Replacement === 'True' ? ' REPLACEMENT' : ''}`);
  if (planOnly) {
    await cfn.send(new DeleteChangeSetCommand({ StackName, ChangeSetName }));
    process.exit(0);
  }
  await cfn.send(new ExecuteChangeSetCommand({ StackName, ChangeSetName }));
  // A new certificate waits on its validation record, so its stack is not waited on.
  if (name !== 'certificate')
    await (existing ? waitUntilStackUpdateComplete : waitUntilStackCreateComplete)({ client: cfn, maxWaitTime: 1800 }, { StackName });
}

if (name === 'certificate') {
  // The certificate exists before the stack completes; read its validation record from ACM.
  for (let i = 0; i < 30; i++) {
    const resource = await cfn
      .send(new DescribeStackResourceCommand({ StackName, LogicalResourceId: 'Certificate' }))
      .catch(() => null);
    const certArn = resource?.StackResourceDetail?.PhysicalResourceId;
    if (certArn?.startsWith('arn:')) {
      const cert = (await new ACMClient({ region }).send(new DescribeCertificateCommand({ CertificateArn: certArn }))).Certificate;
      // One record per name (apex and www); wait until ACM has written both.
      const options = cert.DomainValidationOptions ?? [];
      if (options.length >= 2 && options.every((o) => o.ResourceRecord)) {
        config.certificateArn = certArn;
        console.log(`Certificate ${cert.Status}. Validation records for Namecheap:`);
        for (const o of options)
          console.log(`  ${o.DomainName}: ${o.ResourceRecord.Type} ${o.ResourceRecord.Name} -> ${o.ResourceRecord.Value}`);
        break;
      }
    }
    await delay(10_000);
  }
} else {
  const out = outputs(await describe(StackName));
  if (name === 'site')
    Object.assign(config, {
      siteBucket: out.SiteBucket,
      distributionId: out.DistributionId,
      cloudfrontDomain: out.CloudFrontDomain
    });
  if (name === 'delivery') config.deploymentRoleArn = out.DeploymentRoleArn;
  console.log(JSON.stringify(out, null, 2));
}
writeFileSync(CONFIG, JSON.stringify(config, null, 2) + '\n');
