"""The signing example from AWS's documentation (ListUsers on IAM, 2015-08-30)."""

from datetime import UTC, datetime

from fathom_integration.sigv4 import sign


def test_aws_documented_example():
    headers = sign(
        "GET",
        "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
        {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        b"",
        access_key="AKIDEXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        session_token=None,
        region="us-east-1",
        service="iam",
        now=datetime(2015, 8, 30, 12, 36, tzinfo=UTC),
    )
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date, "
        "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
    )
