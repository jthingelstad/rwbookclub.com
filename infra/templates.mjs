// The CloudFormation templates for rwbookclub.com on S3 and CloudFront, written to infra/generated/ by
// `npm run infra:generate`. Three stacks, each deployed from this Mac as the cloud-engineer identity
// (`npm run infra -- <stack>`), never from CI:
//
//   certificate  rwbookclub.com with www.rwbookclub.com, validated by two CNAMEs at Namecheap
//   site         the bucket, the distribution and its function, headers, access logs, 5xx alarm
//   delivery     the role the gh-pages workflow assumes to publish Oliver's build (scripts/deploy-aws.mjs)
//
// Oliver builds the site on this Mac and force-pushes it to the gh-pages branch (agent/publish.py). That
// push carries .github/workflows/deploy-aws.yml (copied in by website/.eleventy.js), which publishes the
// branch to the bucket. The delivery role trusts the gh-pages branch only.
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DOMAIN, viewerRequestCode } from './viewer-request.mjs';

export { DOMAIN };
export const ALIASES = [DOMAIN, `www.${DOMAIN}`];
export const REPOSITORY = 'jthingelstad/rwbookclub.com';
// GitHub's immutable subject (owner and repository ids) and the named form, the gh-pages branch only.
export const DEPLOY_SUBJECTS = [
  'repo:jthingelstad@5351/rwbookclub.com@1206438732:ref:refs/heads/gh-pages',
  `repo:${REPOSITORY}:ref:refs/heads/gh-pages`
];
// Never c-ip (the reader) or cs-uri-query (whatever a link carried).
export const ACCESS_LOG_FIELDS = [
  'timestamp(ms)',
  'cs-method',
  'cs-uri-stem',
  'sc-status',
  'sc-bytes',
  'sc-range-start',
  'sc-range-end',
  'time-taken',
  'time-to-first-byte',
  'x-edge-result-type',
  'x-edge-detailed-result-type',
  'x-edge-location',
  'cs-protocol',
  'cs(User-Agent)'
];

const ref = (name) => ({ Ref: name });
const sub = (value) => ({ 'Fn::Sub': value });
const get = (name, field) => ({ 'Fn::GetAtt': [name, field] });
const custom = (a, b) => ({ 'Fn::If': ['CustomDomain', a, b] });
// The Thingelstad family alarm topic, subscribed to the shared projects-ops-alerts queue.
const alarmTopic = sub('arn:${AWS::Partition}:sns:us-east-1:${AWS::AccountId}:weekly-thing-librarian-alarms');
// AWS managed CachingOptimized: no cookies, headers or query strings in the key, so ?ref= variants share
// one cached copy; it honours the Cache-Control each object is uploaded with.
const CACHING_OPTIMIZED = '658327ea-f89d-4fab-a63d-7e88639e58f6';

const template = (Description, Resources, Outputs, extra = {}) => ({
  AWSTemplateFormatVersion: '2010-09-09',
  Description,
  ...extra,
  Resources,
  Outputs
});
const output = (Value) => ({ Value });

export const certificate = template(
  `${DOMAIN} and www.${DOMAIN} HTTPS certificate - external DNS validation`,
  {
    Certificate: {
      Type: 'AWS::CertificateManager::Certificate',
      Properties: { DomainName: DOMAIN, SubjectAlternativeNames: [`www.${DOMAIN}`], ValidationMethod: 'DNS' }
    }
  },
  { CertificateArn: output(ref('Certificate')) }
);

