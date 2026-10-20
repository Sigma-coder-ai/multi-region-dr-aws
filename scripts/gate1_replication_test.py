"""Gate 1 micro-test (REAL-AWS): prove two-way S3 Cross-Region Replication works on this account.

Creates (all tagged Project=dr-system, Gate=gate1):
  - one versioned, private S3 bucket in ap-south-1 and one in ap-southeast-1
  - one IAM role that S3 assumes to replicate between exactly those two buckets
  - two replication rules (Mumbai -> Singapore, Singapore -> Mumbai)
Then writes a handful of objects of a few bytes each, measures replication lag both ways, and
exercises the project's S3ObjectStore (conditional writes, epoch-fenced promotion) against real S3.

Everything is deleted in a ``finally`` block, even if a step fails; the script then verifies that
no bucket or role remains. A read-only preflight must pass first (Free plan, credits, regions).

Expected cost: a few dozen S3 requests and a few KB of storage/transfer. About $0.00, covered
by Free plan credits.

Usage:  .venv/Scripts/python scripts/gate1_replication_test.py --profile mahesh-project
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drsystem.aws.preflight import run_preflight  # noqa: E402
from drsystem.aws.s3store import S3ObjectStore  # noqa: E402
from drsystem.control import FencingError, bootstrap_control, promote, read_control  # noqa: E402
from drsystem.storage.base import PreconditionFailed  # noqa: E402

HOME, DR = "ap-south-1", "ap-southeast-1"
TAGS = [{"Key": "Project", "Value": "dr-system"}, {"Key": "Gate", "Value": "gate1"}]
OBJECTS_PER_DIRECTION = 5
REPLICATION_TIMEOUT_S = 900


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Gate1:
    def __init__(self, session: boto3.Session) -> None:
        self.session = session
        suffix = uuid.uuid4().hex[:8]
        self.buckets = {HOME: f"dr-gate1-{suffix}-aps1", DR: f"dr-gate1-{suffix}-apse1"}
        self.role_name = f"dr-gate1-replication-{suffix}"
        self.s3 = {r: session.client("s3", region_name=r) for r in (HOME, DR)}
        self.iam = session.client("iam")
        self.account = session.client("sts").get_caller_identity()["Account"]
        self.created: dict[str, list[str]] = {"buckets": [], "roles": []}
        self.results: dict = {"label": "REAL-AWS", "test": "gate1-two-way-s3-crr", "regions": [HOME, DR]}

    # -- setup ---------------------------------------------------------------------------------
    def create_buckets(self) -> None:
        for region, name in self.buckets.items():
            self.s3[region].create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": region})
            self.created["buckets"].append(name)
            self.s3[region].put_public_access_block(
                Bucket=name,
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
                },
            )
            self.s3[region].put_bucket_tagging(Bucket=name, Tagging={"TagSet": TAGS})
            self.s3[region].put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
            log(f"bucket ready: {name} ({region}), private, versioned")

    def create_role(self) -> str:
        trust = {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"Service": "s3.amazonaws.com"}, "Action": "sts:AssumeRole"}],
        }
        arns = [f"arn:aws:s3:::{b}" for b in self.buckets.values()]
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": ["s3:GetReplicationConfiguration", "s3:ListBucket"], "Resource": arns},
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObjectVersionForReplication", "s3:GetObjectVersionAcl", "s3:GetObjectVersionTagging"],
                    "Resource": [a + "/*" for a in arns],
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3:ReplicateObject", "s3:ReplicateDelete", "s3:ReplicateTags"],
                    "Resource": [a + "/*" for a in arns],
                },
            ],
        }
        role = self.iam.create_role(
            RoleName=self.role_name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Gate 1 micro-test: S3 two-way replication between two test buckets (auto-deleted)",
            Tags=TAGS,
        )
        self.created["roles"].append(self.role_name)
        self.iam.put_role_policy(RoleName=self.role_name, PolicyName="replicate-gate1-buckets", PolicyDocument=json.dumps(policy))
        log(f"replication role created: {self.role_name} (scoped to the two test buckets only)")
        return role["Role"]["Arn"]

    def configure_replication(self, role_arn: str) -> None:
        for src, dst in ((HOME, DR), (DR, HOME)):
            config = {
                "Role": role_arn,
                "Rules": [
                    {
                        "ID": f"to-{dst}",
                        "Priority": 1,
                        "Status": "Enabled",
                        "Filter": {},
                        "DeleteMarkerReplication": {"Status": "Disabled"},
                        "Destination": {"Bucket": f"arn:aws:s3:::{self.buckets[dst]}"},
                    }
                ],
            }
            for attempt in range(12):  # IAM role propagation can take a few seconds
                try:
                    self.s3[src].put_bucket_replication(Bucket=self.buckets[src], ReplicationConfiguration=config)
                    break
                except ClientError as exc:
                    if attempt == 11:
                        raise
                    log(f"waiting for IAM role propagation ({exc.response['Error']['Code']})")
                    time.sleep(5)
            log(f"replication rule enabled: {src} -> {dst}")

    # -- tests ---------------------------------------------------------------------------------
    def measure_replication(self, src: str, dst: str) -> list[dict]:
        out = []
        pending = {}
        for i in range(OBJECTS_PER_DIRECTION):
            key = f"gate1/{src}-to-{dst}/{i:03d}.json"
            body = json.dumps({"seq": i, "written_region": src, "written_at": time.time()}).encode()
            put = self.s3[src].put_object(Bucket=self.buckets[src], Key=key, Body=body)
            pending[key] = {"key": key, "put_at": time.time(), "etag": put["ETag"].strip('"')}
        deadline = time.time() + REPLICATION_TIMEOUT_S
        while pending and time.time() < deadline:
            for key in list(pending):
                try:
                    head = self.s3[dst].head_object(Bucket=self.buckets[dst], Key=key)
                except ClientError:
                    continue
                rec = pending.pop(key)
                rec.update(
                    seen_at=time.time(),
                    lag_s=round(time.time() - rec["put_at"], 1),
                    replication_status=head.get("ReplicationStatus"),
                    etag_match=head["ETag"].strip('"') == rec["etag"],
                )
                out.append(rec)
            time.sleep(1)
        for rec in pending.values():
            out.append({**rec, "lag_s": None, "replicated": False})
        lags = [r["lag_s"] for r in out if r.get("lag_s") is not None]
        log(f"{src} -> {dst}: {len(lags)}/{len(out)} replicated, lag min {min(lags, default=None)}s max {max(lags, default=None)}s "
            "(observed by 1 s polling; includes polling granularity)")
        return out

    def test_adapter(self) -> dict:
        """Run the project's S3ObjectStore and control-state logic against real S3."""
        store = S3ObjectStore(self.buckets[HOME], HOME, client=self.s3[HOME])
        res = {}
        etag = store.put("adapter/k.json", b'{"v":1}', if_none_match=True)
        try:
            store.put("adapter/k.json", b'{"v":2}', if_none_match=True)
            res["if_none_match_enforced"] = False
        except PreconditionFailed:
            res["if_none_match_enforced"] = True
        try:
            store.put("adapter/k.json", b'{"v":3}', if_match="0" * 32)
            res["if_match_enforced"] = False
        except PreconditionFailed:
            res["if_match_enforced"] = True
        store.put("adapter/k.json", b'{"v":4}', if_match=etag)
        res["if_match_with_correct_etag_ok"] = True
        res["metadata_roundtrip"] = store.get("adapter/k.json").metadata.get("written_region") == HOME
        bootstrap_control(store, HOME)
        promoted = promote(store, DR, 1, by="gate1", reason="adapter test", demote_previous=True)
        try:
            promote(store, DR, 1, by="gate1-stale", reason="stale", demote_previous=True)
            res["stale_epoch_fenced"] = False
        except FencingError:
            res["stale_epoch_fenced"] = True
        state, _ = read_control(store)
        res["control_after_promotion"] = {"active_region": state.active_region, "epoch": state.epoch, "demoted": state.demoted_region}
        res["promotion_epoch"] = promoted.epoch
        log(f"adapter on real S3: {res}")
        return res

    # -- cleanup -------------------------------------------------------------------------------
    def _empty_bucket(self, region: str, name: str) -> int:
        s3 = self.s3[region]
        deleted = 0
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=name):
            items = [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in page.get("Versions", []) + page.get("DeleteMarkers", [])]
            if items:
                s3.delete_objects(Bucket=name, Delete={"Objects": items, "Quiet": True})
                deleted += len(items)
        return deleted

    def cleanup(self) -> dict:
        report = {"buckets_deleted": [], "roles_deleted": [], "errors": []}
        for region, name in self.buckets.items():
            if name not in self.created["buckets"]:
                continue
            try:
                self.s3[region].delete_bucket_replication(Bucket=name)
            except ClientError:
                pass
        time.sleep(3)
        for _ in range(3):  # late replicas can land after the first sweep
            for region, name in self.buckets.items():
                if name in self.created["buckets"] and name not in report["buckets_deleted"]:
                    try:
                        self._empty_bucket(region, name)
                        self.s3[region].delete_bucket(Bucket=name)
                        report["buckets_deleted"].append(name)
                        log(f"deleted bucket {name}")
                    except ClientError as exc:
                        if exc.response["Error"]["Code"] == "NoSuchBucket":
                            report["buckets_deleted"].append(name)
                        else:
                            report["errors"].append(f"{name}: {exc.response['Error']['Code']}")
            if len(report["buckets_deleted"]) == len(self.created["buckets"]):
                break
            time.sleep(10)
        for role in self.created["roles"]:
            try:
                self.iam.delete_role_policy(RoleName=role, PolicyName="replicate-gate1-buckets")
            except ClientError:
                pass
            try:
                self.iam.delete_role(RoleName=role)
                report["roles_deleted"].append(role)
                log(f"deleted role {role}")
            except ClientError as exc:
                report["errors"].append(f"{role}: {exc.response['Error']['Code']}")
        return report

    def verify_clean(self) -> dict:
        names = {b["Name"] for b in self.s3[HOME].list_buckets().get("Buckets", [])}
        leftover_buckets = sorted(n for n in names if n.startswith("dr-gate1-"))
        leftover_roles = []
        for page in self.iam.get_paginator("list_roles").paginate():
            leftover_roles += [r["RoleName"] for r in page["Roles"] if r["RoleName"].startswith("dr-gate1-")]
        return {"leftover_buckets": leftover_buckets, "leftover_roles": leftover_roles, "clean": not leftover_buckets and not leftover_roles}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    args = ap.parse_args()

    pre = run_preflight(args.profile)
    print(pre.text())
    if not pre.ok:
        print("Preflight failed: nothing was created.")
        return 2

    session = boto3.Session(profile_name=args.profile)
    g = Gate1(session)
    t_start = time.time()
    try:
        g.create_buckets()
        role_arn = g.create_role()
        g.configure_replication(role_arn)
        g.results["home_to_dr"] = g.measure_replication(HOME, DR)
        g.results["dr_to_home"] = g.measure_replication(DR, HOME)
        g.results["adapter"] = g.test_adapter()
    except Exception as exc:  # noqa: BLE001 - always clean up, then report
        g.results["error"] = f"{type(exc).__name__}: {exc}"
        log(f"ERROR: {g.results['error']}")
    finally:
        g.results["cleanup"] = g.cleanup()
        g.results["verify_clean"] = g.verify_clean()
        g.results["duration_s"] = round(time.time() - t_start, 1)

    out_dir = ROOT / "evidence" / "real-aws" / time.strftime("gate1-%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(json.dumps(g.results, indent=2, default=str), encoding="utf-8")
    log(f"evidence: {out_dir / 'result.json'}")
    ok = (
        "error" not in g.results
        and all(r.get("lag_s") is not None for r in g.results.get("home_to_dr", []) + g.results.get("dr_to_home", []))
        and g.results["verify_clean"]["clean"]
    )
    print("GATE 1:", "PASS" if ok else "FAIL", "| cleanup clean:", g.results["verify_clean"]["clean"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
