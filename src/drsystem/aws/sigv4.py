"""SigV4 signing for Lambda function URLs that use AuthType AWS_IAM.

The function URLs are not public: every request must be signed with credentials of this account
(the Lambda execution role in AWS, or your `aws login` session on the laptop). Nobody on the
internet can call them, so they cannot be used to burn through the free credits.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

SIGNED_HEADERS = ("content-type", "idempotency-key")


def region_from_url(url: str) -> str:
    host = urlsplit(url).hostname or ""
    parts = host.split(".")
    if "lambda-url" not in parts:
        raise ValueError(f"not a Lambda function URL: {url}")
    return parts[parts.index("lambda-url") + 1]


class LambdaUrlSigV4(httpx.Auth):
    requires_request_body = True

    def __init__(self, session=None) -> None:
        import boto3

        self.session = session or boto3.Session()

    def auth_flow(self, request: httpx.Request):
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        creds = self.session.get_credentials().get_frozen_credentials()
        url = str(request.url)
        headers = {k: v for k, v in request.headers.items() if k.lower() in SIGNED_HEADERS}
        aws_req = AWSRequest(method=request.method, url=url, data=request.content or b"", headers=headers)
        SigV4Auth(creds, "lambda", region_from_url(url)).add_auth(aws_req)
        for k, v in aws_req.headers.items():
            request.headers[k] = v
        yield request
