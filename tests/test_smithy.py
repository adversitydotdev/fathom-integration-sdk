"""Query sources over Smithy models (OGRL RFC 0007): classification, validation, and the wire.

The models below are minimal excerpts shaped like `aws/api-models-aws`; the fake AWS
answers with bodies shaped like the real services' responses.
"""

import hashlib
import json
import sys
from urllib.parse import parse_qsl, urlsplit

import pytest

from ogrl_integration import Integration, smithy
from ogrl_integration.http import Request, Response
from ogrl_integration.testing import Harness, HarnessError


def structure(members: dict, **traits) -> dict:
    return {"type": "structure", "members": members, **({"traits": traits} if traits else {})}


def m(target: str, **traits) -> dict:
    return {"target": target, **({"traits": traits} if traits else {})}


S = "smithy.api#String"
REQUIRED = {"smithy.api#required": {}}

IAM = {
    "smithy": "2.0",
    "shapes": {
        "com.amazonaws.iam#Iam": {
            "type": "service",
            "version": "2010-05-08",
            "operations": [
                {"target": "com.amazonaws.iam#ListRoles"},
                {"target": "com.amazonaws.iam#CreateRole"},
                {"target": "com.amazonaws.iam#GenerateCredentialReport"},
            ],
            "traits": {"aws.protocols#awsQuery": {}, "aws.auth#sigv4": {"name": "iam"}},
        },
        "com.amazonaws.iam#ListRoles": {
            "type": "operation",
            "input": {"target": "com.amazonaws.iam#ListRolesRequest"},
            "output": {"target": "com.amazonaws.iam#ListRolesResponse"},
            "traits": {
                "smithy.api#documentation": "<p>Lists the IAM roles. Paged.</p>",
                "smithy.api#paginated": {
                    "inputToken": "Marker",
                    "outputToken": "Marker",
                    "items": "Roles",
                    "pageSize": "MaxItems",
                },
            },
        },
        "com.amazonaws.iam#ListRolesRequest": structure(
            {
                "PathPrefix": m(S),
                "Marker": m(S),
                "MaxItems": m("smithy.api#Integer"),
            }
        ),
        "com.amazonaws.iam#ListRolesResponse": structure(
            {
                "Roles": m("com.amazonaws.iam#roleListType", **REQUIRED),
                "IsTruncated": m("smithy.api#Boolean"),
                "Marker": m(S),
            }
        ),
        "com.amazonaws.iam#roleListType": {"type": "list", "member": m("com.amazonaws.iam#Role")},
        "com.amazonaws.iam#Role": structure(
            {
                "RoleName": m(S, **REQUIRED),
                "RoleId": m(S, **REQUIRED),
                "CreateDate": m("smithy.api#Timestamp", **REQUIRED),
                "MaxSessionDuration": m("smithy.api#Integer"),
                "PermissionsBoundary": m("com.amazonaws.iam#Boundary"),
            }
        ),
        "com.amazonaws.iam#Boundary": structure({"PermissionsBoundaryArn": m(S)}),
        "com.amazonaws.iam#CreateRole": {
            "type": "operation",
            "input": {"target": "com.amazonaws.iam#ListRolesRequest"},
            "output": {"target": "com.amazonaws.iam#ListRolesResponse"},
        },
        "com.amazonaws.iam#GenerateCredentialReport": {"type": "operation"},
    },
}

