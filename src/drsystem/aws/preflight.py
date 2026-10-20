"""Zero-spend preflight: READ-ONLY checks of the AWS account before any deployment.

Safety: every boto3 client created here gets a ``before-call`` hook that blocks any operation not
on the read-only allowlist. Even a coding mistake cannot create, modify or upgrade anything.
The Free Tier API also has ``UpgradeAccountPlan``; it is deliberately not allowlisted.

Hard gates (any failure -> exit code 1, do not deploy)
  - account plan is FREE and ACTIVE
  - remaining credits >= min_credits_usd and plan expiry >= min_days_left away
  - root MFA enabled and no root access keys
  - both project regions enabled
Soft findings (reported for review): not using root for the CLI, Organizations membership,
per-service API reachability in each region, Lambda concurrency quota.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

READ_ONLY_OPERATIONS = frozenset(
    {
        "GetCallerIdentity",
        "GetAccountPlanState",
        "GetAccountSummary",
        "DescribeOrganization",
        "ListRegions",
        "GetAccountSettings",
        "ListStateMachines",
        "ListSchedules",
        "ListTopics",
        "DescribeParameters",
        "DescribeAlarms",
        "ListBuckets",
        "ListDistributions2020_05_31",
        "ListDistributions",
    }
)


class BlockedOperation(RuntimeError):
    pass


def _guard(model, **_kwargs) -> None:
    if model.name not in READ_ONLY_OPERATIONS:
        raise BlockedOperation(f"preflight is read-only; blocked {model.service_model.service_name}:{model.name}")


def guarded_client(session, service: str, region: str):
    client = session.client(service, region_name=region)
    client.meta.events.register("before-call.*.*", _guard)
    return client


@dataclass
class Finding:
    check: str
    status: str  # PASS | FAIL | WARN | INFO
    detail: str
    hard_gate: bool = False


@dataclass
class PreflightReport:
    findings: list[Finding] = field(default_factory=list)

    def add(self, check: str, status: str, detail: str, hard_gate: bool = False) -> None:
        self.findings.append(Finding(check, status, detail, hard_gate))

    @property
    def ok(self) -> bool:
        return not any(f.hard_gate and f.status != "PASS" for f in self.findings)

    def text(self) -> str:
        lines = ["PREFLIGHT (read-only, $0)", "=" * 72]
        for f in self.findings:
            gate = " [GATE]" if f.hard_gate else ""
            lines.append(f"{f.status:<5} {f.check}{gate}: {f.detail}")
        lines.append("=" * 72)
        lines.append("RESULT: OK to propose a deployment for review" if self.ok else "RESULT: STOP. Hard gate failed, do not deploy")
        return "\n".join(lines)


def _err_code(exc: Exception) -> str:
    resp = getattr(exc, "response", None) or {}
    return resp.get("Error", {}).get("Code", type(exc).__name__)


def run_preflight(
    profile: str | None,
    regions: tuple[str, ...] = ("ap-south-1", "ap-southeast-1"),
    min_credits_usd: float = 50.0,
    min_days_left: int = 30,
    session: Any | None = None,
) -> PreflightReport:
    import boto3

    session = session or boto3.Session(profile_name=profile)
    rep = PreflightReport()

    # Identity
    try:
        ident = guarded_client(session, "sts", regions[0]).get_caller_identity()
        is_root = ident["Arn"].endswith(":root")
        rep.add("caller identity", "WARN" if is_root else "PASS", ("ROOT user, use an IAM user instead" if is_root else ident["Arn"]))
    except Exception as exc:  # noqa: BLE001
        rep.add("caller identity", "FAIL", f"cannot authenticate: {_err_code(exc)} (run `aws login` first)", True)
        return rep

    # Account plan + credits (Free Tier API lives in us-east-1)
    try:
        st = guarded_client(session, "freetier", "us-east-1").get_account_plan_state()
        plan, status = st.get("accountPlanType"), st.get("accountPlanStatus")
        rep.add("account plan", "PASS" if (plan, status) == ("FREE", "ACTIVE") else "FAIL", f"{plan} / {status}", True)
        credits = st.get("accountPlanRemainingCredits", {})
        amount = float(credits.get("amount", 0))
        rep.add("remaining credits", "PASS" if amount >= min_credits_usd else "FAIL", f"{amount} {credits.get('unit', 'USD')} (min {min_credits_usd})", True)
        exp = st.get("accountPlanExpirationDate")
        if exp:
            days = (exp - dt.datetime.now(dt.timezone.utc)).days
            rep.add("plan expiry", "PASS" if days >= min_days_left else "FAIL", f"{exp:%Y-%m-%d} ({days} days left, min {min_days_left})", True)
        else:
            rep.add("plan expiry", "FAIL", "no expiration date returned; verify in the Billing console", True)
    except Exception as exc:  # noqa: BLE001
        rep.add("account plan", "FAIL", f"could not read plan state: {_err_code(exc)}; verify in the Billing console", True)

    # Root hygiene
    try:
        summary = guarded_client(session, "iam", "us-east-1").get_account_summary()["SummaryMap"]
        rep.add("root MFA", "PASS" if summary.get("AccountMFAEnabled") == 1 else "FAIL", f"AccountMFAEnabled={summary.get('AccountMFAEnabled')}", True)
        rep.add("root access keys", "PASS" if summary.get("AccountAccessKeysPresent") == 0 else "FAIL", f"AccountAccessKeysPresent={summary.get('AccountAccessKeysPresent')}", True)
    except Exception as exc:  # noqa: BLE001
        rep.add("root MFA", "FAIL", f"cannot read account summary: {_err_code(exc)}", True)

    # Organizations (joining one auto-upgrades a Free plan account)
    try:
        guarded_client(session, "organizations", "us-east-1").describe_organization()
        rep.add("AWS Organizations", "WARN", "account is in an organization; check the plan was not auto-upgraded")
    except Exception as exc:  # noqa: BLE001
        code = _err_code(exc)
        rep.add("AWS Organizations", "PASS" if code == "AWSOrganizationsNotInUseException" else "INFO", code)

    # Regions
    try:
        resp = guarded_client(session, "account", "us-east-1").list_regions(
            RegionOptStatusContains=["ENABLED", "ENABLED_BY_DEFAULT"], MaxResults=50
        )
        enabled = {r["RegionName"] for r in resp.get("Regions", [])}
        for r in regions:
            rep.add(f"region {r}", "PASS" if r in enabled else "FAIL", "enabled" if r in enabled else "not enabled", True)
    except Exception as exc:  # noqa: BLE001
        rep.add("regions", "FAIL", f"cannot list regions: {_err_code(exc)}", True)

    # Per-service reachability (an AccessDenied / SubscriptionRequired here hints at a plan restriction)
    probes = [
        ("lambda", lambda c: c.get_account_settings()),
        ("stepfunctions", lambda c: c.list_state_machines(maxResults=1)),
        ("scheduler", lambda c: c.list_schedules(MaxResults=1)),
        ("sns", lambda c: c.list_topics()),
        ("ssm", lambda c: c.describe_parameters(MaxResults=1)),
        ("cloudwatch", lambda c: c.describe_alarms(MaxRecords=1)),
    ]
    for region in regions:
        for service, call in probes:
            try:
                out = call(guarded_client(session, service, region))
                detail = "reachable"
                if service == "lambda":
                    limit = out.get("AccountLimit", {}).get("ConcurrentExecutions")
                    detail = f"reachable; concurrent executions quota = {limit}"
                rep.add(f"{service} @ {region}", "PASS", detail)
            except BlockedOperation:
                raise
            except Exception as exc:  # noqa: BLE001
                rep.add(f"{service} @ {region}", "WARN", f"{_err_code(exc)}; check the console for a plan restriction")
    for service, call in [("s3", lambda c: c.list_buckets()), ("cloudfront", lambda c: c.list_distributions(MaxItems="1"))]:
        try:
            call(guarded_client(session, service, "us-east-1"))
            rep.add(f"{service} (global)", "PASS", "reachable")
        except Exception as exc:  # noqa: BLE001
            rep.add(f"{service} (global)", "WARN", f"{_err_code(exc)}; check the console for a plan restriction")
    rep.add("cross-region S3 replication", "INFO", "cannot be verified read-only; needs the approved Gate 1 micro-test")
    return rep
