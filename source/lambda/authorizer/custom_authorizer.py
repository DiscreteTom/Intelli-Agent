import json
import logging
import os
from urllib.request import urlopen

import jwt
import requests

# Replace with your Cognito User Pool info
USER_POOL_ID = os.environ["USER_POOL_ID"]
REGION = os.environ["REGION"]
APP_CLIENT_ID = os.environ["APP_CLIENT_ID"]

logger = logging.getLogger()
logger.setLevel(logging.INFO)

keys_url = "https://cognito-idp.{}.amazonaws.com/{}/.well-known/jwks.json".format(
    REGION, USER_POOL_ID
)

response = urlopen(keys_url)
keys = json.loads(response.read())["keys"]


def generatePolicy(principalId, effect, resource):
    authResponse = {}
    authResponse["principalId"] = principalId
    if effect and resource:
        policyDocument = {}
        policyDocument["Version"] = "2012-10-17"
        policyDocument["Statement"] = []
        statementOne = {}
        statementOne["Action"] = "execute-api:Invoke"
        statementOne["Effect"] = effect
        statementOne["Resource"] = resource
        policyDocument["Statement"] = [statementOne]
        authResponse["policyDocument"] = policyDocument

    authResponse["context"] = {
        "stringKey": "stringval",
        "numberKey": 123,
        "booleanKey": True,
    }

    authResponse_JSON = json.dumps(authResponse)

    return authResponse_JSON


def generateAllow(principalId, resource):
    return generatePolicy(principalId, "Allow", resource)


def generateDeny(principalId, resource):
    return generatePolicy(principalId, "Deny", resource)


def lambda_handler(event, context):
    logger.info(event)
    try:
        token = event["queryStringParameters"]["idToken"]
        headers = jwt.get_unverified_header(token)
        kid = headers["kid"]

        # Search for the kid in the downloaded public keys
        key_index = -1
        for i in range(len(keys)):
            if kid == keys[i]["kid"]:
                key_index = i
                break
        if key_index == -1:
            logger.error("Public key not found in jwks.json")
            raise Exception(
                "Custom Authorizer Error: Public key not found in jwks.json"
            )

        # Construct the public key
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(keys[key_index]))

        # Verify the signature of the JWT token
        claims = jwt.decode(
            token, public_key, algorithms=["RS256"], audience=APP_CLIENT_ID
        )

        # Verify the token issuer
        if claims["iss"] != "https://cognito-idp.{}.amazonaws.com/{}".format(
            REGION, USER_POOL_ID
        ):
            logger.error("Token was not issued by the correct issuer")
            raise Exception(
                "Custom Authorizer Error: Token was not issued by the correct issuer"
            )

        # Verify the token client
        if claims["aud"] != APP_CLIENT_ID:
            logger.error("Token was not issued for this audience")
            raise Exception(
                "Custom Authorizer Error: Token was not issued for this audience"
            )

        response = generateAllow("me", event["methodArn"])
        logger.info("Authorized")
        return json.loads(response)

    except Exception as e:
        logger.info("Not Authorized")
        logger.error(e)
        response = generateDeny("me", event["methodArn"])
        return json.loads(response)