EC2 = {
    "smithy": "2.0",
    "shapes": {
        "com.amazonaws.ec2#AmazonEC2": {
            "type": "service",
            "version": "2016-11-15",
            "operations": [
                {"target": "com.amazonaws.ec2#DescribeSecurityGroups"},
                {"target": "com.amazonaws.ec2#GetEbsEncryptionByDefault"},
                {"target": "com.amazonaws.ec2#GetPasswordData"},
            ],
            "traits": {"aws.protocols#ec2Query": {}, "aws.auth#sigv4": {"name": "ec2"}},
        },
        "com.amazonaws.ec2#DescribeSecurityGroups": {
            "type": "operation",
            "input": {"target": "com.amazonaws.ec2#DescribeSecurityGroupsRequest"},
            "output": {"target": "com.amazonaws.ec2#DescribeSecurityGroupsResult"},
            "traits": {
                "smithy.api#paginated": {
                    "inputToken": "NextToken",
                    "outputToken": "NextToken",
                    "items": "SecurityGroups",
                    "pageSize": "MaxResults",
                }
            },
        },
        "com.amazonaws.ec2#DescribeSecurityGroupsRequest": structure(
            {
                "GroupIds": m("com.amazonaws.ec2#GroupIdStringList", **{"smithy.api#xmlName": "GroupId"}),
                "NextToken": m(S),
                "MaxResults": m("smithy.api#Integer"),
                "DryRun": m(
                    "smithy.api#Boolean",
                    **{"aws.protocols#ec2QueryName": "DryRun", "smithy.api#xmlName": "dryRun"},
                ),
                "Filters": m("com.amazonaws.ec2#FilterList", **{"smithy.api#xmlName": "Filter"}),
            }
        ),
        "com.amazonaws.ec2#GroupIdStringList": {
            "type": "list",
            "member": m(S, **{"smithy.api#xmlName": "groupId"}),
        },
        "com.amazonaws.ec2#FilterList": {
            "type": "list",
            "member": m("com.amazonaws.ec2#Filter", **{"smithy.api#xmlName": "Filter"}),
        },
        "com.amazonaws.ec2#Filter": structure(
            {
                "Name": m(S),
                "Values": m("com.amazonaws.ec2#ValueStringList", **{"smithy.api#xmlName": "Value"}),
            }
        ),
        "com.amazonaws.ec2#ValueStringList": {
            "type": "list",
            "member": m(S, **{"smithy.api#xmlName": "item"}),
        },
        "com.amazonaws.ec2#DescribeSecurityGroupsResult": structure(
            {
                "NextToken": m(
                    S, **{"aws.protocols#ec2QueryName": "NextToken", "smithy.api#xmlName": "nextToken"}
                ),
                "SecurityGroups": m(
                    "com.amazonaws.ec2#SecurityGroupList",
                    **{
                        "aws.protocols#ec2QueryName": "SecurityGroupInfo",
                        "smithy.api#xmlName": "securityGroupInfo",
                    },
                ),
            }
        ),
        "com.amazonaws.ec2#SecurityGroupList": {
            "type": "list",
            "member": m("com.amazonaws.ec2#SecurityGroup", **{"smithy.api#xmlName": "item"}),
        },
        "com.amazonaws.ec2#SecurityGroup": structure(
            {
                "GroupId": m(S, **{"smithy.api#xmlName": "groupId"}),
                "GroupName": m(S, **{"smithy.api#xmlName": "groupName"}),
                "VpcId": m(S, **{"smithy.api#xmlName": "vpcId"}),
                "IpPermissions": m(
                    "com.amazonaws.ec2#IpPermissionList", **{"smithy.api#xmlName": "ipPermissions"}
                ),
            }
        ),
        "com.amazonaws.ec2#IpPermissionList": {
            "type": "list",
            "member": m("com.amazonaws.ec2#IpPermission", **{"smithy.api#xmlName": "item"}),
        },
        "com.amazonaws.ec2#IpPermission": structure(
            {
                "FromPort": m("smithy.api#Integer", **{"smithy.api#xmlName": "fromPort"}),
                "IpProtocol": m(S, **{"smithy.api#xmlName": "ipProtocol"}),
            }
        ),
        "com.amazonaws.ec2#GetEbsEncryptionByDefault": {
            "type": "operation",
            "input": {"target": "com.amazonaws.ec2#GetEbsRequest"},
            "output": {"target": "com.amazonaws.ec2#GetEbsResult"},
        },
        "com.amazonaws.ec2#GetEbsRequest": structure({"DryRun": m("smithy.api#Boolean")}),
        "com.amazonaws.ec2#GetEbsResult": structure(
            {
                "EbsEncryptionByDefault": m(
                    "smithy.api#Boolean", **{"smithy.api#xmlName": "ebsEncryptionByDefault"}
                )
            }
        ),
        "com.amazonaws.ec2#GetPasswordData": {
            "type": "operation",
            "input": {"target": "com.amazonaws.ec2#GetEbsRequest"},
            "output": {"target": "com.amazonaws.ec2#GetEbsResult"},
        },
    },
}

KMS = {
    "smithy": "2.0",
    "shapes": {
        "com.amazonaws.kms#TrentService": {
            "type": "service",
            "version": "2014-11-01",
            "operations": [{"target": "com.amazonaws.kms#ListKeys"}],
            "traits": {"aws.protocols#awsJson1_1": {}, "aws.auth#sigv4": {"name": "kms"}},
        },
        "com.amazonaws.kms#ListKeys": {
            "type": "operation",
            "input": {"target": "com.amazonaws.kms#ListKeysRequest"},
            "output": {"target": "com.amazonaws.kms#ListKeysResponse"},
            "traits": {
                "smithy.api#paginated": {
                    "inputToken": "Marker",
                    "outputToken": "NextMarker",
                    "items": "Keys",
                    "pageSize": "Limit",
                }
            },
        },
        "com.amazonaws.kms#ListKeysRequest": structure({"Limit": m("smithy.api#Integer"), "Marker": m(S)}),
        "com.amazonaws.kms#ListKeysResponse": structure(
            {
                "Keys": m("com.amazonaws.kms#KeyList"),
                "NextMarker": m(S),
                "Truncated": m("smithy.api#Boolean"),
            }
        ),
        "com.amazonaws.kms#KeyList": {"type": "list", "member": m("com.amazonaws.kms#KeyListEntry")},
        "com.amazonaws.kms#KeyListEntry": structure(
            {"KeyId": m(S), "KeyArn": m(S), "Created": m("smithy.api#Timestamp")}
        ),
    },
}

