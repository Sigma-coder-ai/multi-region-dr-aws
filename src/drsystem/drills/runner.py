"""Drill runner: inject a fault under live traffic, wait for recovery, measure, optionally fail back.

Traffic during a drill
- sequence writer: writes seq 0,1,2,... through the client (client-side write failover)
- user reader: GET /api/whoami through the edge every ``read_interval_s``

Works against any ``DrillTarget``; ``LocalCluster`` is the SIMULATED implementation. A future
AWS target (real faults via SSM, real CloudFront URL) plugs in without changing this file.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

import httpx

from ..control import read_control
from ..storage.base import StoreUnavailable
from .metrics import ReadSample, RecoveryMetrics, WriteRecord, compute


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    fault: dict
    expect_failover: bool
    fault_region: str = "home"  # "home" or "standby"
    pre_fault: dict | None = None  # fault applied to the standby before the main fault
    clear_after_s: float | None = None  # transient faults (blips)
    observe_s: float = 8.0  # how long to watch when no failover is expected


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in [
        Scenario("hard-outage", "Primary region unreachable (requests hang, replication stops).", {"outage": True}, True),
        Scenario("gray-errors", "Primary answers 70% of requests with HTTP 500 (partial failure).", {"error_rate": 0.7}, True),
        Scenario("storage-down", "Primary's data store fails; app stays up but is not ready.", {"storage_down": True}, True),
        Scenario(
            "health-blip",
            "Primary health check fails for ~0.6 s, then recovers. Anti-flapping must prevent failover.",
            {"health_fail": True},
            False,
            clear_after_s=0.6,
        ),
        Scenario(
            "standby-unhealthy",
            "DR application already unhealthy when the primary fails. Must ALERT, not fail over to a broken region.",
            {"outage": True},
            False,
            pre_fault={"health_fail": True},
            observe_s=8.0,
        ),
    ]
}


@dataclass
class DrillResult:
    label: str
    run_id: str
    drill_id: str
    scenario: str
    description: str
    source: str
    target: str
    t0: float
    events: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    failover: dict | None = None
    failback: dict | None = None
    checks: list[dict] = field(default_factory=list)
    late_arrivals: int = 0
    retried_workflows: list[str] = field(default_factory=list)
    settings: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(c["passed"] for c in self.checks)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["passed"] = self.passed
        return d


class _Traffic:
    def __init__(self, cluster, drill_id: str) -> None:
        self.cluster = cluster
        self.drill_id = drill_id
        self.writes: list[WriteRecord] = []
        self.reads: list[ReadSample] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._seq = 0

    def start(self) -> None:
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._writer, daemon=True, name="drill-writer"),
            threading.Thread(target=self._reader, daemon=True, name="drill-reader"),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=self.cluster.settings.drill.write_deadline_s + 5)

    def _writer(self) -> None:
        cfg = self.cluster.settings.drill
        client = self.cluster.client()
        try:
            while not self._stop.is_set():
                seq = self._seq
                self._seq += 1
                r = client.write("drill/seq", {"drill_id": self.drill_id, "seq": seq}, deadline_s=cfg.write_deadline_s)
                self.writes.append(WriteRecord(seq, r.first_attempt_at, r.acked_at, r.region, r.ok, r.attempts))
                self._stop.wait(cfg.write_interval_s)
        finally:
            client.close()

    def _reader(self) -> None:
        cfg = self.cluster.settings.drill
        http = httpx.Client(timeout=self.cluster.settings.router.origin_timeout_s * 3)
        try:
            while not self._stop.is_set():
                at = time.time()
                t = time.perf_counter()
                try:
                    r = http.get(self.cluster.edge_url + "/api/whoami")
                    ok = r.status_code == 200
                    region = r.json().get("region") if ok else None
                    failover = r.headers.get("x-origin-failover") == "1"
                except (httpx.HTTPError, ValueError):
                    ok, region, failover = False, None, False
                self.reads.append(ReadSample(at, ok, region, (time.perf_counter() - t) * 1000, failover))
                self._stop.wait(cfg.read_interval_s)
        finally:
            http.close()


class DrillRunner:
    def __init__(self, cluster) -> None:
        self.c = cluster

    def _region(self, which: str) -> str:
        return self.c.home if which == "home" else self.c.standby

    def wait_healthy(self, timeout_s: float = 20.0) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            d = self.c.controller.last_decision
            try:
                active = read_control(self.c.stores[self.c.standby])[0].active_region
            except StoreUnavailable:
                active = None
            if d is not None and d.action == "NONE" and active == self.c.home and d.score >= 90:
                return
            time.sleep(0.1)
        raise TimeoutError("cluster did not reach a healthy baseline")

    def _seq_present(self, region: str, drill_id: str) -> set[int]:
        keys = self.c.stores[region].list(f"seq/{drill_id}/")
        return {int(k.rsplit("/", 1)[1].split(".")[0]) for k in keys}

    def run(self, scenario_name: str, failback: bool = False) -> DrillResult:
        sc = SCENARIOS[scenario_name]
        s = self.c.settings
        self.wait_healthy()
        source = self._region(sc.fault_region)
        target = s.peer_of(source)
        drill_id = f"{scenario_name}-{uuid.uuid4().hex[:6]}"
        traffic = _Traffic(self.c, drill_id)
        result = DrillResult(
            label=self.c.timeline.label,
            run_id=self.c.run_id,
            drill_id=drill_id,
            scenario=sc.name,
            description=sc.description,
            source=source,
            target=target,
            t0=0.0,
            settings={
                "probe_interval_s": s.probe.interval_s,
                "probe_timeout_s": s.probe.timeout_s,
                "scorer": asdict(s.scorer),
                "replication_lag_s": s.replication.lag_s,
                "origin_timeout_s": s.router.origin_timeout_s,
                "drain_max_s": s.workflow.drain_max_s,
            },
        )
        n_workflows = len(self.c.workflows)
        traffic.start()
        time.sleep(1.5)  # baseline traffic before the fault
        if sc.pre_fault:
            self.c.inject(target, **sc.pre_fault)
            time.sleep(1.0)
        t0 = time.time()
        result.t0 = t0
        self.c.inject(source, **sc.fault)
        if sc.clear_after_s:
            time.sleep(sc.clear_after_s)
            self.c.clear(source)

        wf = None
        if sc.expect_failover:
            # Wait for the decisive workflow. False alarms / lock contention are retried by the
            # controller, so they are not the end of the drill.
            retried = {"false_alarm", "lock_held"}
            deadline = time.time() + s.workflow.verify_timeout_s + 30
            while time.time() < deadline:
                done = [w for w in self.c.workflows[n_workflows:] if w.outcome not in retried]
                if done:
                    wf = done[0]
                    break
                time.sleep(0.1)
            result.retried_workflows = [w.outcome for w in self.c.workflows[n_workflows:] if w.outcome in retried]
        else:
            time.sleep(sc.observe_s)
        time.sleep(s.drill.settle_s)
        traffic.stop()

        tl = self.c.timeline
        ev = {
            "t1_first_failed_probe": tl.first("first_failed_probe", since=t0, region=source),
            "t2_decision": tl.first("decision", since=t0),
            "t3_promoted": tl.first("promoted", since=t0, region=target),
            "alert": tl.first("alert", since=t0),
        }
        t1, t2, t3 = (ev[k]["t"] if ev[k] else None for k in ("t1_first_failed_probe", "t2_decision", "t3_promoted"))
        present = self._seq_present(target, drill_id) if not self.c.faults[target].current().outage else set()
        metrics: RecoveryMetrics = compute(t0, t1, t2, t3, source, target, traffic.writes, traffic.reads, present)
        result.metrics = metrics.to_dict()
        result.events = {k: (round(v["t"] - t0, 3) if v else None) for k, v in ev.items()}
        result.failover = asdict(wf) if wf else None

        # Recovery of the failed region: replication resumes; count lost writes that arrive late.
        self.c.clear(source)
        if sc.pre_fault:
            self.c.clear(target)
        time.sleep(s.replication.lag_s + s.replication.jitter_s + 1.0)
        if t3 and metrics.lost_writes:
            after = self._seq_present(target, drill_id)
            lost_seqs = {w.seq for w in traffic.writes if w.ok and w.region == source and w.acked_at and w.acked_at < t3} - present
            result.late_arrivals = len(lost_seqs & after)

        self._checks(result, sc, wf, metrics)
        if failback and wf and wf.ok:
            result.failback = self._failback(drill_id)
            fb = result.failback
            result.checks.append(self._check("failback completed", fb["outcome"] == "failed_back", fb["outcome"]))
            result.checks.append(self._check("failback lost 0 writes (planned RPO = 0)", fb["lost_writes"] == 0, fb["lost_writes"]))
        return result

    def _check(self, name: str, passed: bool, observed) -> dict:
        return {"check": name, "passed": bool(passed), "observed": observed}

    def _checks(self, r: DrillResult, sc: Scenario, wf, m: RecoveryMetrics) -> None:
        control = self.c.control_view()
        active = control.get(r.target, {}).get("active_region")
        if sc.expect_failover:
            r.checks.append(self._check("failover workflow ran", wf is not None, wf.outcome if wf else None))
            r.checks.append(self._check("failover verified (reads + writes in target)", bool(wf and wf.ok), wf.outcome if wf else None))
            r.checks.append(self._check("target region holds write authority", active == r.target, active))
            r.checks.append(self._check("writes resumed in target", m.write_rto_s is not None, m.write_rto_s))
        else:
            r.checks.append(self._check("no failover happened", r.events["t3_promoted"] is None, r.events["t3_promoted"]))
            r.checks.append(self._check("home region still active", active == self.c.home, active))
            if sc.pre_fault:
                r.checks.append(self._check("operator alerted", r.events["alert"] is not None, r.events["alert"]))

    def _failback(self, drill_id: str) -> dict:
        s = self.c.settings
        state = read_control(self.c.stores[self.c.standby])[0]
        wait = s.workflow.cooldown_s - (time.time() - state.changed_at)
        if wait > 0:
            time.sleep(wait + 0.2)
        fb_drill = f"{drill_id}-fb"
        traffic = _Traffic(self.c, fb_drill)
        traffic.start()
        time.sleep(1.0)
        t0 = time.time()
        wf = self.c.failback("drill-runner")
        time.sleep(s.drill.settle_s)
        traffic.stop()
        time.sleep(s.replication.lag_s + s.replication.jitter_s + 0.5)
        home_present = self._seq_present(self.c.home, fb_drill)
        acked = {w.seq for w in traffic.writes if w.ok}
        freeze = self.c.timeline.first("write_freeze", since=t0)
        promoted = self.c.timeline.first("promoted", since=t0, region=self.c.home)
        return {
            "outcome": wf.outcome,
            "duration_s": round(sum(st["duration_s"] for st in wf.steps), 3),
            "write_freeze_s": round(promoted["t"] - freeze["t"], 3) if freeze and promoted else None,
            "steps": wf.steps,
            "writes_acked": len(acked),
            "lost_writes": len(acked - home_present),
        }
