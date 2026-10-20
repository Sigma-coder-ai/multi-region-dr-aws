"""Command line entry point: ``python -m drsystem <command>``.

  sim        run the SIMULATED two-region system with the DR Live Monitor dashboard
  drill      run one scenario (or ``all``) N times on fresh simulated clusters, write reports
  optimize   model the cheapest AWS configuration meeting RTO/RPO targets (MODEL ESTIMATE)
  preflight  READ-ONLY zero-spend checks of your AWS account (plan, credits, MFA, regions)
  scenarios  list drill scenarios
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path

from .config import PROJECT_ROOT, load_settings


def _cmd_sim(args) -> int:
    from .sim.cluster import LocalCluster, free_port

    settings = load_settings(args.config)
    if os.environ.get("PORT"):  # hosted (Railway etc.): only the edge is public, on $PORT
        port = int(os.environ["PORT"])
        regions = tuple(replace(r, port=free_port()) if r.port == port else r for r in settings.regions)
        settings = replace(settings, regions=regions, router=replace(settings.router, host="0.0.0.0", port=port))
    cluster = LocalCluster(settings).start()
    print("=" * 72)
    print(" SIMULATION MODE: local processes only. No AWS resources, $0.")
    print(f" DR Live Monitor : {cluster.edge_url}/")
    for r in cluster.settings.regions:
        print(f" {r.label:<16}: {r.endpoint}  ({r.name})")
    print(f" Evidence        : {cluster.evidence_dir}")
    print(" Ctrl+C to stop.")
    print("=" * 72)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        cluster.stop()
    return 0


def _cmd_drill(args) -> int:
    from .drills.report import write_reports
    from .drills.runner import SCENARIOS, DrillRunner
    from .sim.cluster import LocalCluster

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    for n in names:
        if n not in SCENARIOS:
            print(f"unknown scenario {n!r}; choose from: {', '.join(SCENARIOS)} or all", file=sys.stderr)
            return 2
    settings = load_settings(args.config)
    suite_id = time.strftime("suite-%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else PROJECT_ROOT / "evidence" / "simulated" / suite_id
    results = []
    for name in names:
        for i in range(args.repeat):
            print(f"[SIMULATED] drill {name} run {i + 1}/{args.repeat} ...", flush=True)
            with LocalCluster(settings, evidence_dir=out_dir / "runs" / f"{name}-{i + 1}", auto_ports=True, serve_web=False) as c:
                r = DrillRunner(c).run(name, failback=args.failback)
            results.append(r)
            m = r.metrics
            print(
                f"    {'PASS' if r.passed else 'FAIL'}  detect={m['detection_s']}s  write_rto={m['write_rto_s']}s  "
                f"lost_writes={m['lost_writes']}  rpo={m['rpo_s']}s"
                + (f"  failback={r.failback['outcome']}" if r.failback else ""),
                flush=True,
            )
    summary = write_reports(out_dir, results, "SIMULATED")
    print(f"\n[SIMULATED] {sum(r.passed for r in results)}/{len(results)} drills passed. Summary: {summary}")
    return 0 if all(r.passed for r in results) else 1


def _load_real_overheads(path: Path):
    from .drills.optimizer import Overheads

    files = sorted(path.rglob("*.json"))
    drills = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    drills = [d for d in drills if "metrics" in d and "label" in d]
    if not drills:
        raise SystemExit(f"no drill results found under {path}")
    if any(d["label"] != "REAL-AWS" for d in drills):
        raise SystemExit("refusing to calibrate the AWS model with SIMULATED evidence (timings are scaled)")

    def step_median(step: str, default: float) -> float:
        vals = [
            st["duration_s"]
            for d in drills
            if d.get("failover")
            for st in d["failover"]["steps"]
            if st["step"] == step
        ]
        return statistics.median(vals) if vals else default

    base = Overheads()
    return Overheads(
        reassess_s=step_median("reassess", base.reassess_s),
        promote_s=step_median("promote", base.promote_s),
        source="REAL-AWS",
    )


def _cmd_optimize(args) -> int:
    from .drills.optimizer import Overheads, optimize, optimizer_markdown

    ov = _load_real_overheads(Path(args.calibrate)) if args.calibrate else Overheads()
    results = optimize(args.target_rto, args.target_rpo, args.replication_lag, ov)
    md = optimizer_markdown(results, args.target_rto, args.target_rpo, args.replication_lag, ov)
    out = Path(args.out) if args.out else PROJECT_ROOT / "evidence" / "model" / "optimizer.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    best = next((e for e in results if e.meets_targets), None)
    print("[MODEL ESTIMATE] not a measurement")
    if best:
        print(f"  best: {json.dumps(best.to_dict())}")
    print(f"  report: {out}")
    return 0


def _cmd_preflight(args) -> int:
    from .aws.preflight import run_preflight

    report = run_preflight(args.profile, min_credits_usd=args.min_credits, min_days_left=args.min_days)
    print(report.text())
    return 0 if report.ok else 1


def _cmd_scenarios(_args) -> int:
    from .drills.runner import SCENARIOS

    for s in SCENARIOS.values():
        print(f"{s.name:<14} expect_failover={str(s.expect_failover):<5}  {s.description}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="drsystem", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="settings profile (default: config/local-sim.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sim", help="run the simulated system + dashboard").set_defaults(fn=_cmd_sim)

    d = sub.add_parser("drill", help="run drills on fresh simulated clusters")
    d.add_argument("scenario", help="scenario name or 'all'")
    d.add_argument("--repeat", type=int, default=1)
    d.add_argument("--failback", action="store_true", help="run a controlled failback after each failover")
    d.add_argument("--out", help="output directory for reports")
    d.set_defaults(fn=_cmd_drill)

    o = sub.add_parser("optimize", help="cheapest AWS config meeting targets (MODEL ESTIMATE)")
    o.add_argument("--target-rto", type=float, default=300)
    o.add_argument("--target-rpo", type=float, default=60)
    o.add_argument("--replication-lag", type=float, default=15.0, help="assumed S3 CRR lag in seconds")
    o.add_argument("--calibrate", help="directory of REAL-AWS drill JSON results")
    o.add_argument("--out")
    o.set_defaults(fn=_cmd_optimize)

    pf = sub.add_parser("preflight", help="read-only AWS account checks (no resources, no changes)")
    pf.add_argument("--profile", required=True, help="AWS CLI profile you signed in with (e.g. via `aws login`)")
    pf.add_argument("--min-credits", type=float, default=50.0)
    pf.add_argument("--min-days", type=int, default=30)
    pf.set_defaults(fn=_cmd_preflight)

    sub.add_parser("scenarios", help="list drill scenarios").set_defaults(fn=_cmd_scenarios)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