GUARDDUTY = {
    "smithy": "2.0",
    "shapes": {
        "com.amazonaws.guardduty#GuardDutyAPIService": {
            "type": "service",
            "version": "2017-11-28",
            "operations": [
                {"target": "com.amazonaws.guardduty#ListDetectors"},
                {"target": "com.amazonaws.guardduty#GetDetector"},
                {"target": "com.amazonaws.guardduty#GetFindings"},
            ],
            "traits": {"aws.protocols#restJson1": {}, "aws.auth#sigv4": {"name": "guardduty"}},
        },
        "com.amazonaws.guardduty#ListDetectors": {
            "type": "operation",
            "input": {"target": "com.amazonaws.guardduty#ListDetectorsRequest"},
            "output": {"target": "com.amazonaws.guardduty#ListDetectorsResponse"},
            "traits": {
                "smithy.api#http": {"method": "GET", "uri": "/detector", "code": 200},
                "smithy.api#paginated": {
                    "inputToken": "NextToken",
                    "outputToken": "NextToken",
                    "items": "DetectorIds",
                    "pageSize": "MaxResults",
                },
            },
        },
        "com.amazonaws.guardduty#ListDetectorsRequest": structure(
            {
                "MaxResults": m("smithy.api#Integer", **{"smithy.api#httpQuery": "maxResults"}),
                "NextToken": m(S, **{"smithy.api#httpQuery": "nextToken"}),
            }
        ),
        "com.amazonaws.guardduty#ListDetectorsResponse": structure(
            {
                "DetectorIds": m(
                    "com.amazonaws.guardduty#DetectorIds",
                    **{"smithy.api#jsonName": "detectorIds", "smithy.api#required": {}},
                ),
                "NextToken": m(S, **{"smithy.api#jsonName": "nextToken"}),
            }
        ),
        "com.amazonaws.guardduty#DetectorIds": {"type": "list", "member": m(S)},
        "com.amazonaws.guardduty#GetDetector": {
            "type": "operation",
            "input": {"target": "com.amazonaws.guardduty#GetDetectorRequest"},
            "output": {"target": "com.amazonaws.guardduty#GetDetectorResponse"},
            "traits": {"smithy.api#http": {"method": "GET", "uri": "/detector/{DetectorId}", "code": 200}},
        },
        "com.amazonaws.guardduty#GetDetectorRequest": structure(
            {"DetectorId": m(S, **{"smithy.api#httpLabel": {}, "smithy.api#required": {}})}
        ),
        "com.amazonaws.guardduty#GetDetectorResponse": structure(
            {
                "Status": m("com.amazonaws.guardduty#DetectorStatus", **{"smithy.api#jsonName": "status"}),
                "UpdatedAt": m(S, **{"smithy.api#jsonName": "updatedAt"}),
            }
        ),
        "com.amazonaws.guardduty#DetectorStatus": {
            "type": "enum",
            "members": {
                "ENABLED": m("smithy.api#Unit", **{"smithy.api#enumValue": "ENABLED"}),
                "DISABLED": m("smithy.api#Unit", **{"smithy.api#enumValue": "DISABLED"}),
            },
        },
        "com.amazonaws.guardduty#GetFindings": {
            "type": "operation",
            "input": {"target": "com.amazonaws.guardduty#GetDetectorRequest"},
            "output": {"target": "com.amazonaws.guardduty#GetDetectorResponse"},
            "traits": {"smithy.api#http": {"method": "POST", "uri": "/detector/{DetectorId}/findings/get"}},
        },
    },
}


S3 = {
    "smithy": "2.0",
    "shapes": {
        "com.amazonaws.s3#AmazonS3": {
            "type": "service",
            "version": "2006-03-01",
            "operations": [
                {"target": f"com.amazonaws.s3#{name}"}
                for name in (
                    "ListBuckets",
                    "GetBucketEncryption",
                    "GetBucketLocation",
                    "GetBucketPolicy",
                    "PutBucketPolicy",
                )
            ],
            "traits": {"aws.protocols#restXml": {}, "aws.auth#sigv4": {"name": "s3"}},
        },
        "com.amazonaws.s3#ListBuckets": {
            "type": "operation",
            "input": {"target": "com.amazonaws.s3#ListBucketsRequest"},
            "output": {"target": "com.amazonaws.s3#ListBucketsOutput"},
            "traits": {
                "smithy.api#http": {"method": "GET", "uri": "/?x-id=ListBuckets", "code": 200},
                "smithy.api#paginated": {
                    "inputToken": "ContinuationToken",
                    "outputToken": "ContinuationToken",
                    "items": "Buckets",
                    "pageSize": "MaxBuckets",
                },
            },
        },
        "com.amazonaws.s3#ListBucketsRequest": structure(
            {
                "MaxBuckets": m("smithy.api#Integer", **{"smithy.api#httpQuery": "max-buckets"}),
                "ContinuationToken": m(S, **{"smithy.api#httpQuery": "continuation-token"}),
                "Prefix": m(S, **{"smithy.api#httpQuery": "prefix"}),
                "BucketRegion": m(S, **{"smithy.api#httpQuery": "bucket-region"}),
            }
        ),
        "com.amazonaws.s3#ListBucketsOutput": structure(
            {"Buckets": m("com.amazonaws.s3#Buckets"), "ContinuationToken": m(S)},
            **{"smithy.api#xmlName": "ListAllMyBucketsResult"},
        ),
        "com.amazonaws.s3#Buckets": {
            "type": "list",
            "member": m("com.amazonaws.s3#Bucket", **{"smithy.api#xmlName": "Bucket"}),
        },
        "com.amazonaws.s3#Bucket": structure(
            {"Name": m(S), "CreationDate": m("smithy.api#Timestamp"), "BucketRegion": m(S)}
        ),
        "com.amazonaws.s3#BucketRequest": structure(
            {
                "Bucket": m(S, **{"smithy.api#httpLabel": {}, "smithy.api#required": {}}),
                "ExpectedBucketOwner": m(S, **{"smithy.api#httpHeader": "x-amz-expected-bucket-owner"}),
            }
        ),
        "com.amazonaws.s3#GetBucketEncryption": {
            "type": "operation",
            "input": {"target": "com.amazonaws.s3#BucketRequest"},
            "output": {"target": "com.amazonaws.s3#GetBucketEncryptionOutput"},
            "traits": {"smithy.api#http": {"method": "GET", "uri": "/{Bucket}?encryption", "code": 200}},
        },
        "com.amazonaws.s3#GetBucketEncryptionOutput": structure(
            {
                "ServerSideEncryptionConfiguration": m(
                    "com.amazonaws.s3#ServerSideEncryptionConfiguration", **{"smithy.api#httpPayload": {}}
                )
            }
        ),
        "com.amazonaws.s3#ServerSideEncryptionConfiguration": structure(
            {
                "Rules": m(
                    "com.amazonaws.s3#Rules",
                    **{
                        "smithy.api#xmlFlattened": {},
                        "smithy.api#xmlName": "Rule",
                        "smithy.api#required": {},
                    },
                )
            }
        ),
        "com.amazonaws.s3#Rules": {"type": "list", "member": m("com.amazonaws.s3#Rule")},
        "com.amazonaws.s3#Rule": structure(
            {
                "ApplyServerSideEncryptionByDefault": m("com.amazonaws.s3#Default"),
                "BucketKeyEnabled": m("smithy.api#Boolean"),
            }
        ),
        "com.amazonaws.s3#Default": structure({"SSEAlgorithm": m(S), "KMSMasterKeyID": m(S)}),
        "com.amazonaws.s3#GetBucketLocation": {
            "type": "operation",
            "input": {"target": "com.amazonaws.s3#BucketRequest"},
            "output": {"target": "com.amazonaws.s3#GetBucketLocationOutput"},
            "traits": {
                "aws.customizations#s3UnwrappedXmlOutput": {},
                "smithy.api#http": {"method": "GET", "uri": "/{Bucket}?location", "code": 200},
            },
        },
        "com.amazonaws.s3#GetBucketLocationOutput": structure(
            {"LocationConstraint": m(S)}, **{"smithy.api#xmlName": "LocationConstraint"}
        ),
        "com.amazonaws.s3#GetBucketPolicy": {
            "type": "operation",
            "input": {"target": "com.amazonaws.s3#BucketRequest"},
            "output": {"target": "com.amazonaws.s3#GetBucketPolicyOutput"},
            "traits": {"smithy.api#http": {"method": "GET", "uri": "/{Bucket}?policy", "code": 200}},
        },
        "com.amazonaws.s3#GetBucketPolicyOutput": structure(
            {"Policy": m(S, **{"smithy.api#httpPayload": {}})}
        ),
        "com.amazonaws.s3#PutBucketPolicy": {
            "type": "operation",
            "input": {"target": "com.amazonaws.s3#BucketRequest"},
            "traits": {"smithy.api#http": {"method": "PUT", "uri": "/{Bucket}?policy", "code": 200}},
        },
    },
}

