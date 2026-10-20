"""Deploy / inspect / tear down Option A on real AWS (zero out-of-pocket, Free plan only).

  python scripts/aws_deploy.py deploy   --profile mahesh-project --email you@example.com
  python scripts/aws_deploy.py status   --profile mahesh-project
  python scripts/aws_deploy.py teardown --profile mahesh-project

Safety
- The read-only preflight must pass first: plan FREE/ACTIVE, credits >= $50, >= 30 days left.
- Function URLs use AuthType AWS_IAM (signed requests only): nothing is publicly callable.
- Everything is tagged Project=dr-system; teardown deletes it all and verifies by tag search.
- No EC2, NAT, load balancer, domain, CloudFront, KMS keys or paid monitoring.

State (names, ARNs, URLs) is kept in .aws-deploy.json (git-ignored).
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drsystem.aws.preflight import run_preflight  # noqa: E402
from drsystem.aws.s3store import S3ObjectStore  # noqa: E402
from drsystem.control import CONTROL_KEY, bootstrap_control  # noqa: E402

HOME, DR = "ap-south-1", "ap-southeast-1"
REGIONS = (HOME, DR)
P = "drsys"
TAGS = {"Project": "dr-system", "Env": "lab"}
STATE = ROOT / ".aws-deploy.json"
BUILD = ROOT / "build"
LAMBDA_DEPS = ["fastapi>=0.115", "mangum>=0.19", "httpx>=0.27", "boto3>=1.40"]
KEEP_BOTOCORE_SERVICES = {"s3", "ssm", "sns", "sts"}
# More memory = more CPU on Lambda; 1 GB keeps cold imports fast and stays far inside 400k free GB-s/month.
APP_HANDLER, APP_MEMORY, APP_TIMEOUT = "drsystem.aws.app_entry.handler", 1024, 15
CTL_HANDLER, CTL_MEMORY = "drsystem.aws.controller_entry.handler", 256  # mostly waits on network


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def tag_list(extra: dict | None = None) -> list[dict]:
    return [{"Key": k, "Value": v} for k, v in {**TAGS, **(extra or {})}.items()]


def load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, indent=2))


def retry(fn, what: str, attempts: int = 15, delay: float = 4.0, codes=("InvalidParameterValueException",
          "AccessDenied", "InvalidRequest", "InvalidArgument", "ValidationException", "ResourceConflictException")):
    for i in range(attempts):
        try:
            return fn()
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code not in codes or i == attempts - 1:
                raise
            log(f"  waiting ({what}: {code})")
            time.sleep(delay)


# -- build ----------------------------------------------------------------------------------------
def build_zip() -> bytes:
    pkg = BUILD / "lambda"
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir(parents=True)
    log("building Lambda package (linux arm64, Python 3.12)")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--target", str(pkg), "--platform", "manylinux2014_aarch64",
         "--implementation", "cp", "--python-version", "3.12", "--only-binary=:all:", *LAMBDA_DEPS],
        check=True,
    )
    # Keep only the AWS service models the app uses (botocore ships ~400 of them).
    data = pkg / "botocore" / "data"
    for d in data.iterdir():
        if d.is_dir() and d.name not in KEEP_BOTOCORE_SERVICES:
            shutil.rmtree(d)
    shutil.copytree(ROOT / "src" / "drsystem", pkg / "drsystem", ignore=shutil.ignore_patterns("__pycache__"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in pkg.rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts:
                zf.write(f, f.relative_to(pkg).as_posix())
    data = buf.getvalue()
    log(f"package size {len(data) / 1e6:.1f} MB")
    if len(data) > 49_000_000:
        raise SystemExit("package too large for direct upload")
    return data


# -- IAM ------------------------------------------------------------------------------------------
def ensure_role(iam, name: str, service: str, policy: dict, desc: str) -> str:
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}]}
    try:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Description=desc, Tags=tag_list())["Role"]["Arn"]
        log(f"role created: {name}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    iam.put_role_policy(RoleName=name, PolicyName="inline", PolicyDocument=json.dumps(policy))
    return arn


def lambda_logs_statement(acct: str, fn: str) -> dict:
    return {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": [f"arn:aws:logs:*:{acct}:log-group:/aws/lambda/{fn}:*"]}


# -- deploy ---------------------------------------------------------------------------------------
def deploy(session: boto3.Session, email: str) -> None:
    st = load_state()
    acct = session.client("sts").get_caller_identity()["Account"]
    st.setdefault("suffix", uuid.uuid4().hex[:6])
    sfx = st["suffix"]
    buckets = {r: f"{P}-{sfx}-{r}" for r in REGIONS}
    st["buckets"] = buckets
    st["home"], st["dr"] = HOME, DR
    save_state(st)
    iam = session.client("iam")
    s3 = {r: session.client("s3", region_name=r) for r in REGIONS}
    from botocore.config import Config

    upload_cfg = Config(connect_timeout=30, read_timeout=300, retries={"max_attempts": 5, "mode": "standard"})
    lam = {r: session.client("lambda", region_name=r, config=upload_cfg) for r in REGIONS}
    logs = {r: session.client("logs", region_name=r) for r in REGIONS}
    ssm = {r: session.client("ssm", region_name=r) for r in REGIONS}
    bucket_arns = [f"arn:aws:s3:::{b}" for b in buckets.values()]
    fn_arns = [f"arn:aws:lambda:{r}:{acct}:function:{P}-app" for r in REGIONS]

    # IAM roles (least privilege; no deletes of buckets, no IAM rights)
    s3_rw = {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
             "Resource": bucket_arns + [a + "/*" for a in bucket_arns]}
    invoke_urls = {"Effect": "Allow", "Action": ["lambda:InvokeFunctionUrl", "lambda:InvokeFunction"], "Resource": fn_arns}
    app_role = ensure_role(iam, f"{P}-app-role", "lambda.amazonaws.com", {"Version": "2012-10-17", "Statement": [
        s3_rw, invoke_urls,
        {"Effect": "Allow", "Action": ["ssm:GetParameter"], "Resource": [f"arn:aws:ssm:*:{acct}:parameter/{P}/faults/*"]},
        lambda_logs_statement(acct, f"{P}-app")]}, "DR app Lambda")
    topic_arn = f"arn:aws:sns:{DR}:{acct}:{P}-alerts"
    ctl_role = ensure_role(iam, f"{P}-controller-role", "lambda.amazonaws.com", {"Version": "2012-10-17", "Statement": [
        s3_rw, invoke_urls,
        {"Effect": "Allow", "Action": ["sns:Publish"], "Resource": [topic_arn]},
        lambda_logs_statement(acct, f"{P}-controller")]}, "DR controller Lambda")
    repl_role = ensure_role(iam, f"{P}-replication-role", "s3.amazonaws.com", {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetReplicationConfiguration", "s3:ListBucket"], "Resource": bucket_arns},
        {"Effect": "Allow", "Action": ["s3:GetObjectVersionForReplication", "s3:GetObjectVersionAcl", "s3:GetObjectVersionTagging",
                                       "s3:ReplicateObject", "s3:ReplicateDelete", "s3:ReplicateTags"],
         "Resource": [a + "/*" for a in bucket_arns]}]}, "S3 two-way replication")
    ctl_fn_arn = f"arn:aws:lambda:{DR}:{acct}:function:{P}-controller"
    sched_role = ensure_role(iam, f"{P}-scheduler-role", "scheduler.amazonaws.com", {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["lambda:InvokeFunction"], "Resource": [ctl_fn_arn]}]}, "Scheduler invokes controller")

    # Buckets
    for r, b in buckets.items():
        try:
            s3[r].create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": r})
            log(f"bucket created: {b}")
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
                raise
        s3[r].put_public_access_block(Bucket=b, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        s3[r].put_bucket_tagging(Bucket=b, Tagging={"TagSet": tag_list()})
        s3[r].put_bucket_versioning(Bucket=b, VersioningConfiguration={"Status": "Enabled"})
        # Keep old versions for 1 day only: versioning is required by replication, not for history.
        s3[r].put_bucket_lifecycle_configuration(Bucket=b, LifecycleConfiguration={"Rules": [{
            "ID": "expire-noncurrent", "Status": "Enabled", "Filter": {},
            "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
            "Expiration": {"ExpiredObjectDeleteMarker": True},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}]})
    for src, dst in ((HOME, DR), (DR, HOME)):
        cfg = {"Role": repl_role, "Rules": [{"ID": f"to-{dst}", "Priority": 1, "Status": "Enabled", "Filter": {},
               "DeleteMarkerReplication": {"Status": "Disabled"}, "Destination": {"Bucket": f"arn:aws:s3:::{buckets[dst]}"}}]}
        retry(lambda: s3[src].put_bucket_replication(Bucket=buckets[src], ReplicationConfiguration=cfg), "replication role")
        log(f"replication enabled: {src} -> {dst}")

    # SSM fault parameters (standard tier, free)
    for r in REGIONS:
        name = f"/{P}/faults/{r}"
        ssm[r].put_parameter(Name=name, Value="{}", Type="String", Overwrite=True)
        try:
            ssm[r].add_tags_to_resource(ResourceType="Parameter", ResourceId=name, Tags=tag_list())
        except ClientError:
            pass

    # Log groups with short retention (created before the functions so retention applies)
    for r, fn in [(HOME, f"{P}-app"), (DR, f"{P}-app"), (DR, f"{P}-controller")]:
        lg = f"/aws/lambda/{fn}"
        try:
            logs[r].create_log_group(logGroupName=lg, tags=TAGS)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceAlreadyExistsException":
                raise
        logs[r].put_retention_policy(logGroupName=lg, retentionInDays=7)

    # SNS alerts
    sns = session.client("sns", region_name=DR)
    sns.create_topic(Name=f"{P}-alerts", Tags=tag_list())
    subs = sns.list_subscriptions_by_topic(TopicArn=topic_arn).get("Subscriptions", [])
    if not any(s["Endpoint"] == email for s in subs):
        sns.subscribe(TopicArn=topic_arn, Protocol="email", Endpoint=email)
        log("SNS email subscription requested: confirm the email from AWS to receive alerts")

    # Lambdas
    code = build_zip()
    endpoints = st.get("endpoints", {})
    for r in REGIONS:
        ensure_function(lam[r], f"{P}-app", app_role, code, APP_HANDLER, APP_MEMORY, APP_TIMEOUT, {})
        url = ensure_url(lam[r], f"{P}-app")
        endpoints[r] = url
    st["endpoints"] = endpoints
    save_state(st)
    common = {"DR_HOME_REGION": HOME, "DR_BUCKETS": json.dumps(buckets), "DR_ENDPOINTS": json.dumps(endpoints)}
    for r in REGIONS:
        env = {**common, "DR_REGION": r, "DR_FAULT_PARAM": f"/{P}/faults/{r}", "DR_OUTAGE_HANG_S": "4"}
        update_env(lam[r], f"{P}-app", env)
    ensure_function(lam[DR], f"{P}-controller", ctl_role, code, CTL_HANDLER, CTL_MEMORY, 300,
                    {**common, "DR_REGION": DR, "DR_TOPIC_ARN": topic_arn})
    update_env(lam[DR], f"{P}-controller", {**common, "DR_REGION": DR, "DR_TOPIC_ARN": topic_arn})

    # Control object: bootstrap in home, wait for it to replicate to DR
    home_store = S3ObjectStore(buckets[HOME], HOME, client=s3[HOME])
    dr_store = S3ObjectStore(buckets[DR], DR, client=s3[DR])
    bootstrap_control(home_store, HOME)
    log("control object bootstrapped in home; waiting for replication to DR")
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            dr_store.get(CONTROL_KEY)
            break
        except Exception:  # noqa: BLE001
            time.sleep(5)
    else:
        raise SystemExit("control object did not replicate within 10 minutes")
    log("control object present in DR")

    # Scheduler: controller tick every minute
    sched = session.client("scheduler", region_name=DR)
    kwargs = dict(Name=f"{P}-controller-tick", ScheduleExpression="rate(1 minute)", FlexibleTimeWindow={"Mode": "OFF"},
                  Target={"Arn": ctl_fn_arn, "RoleArn": sched_role, "RetryPolicy": {"MaximumRetryAttempts": 0}}, State="ENABLED",
                  Description="DR controller tick (drsystem)")
    try:
        retry(lambda: sched.create_schedule(**kwargs), "scheduler role")
        log("schedule created: controller runs every minute")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConflictException":
            raise
        sched.update_schedule(**kwargs)
    st["deployed_at"] = time.time()
    save_state(st)
    log("DEPLOYED")


def ensure_function(lam, name, role, code, handler, memory, timeout, env):
    try:
        lam.get_function(FunctionName=name)
        exists = True
    except ClientError:
        exists = False
    if exists:
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
        lam.update_function_code(FunctionName=name, ZipFile=code)
        log(f"function code updated: {name} ({lam.meta.region_name})")
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
        return
    try:
        retry(lambda: lam.create_function(
            FunctionName=name, Runtime="python3.12", Architectures=["arm64"], Role=role, Handler=handler,
            Code={"ZipFile": code}, MemorySize=memory, Timeout=timeout, Environment={"Variables": env}, Tags=TAGS,
            Description="drsystem (multi-region DR lab)"), f"create {name}")
        log(f"function created: {name} ({lam.meta.region_name})")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceConflictException":
            raise
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
        lam.update_function_code(FunctionName=name, ZipFile=code)
        log(f"function code updated: {name} ({lam.meta.region_name})")
    lam.get_waiter("function_active_v2").wait(FunctionName=name)
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)


def ensure_url(lam, name) -> str:
    try:
        return lam.create_function_url_config(FunctionName=name, AuthType="AWS_IAM")["FunctionUrl"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceConflictException":
            raise
        return lam.get_function_url_config(FunctionName=name)["FunctionUrl"]


def update_env(lam, name, env):
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)
    retry(lambda: lam.update_function_configuration(FunctionName=name, Environment={"Variables": env}), f"env {name}")
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)


def update_code(session: boto3.Session, debug: bool = False) -> None:
    from botocore.config import Config

    cfg = Config(connect_timeout=30, read_timeout=300, retries={"max_attempts": 5, "mode": "standard"})
    code = build_zip()
    for r, fn in [(HOME, f"{P}-app"), (DR, f"{P}-app"), (DR, f"{P}-controller")]:
        lam = session.client("lambda", region_name=r, config=cfg)
        lam.get_waiter("function_updated_v2").wait(FunctionName=fn)
        lam.update_function_code(FunctionName=fn, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=fn)
        is_app = fn.endswith("-app")
        lam.update_function_configuration(
            FunctionName=fn, Handler=APP_HANDLER if is_app else CTL_HANDLER,
            MemorySize=APP_MEMORY if is_app else CTL_MEMORY, Timeout=APP_TIMEOUT if is_app else 300)
        lam.get_waiter("function_updated_v2").wait(FunctionName=fn)
        if debug:
            env = lam.get_function_configuration(FunctionName=fn)["Environment"]["Variables"]
            env["DR_DEBUG_HANG_S"] = "7"
            lam.update_function_configuration(FunctionName=fn, Environment={"Variables": env})
            lam.get_waiter("function_updated_v2").wait(FunctionName=fn)
        log(f"code updated: {fn} ({r})")


# -- status ---------------------------------------------------------------------------------------
def status(session: boto3.Session) -> int:
    import os

    from drsystem.aws.sigv4 import LambdaUrlSigV4
    from drsystem.control import read_control
    from drsystem.orchestrator.probe import HttpProber

    st = load_state()
    if not st.get("endpoints"):
        print("not deployed")
        return 1
    prober = HttpProber(st["endpoints"], 8.0, auth=LambdaUrlSigV4(session))
    ok = True
    for r in REGIONS:
        p = prober.probe(r)
        ok &= p.ok
        print(f"{r}: ok={p.ok} status={p.status} ready={p.ready} active={p.active} epoch={p.epoch} latency={p.latency_ms:.0f}ms {p.error}")
    for r in REGIONS:
        s = S3ObjectStore(st["buckets"][r], r, client=session.client("s3", region_name=r))
        c, _ = read_control(s)
        print(f"control@{r}: active={c.active_region} epoch={c.epoch} demoted={c.demoted_region} freeze={c.write_freeze}")
    os.environ.setdefault("AWS_DEFAULT_REGION", DR)
    return 0 if ok else 1


# -- teardown -------------------------------------------------------------------------------------
def teardown(session: boto3.Session) -> int:
    st = load_state()
    acct = session.client("sts").get_caller_identity()["Account"]
    buckets = st.get("buckets", {})
    errors = []

    def attempt(fn, what):
        try:
            fn()
            log(f"deleted {what}")
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code not in {"ResourceNotFoundException", "NoSuchEntity", "NoSuchBucket", "NotFound", "ParameterNotFound",
                            "NoSuchReplicationConfiguration"}:
                errors.append(f"{what}: {code}")

    sched = session.client("scheduler", region_name=DR)
    attempt(lambda: sched.delete_schedule(Name=f"{P}-controller-tick"), "schedule")
    lam_dr = session.client("lambda", region_name=DR)
    attempt(lambda: lam_dr.delete_function(FunctionName=f"{P}-controller"), "controller function")
    for r in REGIONS:
        lam = session.client("lambda", region_name=r)
        attempt(lambda lam=lam: lam.delete_function(FunctionName=f"{P}-app"), f"app function {r}")
    sns = session.client("sns", region_name=DR)
    attempt(lambda: sns.delete_topic(TopicArn=f"arn:aws:sns:{DR}:{acct}:{P}-alerts"), "SNS topic")
    for r in REGIONS:
        ssm = session.client("ssm", region_name=r)
        attempt(lambda ssm=ssm, r=r: ssm.delete_parameter(Name=f"/{P}/faults/{r}"), f"SSM parameter {r}")
    for r, b in buckets.items():
        s3 = session.client("s3", region_name=r)
        attempt(lambda s3=s3, b=b: s3.delete_bucket_replication(Bucket=b), f"replication {b}")
    time.sleep(5)
    for _ in range(3):
        remaining = []
        for r, b in buckets.items():
            s3 = session.client("s3", region_name=r)
            try:
                for page in s3.get_paginator("list_object_versions").paginate(Bucket=b):
                    items = [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in page.get("Versions", []) + page.get("DeleteMarkers", [])]
                    for i in range(0, len(items), 1000):
                        s3.delete_objects(Bucket=b, Delete={"Objects": items[i:i + 1000], "Quiet": True})
                s3.delete_bucket(Bucket=b)
                log(f"deleted bucket {b}")
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "NoSuchBucket":
                    remaining.append(b)
        if not remaining:
            break
        time.sleep(15)
    else:
        errors.append(f"buckets not deleted: {remaining}")
    for r, fn in [(HOME, f"{P}-app"), (DR, f"{P}-app"), (DR, f"{P}-controller")]:
        logs = session.client("logs", region_name=r)
        attempt(lambda logs=logs, fn=fn: logs.delete_log_group(logGroupName=f"/aws/lambda/{fn}"), f"log group {fn} {r}")
    iam = session.client("iam")
    for role in (f"{P}-app-role", f"{P}-controller-role", f"{P}-replication-role", f"{P}-scheduler-role"):
        attempt(lambda role=role: iam.delete_role_policy(RoleName=role, PolicyName="inline"), f"policy {role}")
        attempt(lambda role=role: iam.delete_role(RoleName=role), f"role {role}")
    # Verify
    left = {}
    for r in (*REGIONS, "us-east-1"):
        tagging = session.client("resourcegroupstaggingapi", region_name=r)
        arns = [m["ResourceARN"] for m in tagging.get_resources(TagFilters=[{"Key": "Project", "Values": ["dr-system"]}])["ResourceTagMappingList"]]
        if arns:
            left[r] = arns
    roles_left = [x["RoleName"] for page in iam.get_paginator("list_roles").paginate() for x in page["Roles"] if x["RoleName"].startswith(P)]
    print(json.dumps({"errors": errors, "tagged_resources_left": left, "roles_left": roles_left}, indent=2))
    if not errors and not roles_left:
        STATE.unlink(missing_ok=True)
        log("TEARDOWN COMPLETE (tag index can lag a few minutes; rerun teardown to re-verify)")
        return 0
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["deploy", "update-code", "status", "teardown"])
    ap.add_argument("--profile", required=True)
    ap.add_argument("--email", help="alert email (deploy)")
    a = ap.parse_args()
    session = boto3.Session(profile_name=a.profile)
    if a.action == "deploy":
        if not a.email:
            raise SystemExit("--email is required for deploy")
        pre = run_preflight(a.profile)
        print(pre.text())
        if not pre.ok:
            return 2
        deploy(session, a.email)
        return 0
    if a.action == "status":
        return status(session)
    if a.action == "update-code":
        update_code(session, debug=False)
        return 0
    return teardown(session)


if __name__ == "__main__":
    raise SystemExit(main())
