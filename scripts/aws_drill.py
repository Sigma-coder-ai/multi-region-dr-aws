"""REAL-AWS drills against the deployed system. Results are labelled REAL-AWS.

  python scripts/aws_drill.py hard-outage --profile mahesh-project --failback
  python scripts/aws_drill.py gray-errors --profile mahesh-project --failback
  python scripts/aws_drill.py health-blip --profile mahesh-project

Faults are injected the same way an operator would on AWS:
- hard-outage: the home app hangs (SSM fault flag `outage`) AND home->DR S3 replication is paused,
               so writes not yet replicated are stranded (this is what creates real RPO)
- gray-errors: the home app answers 70% of requests with HTTP 500
- health-blip: the home app's health check fails for ~75 s (about one controller tick); must NOT fail over

Detection and failover are done by the deployed controller Lambda (every minute), not by this script.
Failback is human-approved, so this script runs it from the operator's laptop after the cool-down.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SCENARIOS = {
    "hard-outage": ({"outage": True}, True, True, None),
    "gray-errors": ({"error_rate": 0.7}, True, False, None),
    "health-blip": ({"health_fail": True}, False, False, 75),
}
DESCRIPTIONS = {
    "hard-outage": "Home app unreachable and home->DR replication paused (stranded writes).",
    "gray-errors": "Home app answers 70% of requests with HTTP 500.",
    "health-blip": "Home health check fails for ~75 s (one controller tick). Must not fail over.",
}


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Traffic:
    def __init__(self, client_factory, drill_id: str, write_interval: float = 1.0, read_interval: float = 2.0):
        from drsystem.drills.metrics import ReadSample, WriteRecord

        self.WriteRecord, self.ReadSample = WriteRecord, ReadSample
        self.client_factory = client_factory
        self.drill_id = drill_id
        self.write_interval, self.read_interval = write_interval, read_interval
        self.writes, self.reads = [], []
        self._stop = threading.Event()
        self._threads = []

    def start(self):
        self._threads = [threading.Thread(target=self._writer, daemon=True), threading.Thread(target=self._reader, daemon=True)]
        for t in self._threads:
            t.start()

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=120)

    def _writer(self):
        c = self.client_factory()
        seq = 0
        while not self._stop.is_set():
            r = c.write("drill/seq", {"drill_id": self.drill_id, "seq": seq}, deadline_s=600)
            self.writes.append(self.WriteRecord(seq, r.first_attempt_at, r.acked_at, r.region, r.ok, r.attempts))
            seq += 1
            self._stop.wait(self.write_interval)

    def _reader(self):
        c = self.client_factory()
        while not self._stop.is_set():
            at, t = time.time(), time.perf_counter()
            try:
                r = c.get("whoami")
                ok = r.status_code == 200
                region = r.json().get("region") if ok else None
            except Exception:  # noqa: BLE001
                ok, region = False, None
            self.reads.append(self.ReadSample(at, ok, region, (time.perf_counter() - t) * 1000))
            self._stop.wait(self.read_interval)


def set_replication(s3, bucket: str, enabled: bool) -> None:
    cfg = s3.get_bucket_replication(Bucket=bucket)["ReplicationConfiguration"]
    for rule in cfg["Rules"]:
        rule["Status"] = "Enabled" if enabled else "Disabled"
    s3.put_bucket_replication(Bucket=bucket, ReplicationConfiguration=cfg)


def seqs_in(store, drill_id: str) -> set[int]:
    return {int(k.rsplit("/", 1)[1].split(".")[0]) for k in store.list(f"seq/{drill_id}/")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=list(SCENARIOS))
    ap.add_argument("--profile", required=True)
    ap.add_argument("--failback", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "evidence" / "real-aws" / "drills"))
    a = ap.parse_args()

    st = json.loads((ROOT / ".aws-deploy.json").read_text())
    os.environ["AWS_PROFILE"] = a.profile
    os.environ["DR_HOME_REGION"] = st["home"]
    os.environ["DR_BUCKETS"] = json.dumps(st["buckets"])
    os.environ["DR_ENDPOINTS"] = json.dumps(st["endpoints"])

    import boto3

    from drsystem.aws.events_sink import read_events
    from drsystem.aws.lambda_handlers import build_control_plane
    from drsystem.aws.preflight import run_preflight
    from drsystem.control import read_control
    from drsystem.drills.metrics import compute
    from drsystem.drills.report import write_reports
    from drsystem.drills.runner import DrillResult
    from drsystem.notify import OutboxNotifier
    from drsystem.orchestrator.workflows import FailbackWorkflow

    pre = run_preflight(a.profile)
    if not pre.ok:
        print(pre.text())
        return 2
    home, dr = st["home"], st["dr"]
    fault, expect_failover, pause_replication, clear_after = SCENARIOS[a.scenario]
    cp = build_control_plane(dr)
    stores, ssm = cp["stores"], boto3.client("ssm", region_name=home)
    s3_home = boto3.client("s3", region_name=home)
    fault_param = f"/drsys/faults/{home}"

    # Baseline
    c, _ = read_control(stores[dr])
    if c.active_region != home or c.write_freeze:
        log(f"ABORT: baseline not clean (active={c.active_region}, freeze={c.write_freeze})")
        return 2
    for r in (home, dr):
        p = cp["prober"].probe(r)
        if not p.ok:
            log(f"ABORT: {r} not healthy before drill ({p.status} {p.error})")
            return 2
    drill_id = f"aws-{a.scenario}-{uuid.uuid4().hex[:6]}"
    log(f"[REAL-AWS] drill {drill_id}: baseline healthy, starting traffic")
    traffic = Traffic(cp["client_factory"], drill_id)
    traffic.start()
    time.sleep(30)

    t0 = time.time()
    if pause_replication:
        set_replication(s3_home, st["buckets"][home], enabled=False)
    ssm.put_parameter(Name=fault_param, Value=json.dumps(fault), Type="String", Overwrite=True)
    log(f"fault injected at t0: {fault}{' + home->DR replication paused' if pause_replication else ''}")
    if clear_after:
        time.sleep(clear_after)
        ssm.put_parameter(Name=fault_param, Value="{}", Type="String", Overwrite=True)
        log("transient fault cleared")

    promoted_at = None
    if expect_failover:
        deadline = time.time() + 15 * 60
        while time.time() < deadline:
            c, _ = read_control(stores[dr])
            if c.active_region == dr:
                promoted_at = c.changed_at
                log(f"failover detected: {dr} active at t0+{promoted_at - t0:.0f}s (epoch {c.epoch})")
                break
            time.sleep(5)
        # let the controller finish verification
        end = time.time() + 240
        while time.time() < end and not any(e["kind"] == "workflow_complete" for e in read_events(stores[dr], t0)):
            time.sleep(5)
    else:
        time.sleep(4 * 60)
    time.sleep(30)
    traffic.stop()
    present = seqs_in(stores[dr], drill_id)

    events = read_events(stores[dr], t0)
    first = lambda kind, **m: next((e for e in events if e["kind"] == kind and all(e.get(k) == v for k, v in m.items())), None)  # noqa: E731
    ev = {
        "t1_first_failed_probe": first("first_failed_probe", region=home),
        "t2_decision": first("decision"),
        "t3_promoted": first("promoted", region=dr),
        "alert": first("alert"),
    }
    t1, t2, t3 = (ev[k]["t"] if ev[k] else None for k in ("t1_first_failed_probe", "t2_decision", "t3_promoted"))
    metrics = compute(t0, t1, t2, t3, home, dr, traffic.writes, traffic.reads, present)
    wf_done = first("workflow_complete")
    steps = [{k: v for k, v in e.items() if k not in {"t", "kind", "label", "run_id", "workflow", "wf_run"}}
             for e in events if e["kind"] == "wf_step_end" and wf_done and e.get("wf_run") == wf_done.get("wf_run")]

    # Recovery of the failed region
    ssm.put_parameter(Name=fault_param, Value="{}", Type="String", Overwrite=True)
    if pause_replication:
        set_replication(s3_home, st["buckets"][home], enabled=True)
    log("fault cleared" + (", replication re-enabled" if pause_replication else ""))

    result = DrillResult(
        label="REAL-AWS", run_id=st["suffix"], drill_id=drill_id, scenario=a.scenario, description=DESCRIPTIONS[a.scenario],
        source=home, target=dr, t0=t0,
        events={k: (round(v["t"] - t0, 1) if v else None) for k, v in ev.items()},
        metrics=metrics.to_dict(),
        failover={"name": "failover", "run_id": wf_done.get("wf_run"), "ok": wf_done.get("ok"), "outcome": wf_done.get("outcome"),
                  "steps": steps, "details": {}} if wf_done else None,
        settings={"controller_tick_s": 60, "probe_timeout_s": cp["settings"].probe.timeout_s,
                  "scorer": asdict(cp["settings"].scorer), "workflow": asdict(cp["settings"].workflow),
                  "write_interval_s": 1.0, "read_interval_s": 2.0},
    )
    active_now = read_control(stores[dr])[0].active_region
    chk = lambda name, ok, obs: result.checks.append({"check": name, "passed": bool(ok), "observed": obs})  # noqa: E731
    if expect_failover:
        chk("failover workflow completed", wf_done is not None and wf_done.get("ok"), wf_done and wf_done.get("outcome"))
        chk("DR region holds write authority", active_now == dr, active_now)
        chk("writes resumed in DR", metrics.write_rto_s is not None, metrics.write_rto_s)
    else:
        chk("no failover happened", t3 is None, ev["t3_promoted"] and "promoted")
        chk("home region still active", active_now == home, active_now)

    if a.failback and expect_failover and active_now == dr:
        c, _ = read_control(stores[dr])
        wait = cp["settings"].workflow.cooldown_s - (time.time() - c.changed_at)
        if wait > 0:
            log(f"waiting {wait:.0f}s for the cool-down before the approved failback")
            time.sleep(wait + 5)
        fb_id = f"{drill_id}-fb"
        fb_traffic = Traffic(cp["client_factory"], fb_id)
        fb_traffic.start()
        time.sleep(20)
        tf = time.time()
        log("running human-approved failback")
        wf = FailbackWorkflow(notifier=OutboxNotifier(), **cp).run("mahesh (operator, approved for REAL-AWS drill)")
        time.sleep(30)
        fb_traffic.stop()
        log(f"failback outcome: {wf.outcome}; waiting for replication to settle")
        time.sleep(60)
        acked = {w.seq for w in fb_traffic.writes if w.ok}
        in_home = seqs_in(stores[home], fb_id)
        fb_events = read_events(stores[dr], tf)
        freeze = next((e for e in fb_events if e["kind"] == "write_freeze"), None)
        prom = next((e for e in fb_events if e["kind"] == "promoted" and e.get("region") == home), None)
        result.failback = {
            "outcome": wf.outcome,
            "duration_s": round(sum(s["duration_s"] for s in wf.steps), 1),
            "write_freeze_s": round(prom["t"] - freeze["t"], 1) if freeze and prom else None,
            "steps": wf.steps,
            "writes_acked": len(acked),
            "lost_writes": len(acked - in_home),
        }
        chk("failback completed", wf.outcome == "failed_back", wf.outcome)
        chk("failback lost 0 writes (planned RPO = 0)", len(acked - in_home) == 0, len(acked - in_home))

    out = Path(a.out) / time.strftime("%Y%m%d")
    summary = write_reports(out, [result], "REAL-AWS")
    # Rebuild SUMMARY.md from every REAL-AWS drill in the folder, not just this run.
    from drsystem.drills.report import suite_markdown

    allr = []
    for f in sorted(out.glob("*.json")):
        j = json.loads(f.read_text(encoding="utf-8"))
        j.pop("passed", None)
        allr.append(DrillResult(**j))
    summary.write_text(suite_markdown("REAL-AWS", allr), encoding="utf-8")
    m = result.metrics
    log(f"[REAL-AWS] {a.scenario}: {'PASS' if result.passed else 'FAIL'} detect={m['detection_s']}s decision={m['decision_s']}s "
        f"promotion={m['promotion_s']}s write_rto={m['write_rto_s']}s read_outage={m['read_outage_s']}s "
        f"lost_writes={m['lost_writes']} rpo={m['rpo_s']}s"
        + (f" failback={result.failback['outcome']} fb_lost={result.failback['lost_writes']}" if result.failback else ""))
    log(f"report: {summary.parent}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