SETTINGS = smithy.Settings(
    services={
        "iam": smithy.Service("models/iam.json", "https://iam.amazonaws.com", signing_region="us-east-1"),
        "ec2": smithy.Service("models/ec2.json", "https://ec2.{region}.amazonaws.com"),
        "kms": smithy.Service("models/kms.json", "https://kms.{region}.amazonaws.com"),
        "guardduty": smithy.Service("models/guardduty.json", "https://guardduty.{region}.amazonaws.com"),
        "s3": smithy.Service("models/s3.json", "https://s3.{region}.amazonaws.com"),
    },
    read_operations=("iam.GenerateCredentialReport",),
    deny_operations=("ec2.GetPasswordData",),
)
MODELS = {"iam": IAM, "ec2": EC2, "kms": KMS, "guardduty": GUARDDUTY, "s3": S3}
OPEN_GROUPS = smithy.Query(
    operation="ec2.DescribeSecurityGroups",
    params={"Filters": '[{"Name": "ip-permission.cidr", "Values": ["0.0.0.0/0"]}]'},
    fields={"id": "GroupId", "name": "GroupName", "vpc": "VpcId", "rules": "IpPermissions"},
    regions="*",
)


@pytest.fixture
def api():
    return smithy.Api(SETTINGS, MODELS)


def check(api, query):
    problems, schema = smithy.validate(api, query)
    return {p.pointer: p.detail for p in problems}, schema


# --- classification and validation ---------------------------------------------------------


def test_reads_are_classified(api):
    ops = api.operations
    assert ops["iam.ListRoles"].readable
    assert "not a read" in ops["iam.CreateRole"].reason
    assert ops["iam.GenerateCredentialReport"].readable  # listed in read_operations
    assert "denies it" in ops["ec2.GetPasswordData"].reason
    assert "only GET" in ops["guardduty.GetFindings"].reason  # a POST under restJson1
    assert ops["guardduty.GetDetector"].readable
    assert [op.name for op in api.search("roles")] == ["iam.ListRoles"]
    assert ops["iam.ListRoles"].summary == "Lists the IAM roles."


def test_a_valid_query_derives_its_item_schema(api):
    problems, schema = check(api, OPEN_GROUPS)
    assert problems == {}
    assert schema["required"] == ["id", "name", "region", "rules", "vpc"]
    assert schema["properties"]["rules"] == {"type": ["array", "null"]}
    assert schema["properties"]["region"] == {"type": ["string"]}

    roles = smithy.Query(
        "iam.ListRoles",
        fields={
            "id": "RoleId",
            "name": "RoleName",
            "created": "CreateDate",
            "boundary": "PermissionsBoundary.PermissionsBoundaryArn",
        },
    )
    problems, schema = check(api, roles)
    assert problems == {}
    assert schema["properties"]["created"] == {"type": ["string"]}  # required at every level
    assert schema["properties"]["boundary"] == {"type": ["string", "null"]}
    assert "region" not in schema["properties"]  # IAM is global


def test_feedback_points_at_the_problem(api):
    bad = smithy.Query(
        "ec2.DescribeSecurityGroups",
        params={"Filters": '[{"Nam": "x"}]', "NextToken": "abc", "DryRun": "yes"},
        fields={"id": "GroupID", "name": "GroupName", "region": "VpcId"},
        regions=["us-east-1", "mars"],
    )
    problems, _ = check(api, bad)
    assert problems == {
        "/regions/1": "'mars' is not a region code such as us-east-1",
        "/params/Filters": "Filters[0] has no member 'Nam'; did you mean 'Name'?",
        "/params/NextToken": "NextToken is the paginator's; leave it out",
        "/params/DryRun": "DryRun is true or false",
        "/fields/id": "items have no 'GroupID'; did you mean 'GroupId' or 'GroupName'?",
        "/fields/region": "region is added to every item of this query",
    }
    assert check(api, smithy.Query("ec2.DescribeSecurityGroups", params={"Filters": "[{"}))[0][
        "/params/Filters"
    ].startswith("Filters is JSON text")
    assert (
        "did you mean 'ec2.DescribeSecurityGroups'"
        in check(api, smithy.Query("ec2.DescribeSecurityGroup"))[0]["/operation"]
    )
    assert "<service>.<Operation>" in check(api, smithy.Query("DescribeSecurityGroups"))[0]["/operation"]


