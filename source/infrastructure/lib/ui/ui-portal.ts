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
  aws_apigatewayv2 as apigwv2,
  aws_lambda as lambda,
  RemovalPolicy,
  Stack,
  NestedStack,
} from "aws-cdk-lib";
import { HttpLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";

export class PortalStack extends NestedStack {
  public portalConstruct: PortalConstruct;

  constructor(scope: Construct, id: string) {
    super(scope, id);
    this.portalConstruct = new PortalConstruct(this, "PortalConstruct");
  }
}

export interface PortalConstructOutputs {
  portalBucket: s3.Bucket;
  portalUrl: string;
}
/**
 * Construct to provision Portal assets and CloudFront Distribution
 */
export class PortalConstruct
  extends Construct
  implements PortalConstructOutputs
{
  public portalBucket: s3.Bucket;
  public portalUrl: string;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    // Create S3 bucket
    this.portalBucket = new s3.Bucket(this, "PortalBucket", {
      versioned: false,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.RETAIN,
      autoDeleteObjects: false,
      objectOwnership: s3.ObjectOwnership.OBJECT_WRITER,
    });

    // Create Origin Access Identity
    // const oai = new cloudfront.OriginAccessIdentity(this, 'MyOriginAccessIdentity');
    // this.portalBucket.grantRead(oai);

    // Create CloudFront distribution
    // const distribution = new cloudfront.CfnDistribution(this, 'MyCloudfrontDistribution', {
    //   distributionConfig: {
    //     defaultCacheBehavior: {
    //       targetOriginId: this.portalBucket.bucketName,
    //       viewerProtocolPolicy: 'redirect-to-https',
    //       allowedMethods: ['GET', 'HEAD', 'OPTIONS'],
    //       cachedMethods: ['GET', 'HEAD', 'OPTIONS'],
    //       forwardedValues: {
    //         queryString: false,
    //         cookies: { forward: 'none' },
    //       },
    //       minTtl: 0,
    //       defaultTtl: 3600,
    //       maxTtl: 86400,
    //     },
    //     enabled: true,
    //     httpVersion: 'http2',
    //     defaultRootObject: 'index.html',
    //     ipv6Enabled: false,
    //     priceClass: 'PriceClass_All',
    //     origins: [
    //       {
    //         id: this.portalBucket.bucketName,
    //         domainName: this.portalBucket.bucketRegionalDomainName,
    //         s3OriginConfig: {
    //           originAccessIdentity: `origin-access-identity/cloudfront/${oai.originAccessIdentityId}`,
    //         },
    //       },
    //     ],
    //     viewerCertificate: {
    //       cloudFrontDefaultCertificate: true,
    //       minimumProtocolVersion: 'TLSv1.2_2021',
    //     },
    //   },
    // });

    const region = Stack.of(this).region;
    const lwaFn = new lambda.Function(this, "LwaFn", {
      runtime: lambda.Runtime.PROVIDED_AL2,
      handler: "bootstrap",
      memorySize: 2048,
      layers: [
        lambda.LayerVersion.fromLayerVersionArn(
          this,
          "LWALayer",
          `arn:${region.startsWith("cn") ? "aws-cn" : "aws"}:lambda:${region}:${
            region == "cn-north-1"
              ? "041581134020"
              : region == "cn-northwest-1"
              ? "069767869989"
              : "753240598075"
          }:layer:LambdaAdapterLayerX86:23`
        ),
        new lambda.LayerVersion(this, "NginxLayer", {
          code: lambda.Code.fromAsset("./lib/ui/Nginx123X86.zip"),
        }),
      ],
      code: lambda.Code.fromAsset("../lambda/portal"),
      environment: {
        PORT: "8080",
      },
    });
    const awsExportsFn = new lambda.Function(this, "awsExportsFn", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "app.handler",
      memorySize: 1024,
      environment: {
        BUCKET: this.portalBucket.bucketName,
      },
      code: lambda.Code.fromAsset("../lambda/aws_exports"),
    });
    this.portalBucket.grantRead(awsExportsFn);

    const apigw = new apigwv2.HttpApi(this, "PortalApi");
    apigw.addRoutes({
      path: "/aws-exports.json",
      methods: [apigwv2.HttpMethod.GET],
      integration: new HttpLambdaIntegration(
        "awsExportsIntegration",
        awsExportsFn
      ),
    });
    apigw.addRoutes({
      path: "/{proxy+}",
      methods: [apigwv2.HttpMethod.GET],
      integration: new HttpLambdaIntegration("lwaFnIntegration", lwaFn),
    });

    // this.portalUrl = distribution.attrDomainName;
    this.portalUrl = apigw.url!;

    // Upload static web assets
    new s3d.BucketDeployment(this, "DeployWebAssets", {
      sources: [s3d.Source.asset("../portal/dist")],
      destinationBucket: this.portalBucket,
      prune: false,
    });
  }
}
