import boto3
import os

bucket = os.environ["BUCKET"]

s3 = boto3.client("s3")
content = (
    s3.get_object(Bucket=bucket, Key="aws-exports.json")["Body"].read().decode("utf-8")
)


def handler(event, context):
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
        },
        "body": content,
    }