def test_regions_match_the_endpoint(api):
    regional = smithy.Query("ec2.DescribeSecurityGroups", fields={"id": "GroupId", "name": "GroupName"})
    assert check(api, regional)[0] == {"/regions": 'ec2 is regional: list regions, or "*"'}
    roles = smithy.Query("iam.ListRoles", fields={"id": "RoleId", "name": "RoleName"}, regions="*")
    assert check(api, roles)[0] == {"/regions": "iam is global: leave regions out"}


def test_single_outputs_scalar_items_and_labels(api):
    ebs = smithy.Query(
        "ec2.GetEbsEncryptionByDefault",
        fields={"id": "$region", "name": "$region", "enabled": "EbsEncryptionByDefault"},
        regions="*",
    )
    assert check(api, ebs)[0] == {}  # not paginated, no items: the output is the one item

    detectors = smithy.Query("guardduty.ListDetectors", fields={"id": ".", "name": "."}, regions="*")
    assert check(api, detectors)[0] == {}
    wrong = smithy.Query("guardduty.ListDetectors", fields={"id": "DetectorId", "name": "."}, regions="*")
    assert "project them with '.'" in check(api, wrong)[0]["/fields/id"]

    detector = smithy.Query(
        "guardduty.GetDetector", fields={"id": "$region", "name": "$region", "status": "Status"}, regions="*"
    )
    assert check(api, detector)[0] == {"/params/DetectorId": "guardduty.GetDetector needs DetectorId"}
    escaped = smithy.Query(**{**detector.__dict__, "params": {"DetectorId": "../x"}})
    assert "cannot contain /" in check(api, escaped)[0]["/params/DetectorId"]


def test_describe(api):
    described = api.describe("ec2.DescribeSecurityGroups")
    assert described["regional"] and described["paginated"] and described["items"] == "SecurityGroups"
    assert [p["name"] for p in described["params"]] == ["DryRun", "Filters", "GroupIds"]
    assert {"name": "Filters", "type": "array of object", "required": False, "json": True} in described[
        "params"
    ]
    assert {"path": "IpPermissions", "type": "array of object"} in described["fields"]
    assert api.describe("guardduty.ListDetectors")["item"] == "string"


def test_trim_keeps_reads_and_the_shapes_they_reach(api):
    trimmed = smithy.trim(api, "ec2")
    assert "com.amazonaws.ec2#GetPasswordData" not in trimmed["shapes"]
    assert "com.amazonaws.ec2#IpPermission" in trimmed["shapes"]
    again = smithy.Api(SETTINGS, {**MODELS, "ec2": trimmed})
    assert check(again, OPEN_GROUPS)[0] == {}


# --- the wire --------------------------------------------------------------------------------

CREDENTIALS = {
    "AWS_ACCESS_KEY_ID": "AKIDEXAMPLE",
    "AWS_SECRET_ACCESS_KEY": "secret",
    "AWS_SESSION_TOKEN": "token",
}


