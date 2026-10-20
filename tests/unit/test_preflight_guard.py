"""The preflight must be physically unable to change anything in AWS."""

import pathlib

import boto3
import pytest

from drsystem.aws.preflight import READ_ONLY_OPERATIONS, BlockedOperation, guarded_client

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"


def test_mutating_calls_are_blocked_before_any_request():
    session = boto3.Session(aws_access_key_id="x", aws_secret_access_key="y", region_name="ap-south-1")
    s3 = guarded_client(session, "s3", "ap-south-1")
    with pytest.raises(BlockedOperation):
        s3.create_bucket(Bucket="should-never-exist")
    ft = guarded_client(session, "freetier", "us-east-1")
    with pytest.raises(BlockedOperation):
        ft.upgrade_account_plan(accountPlanType="PAID")


def test_allowlist_contains_only_read_operations():
    prefixes = ("Get", "List", "Describe")
    assert all(op.startswith(prefixes) for op in READ_ONLY_OPERATIONS)


def test_no_code_path_upgrades_the_account_plan():
    offenders = [
        p for p in SRC.rglob("*.py") if "upgrade_account_plan" in p.read_text(encoding="utf-8") or "UpgradeAccountPlan(" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
