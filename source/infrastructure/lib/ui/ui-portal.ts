/**********************************************************************************************************************
 *  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.                                                *
 *                                                                                                                    *
 *  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance    *
 *  with the License. A copy of the License is located at                                                             *
 *                                                                                                                    *
 *      http://www.apache.org/licenses/LICENSE-2.0                                                                    *
 *                                                                                                                    *
 *  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES *
 *  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions    *
 *  and limitations under the License.                                                                                *
 *********************************************************************************************************************/

import { Construct } from "constructs";
import {
  aws_cloudfront as cloudfront,
  aws_s3 as s3,
  aws_s3_deployment as s3d,
  RemovalPolicy
} from "aws-cdk-lib";

export interface PortalConstructOutputs {
  portalBucket: s3.Bucket;
  portalUrl: string;
}
/**
 * Construct to provision Portal assets and CloudFront Distribution
 */
export class PortalConstruct extends Construct implements PortalConstructOutputs {
  public portalBucket: s3.Bucket;
  public portalUrl: string;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    // Create S3 bucket
    this.portalBucket = new s3.Bucket(this, 'PortalBucket', {
      versioned: false,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.RETAIN,
      autoDeleteObjects: false,
      objectOwnership: s3.ObjectOwnership.OBJECT_WRITER,
    });

    // Create Origin Access Identity
    const oai = new cloudfront.OriginAccessIdentity(this, 'MyOriginAccessIdentity');
    this.portalBucket.grantRead(oai);

    // Create CloudFront distribution
    const distribution = new cloudfront.CfnDistribution(this, 'MyCloudfrontDistribution', {
      distributionConfig: {
        defaultCacheBehavior: {
          targetOriginId: this.portalBucket.bucketName,
          viewerProtocolPolicy: 'redirect-to-https',
          allowedMethods: ['GET', 'HEAD', 'OPTIONS'],
          cachedMethods: ['GET', 'HEAD', 'OPTIONS'],
          forwardedValues: {
            queryString: false,
            cookies: { forward: 'none' },
          },
          minTtl: 0,
          defaultTtl: 3600,
          maxTtl: 86400,
        },
        enabled: true,
        httpVersion: 'http2',
        defaultRootObject: 'index.html',
        ipv6Enabled: false,
        priceClass: 'PriceClass_All',
        origins: [
          {
            id: this.portalBucket.bucketName,
            domainName: this.portalBucket.bucketRegionalDomainName,
            s3OriginConfig: {
              originAccessIdentity: `origin-access-identity/cloudfront/${oai.originAccessIdentityId}`,
            },
          },
        ],
        viewerCertificate: {
          cloudFrontDefaultCertificate: true,
          minimumProtocolVersion: 'TLSv1.2_2021',
        },
      },
    });

    this.portalUrl = distribution.attrDomainName;

    // Upload static web assets
    new s3d.BucketDeployment(this, "DeployWebAssets", {
      sources: [s3d.Source.asset("../portal/dist")],
      destinationBucket: this.portalBucket,
      prune: false,
    });
  }
}