class FakeAws:
    """Answers like AWS, per protocol, and records what it was sent."""

    def __init__(self, fail_region: str | None = None) -> None:
        self.requests: list[Request] = []
        self.fail_region = fail_region

    def send(self, request: Request, timeout: float) -> Response:
        self.requests.append(request)
        assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
        host = urlsplit(request.url).hostname or ""
        if self.fail_region and self.fail_region in host:
            body = (
                b"<Response><Errors><Error><Code>AuthFailure</Code>"
                b"<Message>not enabled</Message></Error></Errors></Response>"
            )
            return Response(401, {}, body)
        form = dict(parse_qsl((request.body or b"").decode()))
        if host.startswith("ec2."):
            return self.ec2(form)
        if host.startswith("iam."):
            return self.iam(form)
        if host.startswith("kms."):
            return self.kms(json.loads(request.body or b"{}"))
        if host.startswith("s3."):
            return self.s3(request)
        return self.guardduty(request)

    def ec2(self, form):
        if form["Action"] == "GetEbsEncryptionByDefault":
            body = '<GetEbsEncryptionByDefaultResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"><ebsEncryptionByDefault>true</ebsEncryptionByDefault></GetEbsEncryptionByDefaultResponse>'
            return Response(200, {}, body.encode())
        page2 = form.get("NextToken") == "t2"
        groups = (
            "<item><groupId>sg-2</groupId><groupName>web</groupName></item>"
            if page2
            else "<item><groupId>sg-1</groupId><groupName>default</groupName><vpcId>vpc-1</vpcId>"
            "<ipPermissions><item><fromPort>22</fromPort><ipProtocol>tcp</ipProtocol></item></ipPermissions></item>"
        )
        token = "" if page2 else "<nextToken>t2</nextToken>"
        body = (
            '<DescribeSecurityGroupsResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">'
            f"<requestId>r</requestId><securityGroupInfo>{groups}</securityGroupInfo>{token}"
            "</DescribeSecurityGroupsResponse>"
        )
        return Response(200, {}, body.encode())

    def iam(self, form):
        page2 = form.get("Marker") == "m2"
        role = (
            "<member><RoleName>audit</RoleName><RoleId>AROA2</RoleId><CreateDate>2024-02-01T00:00:00Z</CreateDate></member>"
            if page2
            else "<member><RoleName>admin</RoleName><RoleId>AROA1</RoleId>"
            "<CreateDate>2024-01-01T00:00:00Z</CreateDate>"
            "<MaxSessionDuration>3600</MaxSessionDuration><PermissionsBoundary><PermissionsBoundaryArn>arn:b</PermissionsBoundaryArn></PermissionsBoundary></member>"
        )
        more = (
            "<IsTruncated>false</IsTruncated>"
            if page2
            else "<IsTruncated>true</IsTruncated><Marker>m2</Marker>"
        )
        body = (
            '<ListRolesResponse xmlns="https://iam.amazonaws.com/doc/2010-05-08/"><ListRolesResult>'
            f"<Roles>{role}</Roles>{more}</ListRolesResult><ResponseMetadata><RequestId>r</RequestId></ResponseMetadata></ListRolesResponse>"
        )
        return Response(200, {}, body.encode())

    def kms(self, payload):
        if payload.get("Marker") == "k2":
            return Response(200, {}, json.dumps({"Keys": [{"KeyId": "k-2", "KeyArn": "arn:k2"}]}).encode())
        keys = [{"KeyId": "k-1", "KeyArn": "arn:k1", "Created": 1704067200}]
        return Response(200, {}, json.dumps({"Keys": keys, "NextMarker": "k2", "Truncated": True}).encode())

    def s3(self, request):
        assert request.headers["X-Amz-Content-Sha256"] == hashlib.sha256(b"").hexdigest()
        url = urlsplit(request.url)
        if url.path == "/" and "continuation-token=c2" in url.query:
            body = (
                "<ListAllMyBucketsResult><Buckets><Bucket><Name>logs</Name></Bucket></Buckets>"
                "</ListAllMyBucketsResult>"
            )
        elif url.path == "/":
            body = (
                '<ListAllMyBucketsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Buckets>'
                "<Bucket><Name>data</Name><CreationDate>2024-01-01T00:00:00.000Z</CreationDate>"
                "<BucketRegion>us-east-1</BucketRegion></Bucket></Buckets>"
                "<ContinuationToken>c2</ContinuationToken></ListAllMyBucketsResult>"
            )
        elif url.path == "/logs" and url.query == "encryption=":
            body = (
                "<Error><Code>ServerSideEncryptionConfigurationNotFoundError</Code>"
                "<Message>The server side encryption configuration was not found</Message></Error>"
            )
            return Response(404, {}, body.encode())
        elif url.query == "encryption=":
            body = (
                '<ServerSideEncryptionConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                "<Rule><ApplyServerSideEncryptionByDefault><SSEAlgorithm>aws:kms</SSEAlgorithm>"
                "</ApplyServerSideEncryptionByDefault><BucketKeyEnabled>true</BucketKeyEnabled></Rule>"
                "</ServerSideEncryptionConfiguration>"
            )
        elif url.query == "location=":
            body = (
                '<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                "eu-west-1</LocationConstraint>"
            )
        else:
            body = '{"Version": "2012-10-17", "Statement": []}'
        return Response(200, {}, body.encode())

    def guardduty(self, request):
        if "/detector/" in request.url:
            return Response(200, {}, json.dumps({"status": "ENABLED", "updatedAt": "x"}).encode())
        return Response(200, {}, json.dumps({"detectorIds": ["d-1"]}).encode())


@pytest.fixture
def bundled(tmp_path, monkeypatch, api):
    trimmed = {name: smithy.trim(api, name) for name in MODELS}
    (tmp_path / "_ogrl_smithy.py").write_text(smithy.encode(SETTINGS, trimmed))
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("_ogrl_smithy", None)
    yield
    sys.modules.pop("_ogrl_smithy", None)


def collect(query: smithy.Query, config=None, aws=None, **extra):
    app = Integration("dev.example.aws", "1.0.0")
    app.transport = aws or FakeAws()
    spec = {
        "operation": query.operation,
        "params": query.params,
        "items": query.items,
        "fields": query.fields,
        "regions": query.regions,
        **({"for_each": query.for_each.__dict__} if query.for_each else {}),
        **extra,
    }
    return Harness(app).collect(
        "q", 1, config or {"regions": "us-east-1"}, CREDENTIALS, query=spec
    ), app.transport


def test_ec2_query_serializes_lists_and_reads_item_lists(bundled):
    out, aws = collect(OPEN_GROUPS, {"regions": "us-east-1,eu-west-1"})
    assert [(i["id"], i["region"]) for i in out.items] == [
        ("sg-1", "us-east-1"),
        ("sg-2", "us-east-1"),
        ("sg-1", "eu-west-1"),
        ("sg-2", "eu-west-1"),
    ]
    assert out.items[0]["rules"] == [{"FromPort": 22, "IpProtocol": "tcp"}]
    assert out.items[1]["vpc"] is None
    first = aws.requests[0]
    assert first.url == "https://ec2.us-east-1.amazonaws.com/"
    assert dict(parse_qsl(first.body.decode())) == {
        "Action": "DescribeSecurityGroups",
        "Version": "2016-11-15",
        "Filter.1.Name": "ip-permission.cidr",
        "Filter.1.Value.1": "0.0.0.0/0",
    }
    assert dict(parse_qsl(aws.requests[1].body.decode()))["NextToken"] == "t2"
    assert "/eu-west-1/ec2/aws4_request" in aws.requests[2].headers["Authorization"]
    assert first.headers["Accept"] == "text/xml"


