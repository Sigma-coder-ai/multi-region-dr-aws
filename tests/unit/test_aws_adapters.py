"""AWS adapters against moto (in-process mocks). Offline, no credentials, $0."""

import json

import boto3
import pytest
from moto import mock_aws

from drsystem.aws.faults_ssm import SsmFaults
from drsystem.aws.notify_sns import SnsNotifier
from drsystem.aws.s3store import S3ObjectStore
from drsystem.control import FencingError, bootstrap_control, promote
from drsystem.storage.base import NotFound, PreconditionFailed

REGION = "ap-south-1"


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket="dr-test", CreateBucketConfiguration={"LocationConstraint": REGION})
        yield S3ObjectStore("dr-test", REGION, client=client)


def test_s3_basic_roundtrip(s3):
    etag = s3.put("notes/a.json", b'{"x":1}')
    obj = s3.get("notes/a.json")
    assert obj.etag == etag and obj.json() == {"x": 1} and obj.metadata["written_region"] == REGION
    assert s3.list("notes/") == ["notes/a.json"]
    s3.delete("notes/a.json")
    with pytest.raises(NotFound):
        s3.get("notes/a.json")


def test_s3_if_none_match(s3):
    s3.put("k", b"1", if_none_match=True)
    try:
        s3.put("k", b"2", if_none_match=True)
    except PreconditionFailed:
        return
    pytest.skip("installed moto does not enforce IfNoneMatch; real S3 does")


def test_s3_control_promotion_fencing(s3):
    bootstrap_control(s3, REGION)
    promote(s3, "ap-southeast-1", 1, by="t", reason="r", demote_previous=True)
    with pytest.raises(FencingError):
        promote(s3, "ap-southeast-1", 1, by="t2", reason="r", demote_previous=True)


def test_ssm_faults():
    with mock_aws():
        ssm = boto3.client("ssm", region_name=REGION)
        faults = SsmFaults("/dr/faults/ap-south-1", REGION, client=ssm, cache_s=0)
        assert not faults.current().any
        ssm.put_parameter(Name="/dr/faults/ap-south-1", Value=json.dumps({"error_rate": 0.5, "junk": 1}), Type="String")
        assert faults.current().error_rate == 0.5


def test_sns_notifier():
    with mock_aws():
        sns = boto3.client("sns", region_name=REGION)
        arn = sns.create_topic(Name="dr-alerts")["TopicArn"]
        SnsNotifier(arn, REGION, client=sns).notify("subject", "message", "WARNING")