export const site = template(
  `${DOMAIN} static site - the R/W Book Club, built by Oliver`,
  {
    // Versioned, so an object a deploy deletes or overwrites can be restored.
    SiteBucket: {
      Type: 'AWS::S3::Bucket',
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain',
      Properties: {
        PublicAccessBlockConfiguration: {
          BlockPublicAcls: true,
          BlockPublicPolicy: true,
          IgnorePublicAcls: true,
          RestrictPublicBuckets: true
        },
        BucketEncryption: {
          ServerSideEncryptionConfiguration: [{ ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' } }]
        },
        OwnershipControls: { Rules: [{ ObjectOwnership: 'BucketOwnerEnforced' }] },
        VersioningConfiguration: { Status: 'Enabled' },
        LifecycleConfiguration: {
          Rules: [{ Id: 'old-versions', Status: 'Enabled', NoncurrentVersionExpiration: { NoncurrentDays: 90 } }]
        }
      }
    },
    SitePolicy: {
      Type: 'AWS::S3::BucketPolicy',
      Properties: {
        Bucket: ref('SiteBucket'),
        PolicyDocument: {
          Version: '2012-10-17',
          Statement: [
            {
              Effect: 'Allow',
              Principal: { Service: 'cloudfront.amazonaws.com' },
              Action: 's3:GetObject',
              Resource: sub('${SiteBucket.Arn}/*'),
              Condition: {
                StringEquals: {
                  'AWS:SourceArn': sub('arn:${AWS::Partition}:cloudfront::${AWS::AccountId}:distribution/${Distribution}')
                }
              }
            }
          ]
        }
      }
    },
    OriginAccess: {
      Type: 'AWS::CloudFront::OriginAccessControl',
      Properties: {
        OriginAccessControlConfig: {
          Name: sub('${AWS::StackName}-s3'),
          OriginAccessControlOriginType: 's3',
          SigningBehavior: 'always',
          SigningProtocol: 'sigv4'
        }
      }
    },
    ViewerRequest: {
      Type: 'AWS::CloudFront::Function',
      Properties: {
        Name: sub('${AWS::StackName}-viewer-request'),
        AutoPublish: true,
        FunctionConfig: {
          Comment: 'www to the apex, the slash form, and index.html for directory URLs',
          Runtime: 'cloudfront-js-2.0'
        },
        FunctionCode: viewerRequestCode
      }
    },
    // GitHub Pages, the host before this, answered every file with Access-Control-Allow-Origin: *, so this
    // keeps that for the feeds, calendar and JSON other sites fetch, and adds the security headers.
    Headers: {
      Type: 'AWS::CloudFront::ResponseHeadersPolicy',
      Properties: {
        ResponseHeadersPolicyConfig: {
          Name: sub('${AWS::StackName}-headers'),
          CorsConfig: {
            AccessControlAllowCredentials: false,
            AccessControlAllowHeaders: { Items: ['*'] },
            AccessControlAllowMethods: { Items: ['GET', 'HEAD'] },
            AccessControlAllowOrigins: { Items: ['*'] },
            OriginOverride: true
          },
          SecurityHeadersConfig: {
            ContentTypeOptions: { Override: true },
            FrameOptions: { FrameOption: 'SAMEORIGIN', Override: true },
            ReferrerPolicy: { ReferrerPolicy: 'strict-origin-when-cross-origin', Override: true },
            StrictTransportSecurity: { AccessControlMaxAgeSec: 31536000, Override: true }
          }
        }
      }
    },
    Distribution: {
      Type: 'AWS::CloudFront::Distribution',
      Properties: {
        DistributionConfig: {
          Enabled: true,
          Comment: `${DOMAIN} - R/W Book Club`,
          PriceClass: 'PriceClass_100',
          IPV6Enabled: true,
          HttpVersion: 'http2and3',
          DefaultRootObject: 'index.html',
          Aliases: custom(ALIASES, ref('AWS::NoValue')),
          ViewerCertificate: custom(
            { AcmCertificateArn: ref('CertificateArn'), SslSupportMethod: 'sni-only', MinimumProtocolVersion: 'TLSv1.2_2021' },
            { CloudFrontDefaultCertificate: true }
          ),
          Origins: [
            {
              Id: 'site',
              DomainName: get('SiteBucket', 'RegionalDomainName'),
              OriginAccessControlId: ref('OriginAccess'),
              S3OriginConfig: { OriginAccessIdentity: '' }
            }
          ],
          DefaultCacheBehavior: {
            TargetOriginId: 'site',
            ViewerProtocolPolicy: 'redirect-to-https',
            AllowedMethods: ['GET', 'HEAD', 'OPTIONS'],
            CachedMethods: ['GET', 'HEAD'],
            Compress: true,
            CachePolicyId: CACHING_OPTIMIZED,
            ResponseHeadersPolicyId: ref('Headers'),
            FunctionAssociations: [{ EventType: 'viewer-request', FunctionARN: get('ViewerRequest', 'FunctionARN') }]
          },
          // A missing object is a 403 from S3 behind OAC (no ListBucket). Both become the site's 404 page.
          CustomErrorResponses: [
            { ErrorCode: 403, ResponseCode: 404, ResponsePagePath: '/404.html', ErrorCachingMinTTL: 60 },
            { ErrorCode: 404, ResponseCode: 404, ResponsePagePath: '/404.html', ErrorCachingMinTTL: 60 }
          ]
        }
      }
    },
    DistributionErrorAlarm: {
      Type: 'AWS::CloudWatch::Alarm',
      Properties: {
        AlarmName: sub('${AWS::StackName}-distribution-5xx'),
        AlarmDescription: `CloudFront returned more than 5% 5xx responses for ${DOMAIN} in two of three five-minute windows.`,
        Namespace: 'AWS/CloudFront',
        MetricName: '5xxErrorRate',
        Dimensions: [
          { Name: 'DistributionId', Value: ref('Distribution') },
          { Name: 'Region', Value: 'Global' }
        ],
        Statistic: 'Average',
        Period: 300,
        EvaluationPeriods: 3,
        DatapointsToAlarm: 2,
        Threshold: 5,
        ComparisonOperator: 'GreaterThanThreshold',
        TreatMissingData: 'notBreaching',
        AlarmActions: [alarmTopic]
      }
    },
    // Access logs, the same shape as the other sites': CloudFront's standard logging into CloudWatch
    // Logs, kept 14 days, JSON, with no client IP and no query string.
    AccessLogGroup: {
      Type: 'AWS::Logs::LogGroup',
      Properties: { LogGroupName: '/rwbookclub/web-access', RetentionInDays: 14 }
    },
    AccessLogSource: {
      Type: 'AWS::Logs::DeliverySource',
      Properties: {
        Name: 'rwbookclub-web-access',
        LogType: 'ACCESS_LOGS',
        ResourceArn: sub('arn:${AWS::Partition}:cloudfront::${AWS::AccountId}:distribution/${Distribution}')
      }
    },
    AccessLogDestination: {
      Type: 'AWS::Logs::DeliveryDestination',
      Properties: {
        Name: 'rwbookclub-web-access',
        DestinationResourceArn: get('AccessLogGroup', 'Arn'),
        OutputFormat: 'json'
      }
    },
    AccessLogDelivery: {
      Type: 'AWS::Logs::Delivery',
      DependsOn: 'AccessLogSource',
      Properties: {
        DeliverySourceName: 'rwbookclub-web-access',
        DeliveryDestinationArn: get('AccessLogDestination', 'Arn'),
        RecordFields: ACCESS_LOG_FIELDS
      }
    }
  },
  {
    SiteBucket: output(ref('SiteBucket')),
    DistributionId: output(ref('Distribution')),
    CloudFrontDomain: output(get('Distribution', 'DomainName'))
  },
  {
    Parameters: { CertificateArn: { Type: 'String', Default: '' } },
    Conditions: { CustomDomain: { 'Fn::Not': [{ 'Fn::Equals': [ref('CertificateArn'), ''] }] } }
  }
);

// Application delivery only: publish the gh-pages build into the bucket and clear the cache.
// Infrastructure changes stay a separate, local deployment.
export const delivery = template(
  `${DOMAIN} GitHub application delivery from the gh-pages branch`,
  {
    DeploymentRole: {
      Type: 'AWS::IAM::Role',
      Properties: {
        AssumeRolePolicyDocument: {
          Version: '2012-10-17',
          Statement: [
            {
              Effect: 'Allow',
              Principal: {
                Federated: sub('arn:${AWS::Partition}:iam::${AWS::AccountId}:oidc-provider/token.actions.githubusercontent.com')
              },
              Action: 'sts:AssumeRoleWithWebIdentity',
              Condition: {
                StringEquals: {
                  'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
                  'token.actions.githubusercontent.com:sub': DEPLOY_SUBJECTS
                }
              }
            }
          ]
        },
        Policies: [
          {
            PolicyName: 'ApplicationDelivery',
            PolicyDocument: {
              Version: '2012-10-17',
              Statement: [
                {
                  Effect: 'Allow',
                  Action: ['s3:ListBucket', 's3:GetBucketLocation'],
                  Resource: sub('arn:${AWS::Partition}:s3:::${SiteBucket}')
                },
                {
                  // Delete removes pages the build no longer writes; the bucket keeps 90 days of versions.
                  Effect: 'Allow',
                  Action: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject'],
                  Resource: sub('arn:${AWS::Partition}:s3:::${SiteBucket}/*')
                },
                {
                  Effect: 'Allow',
                  Action: ['cloudfront:CreateInvalidation', 'cloudfront:GetInvalidation', 'cloudfront:GetDistribution'],
                  Resource: sub('arn:${AWS::Partition}:cloudfront::${AWS::AccountId}:distribution/${DistributionId}')
                },
                {
                  // Read-only: the deploy refuses to publish unless the live template matches the generated one.
                  Effect: 'Allow',
                  Action: ['cloudformation:DescribeStacks', 'cloudformation:GetTemplate'],
                  Resource: sub('arn:${AWS::Partition}:cloudformation:${AWS::Region}:${AWS::AccountId}:stack/${SiteStack}/*')
                }
              ]
            }
          }
        ]
      }
    }
  },
  { DeploymentRoleArn: output(get('DeploymentRole', 'Arn')) },
  {
    Parameters: {
      SiteStack: { Type: 'String' },
      SiteBucket: { Type: 'String' },
      DistributionId: { Type: 'String' }
    }
  }
);

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const out = join(dirname(fileURLToPath(import.meta.url)), 'generated');
  mkdirSync(out, { recursive: true });
  for (const [name, body] of Object.entries({ certificate, site, delivery }))
    writeFileSync(join(out, `${name}.json`), JSON.stringify(body, null, 2) + '\n');
}