def test_aws_query_unwraps_the_result_and_member_lists(bundled):
    roles = smithy.Query(
        "iam.ListRoles",
        params={"PathPrefix": "/"},
        fields={
            "id": "RoleId",
            "name": "RoleName",
            "created": "CreateDate",
            "max": "MaxSessionDuration",
            "boundary": "PermissionsBoundary.PermissionsBoundaryArn",
        },
    )
    out, aws = collect(roles)
    assert out.items == [
        {"id": "AROA1", "name": "admin", "created": "2024-01-01T00:00:00Z", "max": 3600, "boundary": "arn:b"},
        {"id": "AROA2", "name": "audit", "created": "2024-02-01T00:00:00Z", "max": None, "boundary": None},
    ]
    assert aws.requests[0].url == "https://iam.amazonaws.com/"
    assert "/us-east-1/iam/aws4_request" in aws.requests[0].headers["Authorization"]  # global: signing_region
    assert dict(parse_qsl(aws.requests[1].body.decode()))["Marker"] == "m2"


def test_aws_json_targets_the_operation_and_reads_epoch_times(bundled):
    keys = smithy.Query(
        "kms.ListKeys", fields={"id": "KeyId", "name": "KeyArn", "created": "Created"}, regions=["us-west-2"]
    )
    out, aws = collect(keys)
    assert out.items[0] == {
        "id": "k-1",
        "name": "arn:k1",
        "created": "2024-01-01T00:00:00Z",
        "region": "us-west-2",
    }
    assert [i["id"] for i in out.items] == ["k-1", "k-2"]
    first = aws.requests[0]
    assert first.headers["X-Amz-Target"] == "TrentService.ListKeys"
    assert first.headers["Content-Type"] == "application/x-amz-json-1.1"
    assert json.loads(aws.requests[1].body) == {"Marker": "k2"}


def test_rest_json_fills_labels_and_honours_json_names(bundled):
    detectors = smithy.Query("guardduty.ListDetectors", fields={"id": ".", "name": "."}, regions="*")
    out, aws = collect(detectors)
    assert out.items == [{"id": "d-1", "name": "d-1", "region": "us-east-1"}]
    assert (
        aws.requests[0].method == "GET"
        and aws.requests[0].url == "https://guardduty.us-east-1.amazonaws.com/detector"
    )

    detector = smithy.Query(
        "guardduty.GetDetector",
        params={"DetectorId": "d-1"},
        fields={"id": "$region", "name": "$region", "status": "Status"},
        regions="*",
    )
    out, aws = collect(detector)
    assert out.items == [{"id": "us-east-1", "name": "us-east-1", "status": "ENABLED", "region": "us-east-1"}]
    assert aws.requests[0].url.endswith("/detector/d-1")


def test_single_output_is_one_item_per_region(bundled):
    ebs = smithy.Query(
        "ec2.GetEbsEncryptionByDefault",
        fields={"id": "$region", "name": "$region", "enabled": "EbsEncryptionByDefault"},
        regions=["us-east-1", "us-east-2"],
    )
    out, _ = collect(ebs)
    assert out.items == [
        {"id": "us-east-1", "name": "us-east-1", "enabled": True, "region": "us-east-1"},
        {"id": "us-east-2", "name": "us-east-2", "enabled": True, "region": "us-east-2"},
    ]


def test_a_failed_region_makes_the_collection_incomplete(bundled):
    out, _ = collect(OPEN_GROUPS, {"regions": "us-east-1,ap-east-1"}, FakeAws(fail_region="ap-east-1"))
    assert len(out.items) == 2 and not out.complete
    assert out.reason.startswith(
        "ap-east-1: POST https://ec2.ap-east-1.amazonaws.com/ answered 401: AuthFailure: not enabled"
    )

    with pytest.raises(HarnessError) as caught:  # every region failed: the error itself
        collect(OPEN_GROUPS, {"regions": "ap-east-1"}, FakeAws(fail_region="ap-east-1"))
    assert caught.value.kind == "auth_failed"


def test_execute_rechecks_reads_and_regions(bundled):
    with pytest.raises(HarnessError) as caught:
        collect(smithy.Query("ec2.GetPasswordData", fields={"id": "$region", "name": "$region"}, regions="*"))
    # The bundle is trimmed to queryable operations, so a denied one is not even there.
    assert caught.value.kind == "invalid_config"
    assert "no queryable operation 'ec2.GetPasswordData'" in caught.value.error["message"]
    with pytest.raises(HarnessError) as caught:
        collect(OPEN_GROUPS, {"regions": "us-east-1.evil.test/"})
    assert caught.value.kind == "invalid_config"


def test_max_items_stops_before_the_next_page(bundled):
    out, aws = collect(OPEN_GROUPS, max_items=1)
    assert len(out.items) == 1 and len(aws.requests) == 1


def test_throttling_is_retried():
    from ogrl_integration.http import Http

    class Throttled:
        calls = 0

        def send(self, request, timeout):
            Throttled.calls += 1
            if Throttled.calls == 1:
                return Response(
                    400,
                    {},
                    b"<ErrorResponse><Error><Code>Throttling</Code><Message>slow</Message></Error></ErrorResponse>",
                )
            return Response(200, {}, b"<ok/>")

    http = Http(Throttled(), sleep=lambda s: None)
    http.request("POST", "https://iam.amazonaws.com/", throttled=smithy._throttled)
    assert Throttled.calls == 2


def test_json_access_denied_is_a_permission_error(bundled):
    class Denied(FakeAws):
        def kms(self, payload):
            body = {"__type": "AccessDeniedException", "message": "not authorized to perform: kms:ListKeys"}
            return Response(400, {}, json.dumps(body).encode())

    keys = smithy.Query("kms.ListKeys", fields={"id": "KeyId", "name": "KeyArn"}, regions="*")
    with pytest.raises(HarnessError) as caught:
        collect(keys, aws=Denied())
    assert caught.value.kind == "permission_denied"
    assert "AccessDeniedException: not authorized" in caught.value.error["message"]


def test_rest_xml_reads_s3(bundled, api):
    assert not api.operations["s3.PutBucketPolicy"].readable
    buckets = smithy.Query(
        "s3.ListBuckets",
        fields={"id": "Name", "name": "Name", "created": "CreationDate", "home": "BucketRegion"},
        regions=["us-east-1"],
    )
    out, aws = collect(buckets)
    assert [(i["name"], i["created"]) for i in out.items] == [
        ("data", "2024-01-01T00:00:00Z"),
        ("logs", None),
    ]
    assert aws.requests[0].url == "https://s3.us-east-1.amazonaws.com/?x-id=ListBuckets"
    assert "/us-east-1/s3/aws4_request" in aws.requests[0].headers["Authorization"]

    encryption = smithy.Query(
        "s3.GetBucketEncryption",
        params={"Bucket": "data"},
        fields={
            "id": "$region",
            "name": "$region",
            "rules": "ServerSideEncryptionConfiguration.Rules",
        },
        regions=["us-east-1"],
    )
    out, aws = collect(encryption)
    assert out.items[0]["rules"] == [
        {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}, "BucketKeyEnabled": True}
    ]
    assert aws.requests[0].url == "https://s3.us-east-1.amazonaws.com/data?encryption="

    location = smithy.Query(
        "s3.GetBucketLocation",
        params={"Bucket": "data"},
        fields={"id": "$region", "name": "LocationConstraint"},
        regions=["us-east-1"],
    )
    assert collect(location)[0].items[0]["name"] == "eu-west-1"

    policy = smithy.Query(
        "s3.GetBucketPolicy",
        params={"Bucket": "data"},
        fields={"id": "$region", "name": "$region", "policy": "Policy"},
        regions=["us-east-1"],
    )
    assert collect(policy)[0].items[0]["policy"].startswith('{"Version"')


ENCRYPTION = smithy.Query(
    "s3.GetBucketEncryption",
    fields={
        "id": "$parent.Name",
        "name": "$parent.Name",
        "created": "$parent.CreationDate",
        "rules": "ServerSideEncryptionConfiguration.Rules",
    },
    regions=["us-east-1"],
    for_each=smithy.ForEach("s3.ListBuckets", bind={"Bucket": "Name"}, params={"BucketRegion": "$region"}),
)


def test_for_each_validates_both_calls(api):
    problems, schema = check(api, ENCRYPTION)
    assert problems == {}
    assert schema["required"] == ["created", "error", "id", "name", "region", "rules"]
    assert schema["properties"]["error"] == {"type": ["string", "null"]}
    assert schema["properties"]["rules"] == {"type": ["array", "null"]}
    assert schema["properties"]["created"] == {"type": ["string", "null"]}

    def problems_with(**changes):
        fe = {**ENCRYPTION.for_each.__dict__, **changes.pop("for_each", {})}
        query = smithy.Query(**{**ENCRYPTION.__dict__, **changes, "for_each": smithy.ForEach(**fe)})
        return check(api, query)[0]

    assert problems_with(for_each={"bind": {"Bucket": "Nme"}}) == {
        "/for_each/bind/Bucket": "items have no 'Nme'; did you mean 'Name'?"
    }
    assert problems_with(params={"Bucket": "x"}) == {
        "/params/Bucket": "for_each binds Bucket; leave it out of params"
    }
    assert problems_with(for_each={"operation": "s3.GetBucketLocation", "params": {"Bucket": "x"}}) == {
        "/for_each/items": "s3.GetBucketLocation returns one result; name its list"
    }
    assert (
        "list from s3"
        in problems_with(for_each={"operation": "kms.ListKeys", "params": {}})["/for_each/operation"]
    )
    assert problems_with(for_each={"limit": 0}) == {"/for_each/limit": "between 1 and 5000"}
    plain = smithy.Query(
        "s3.ListBuckets", fields={"id": "$parent.Name", "name": "Name"}, regions=["us-east-1"]
    )
    assert check(api, plain)[0] == {"/fields/id": "$parent reads the listed item of a for_each query"}
    global_region = smithy.Query(
        "iam.ListRoles", params={"PathPrefix": "$region"}, fields={"id": "RoleId", "name": "RoleName"}
    )
    assert check(api, global_region)[0] == {"/params/PathPrefix": "iam is global: there is no $region"}


def test_for_each_calls_once_per_listed_item(bundled):
    out, aws = collect(ENCRYPTION)
    assert out.complete
    assert out.items == [
        {
            "id": "data",
            "name": "data",
            "created": "2024-01-01T00:00:00Z",
            "rules": [
                {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}, "BucketKeyEnabled": True}
            ],
            "region": "us-east-1",
            "error": None,
        },
        {
            "id": "logs",
            "name": "logs",
            "created": None,
            "rules": None,
            "region": "us-east-1",
            "error": "ServerSideEncryptionConfigurationNotFoundError",
        },
    ]
    urls = [r.url.split("amazonaws.com", 1)[1] for r in aws.requests]
    assert urls == [
        "/?x-id=ListBuckets&bucket-region=us-east-1",
        "/data?encryption=",
        "/?x-id=ListBuckets&bucket-region=us-east-1&continuation-token=c2",
        "/logs?encryption=",
    ]


def test_for_each_limit_makes_the_collection_incomplete(bundled):
    limited = smithy.Query(
        **{**ENCRYPTION.__dict__, "for_each": smithy.ForEach(**{**ENCRYPTION.for_each.__dict__, "limit": 1})}
    )
    out, _ = collect(limited)
    assert [i["id"] for i in out.items] == ["data"]
    assert (out.complete, out.reason) == (False, "us-east-1: s3.ListBuckets listed more than 1 items")
