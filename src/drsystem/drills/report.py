"""Markdown + JSON reports. Simulated evidence carries an unmissable banner."""

from __future__ import annotations

import json
import platform
import time
from pathlib import Path

from .metrics import summarize
from .runner import DrillResult

SIM_BANNER = (
    "> ⚠️ **SIMULATED RESULT, NOT AWS EVIDENCE.** Produced by the local simulation: two processes on one "
    "machine, with simulated replication and a simulated CloudFront router, using **scaled-down timings**. "
    "It validates the recovery *logic*. Only `REAL-AWS` drills count as RTO/RPO evidence."
)
AWS_BANNER = "> ✅ **REAL-AWS RESULT.** Measured against deployed AWS resources."

METRIC_ROWS = [
    ("detection_s", "Detection (fault → first failed probe)", "s"),
    ("decision_s", "Decision (first failed probe → failover decision)", "s"),
    ("promotion_s", "Promotion (decision → control object promoted)", "s"),
    ("read_rto_s", "Read RTO (fault → first read served by target)", "s"),
    ("read_outage_s", "Longest user-visible read outage", "s"),
    ("write_rto_s", "Write RTO (fault → first write acked by target)", "s"),
    ("lost_writes", "Acknowledged writes lost at promotion", ""),
    ("rpo_s", "RPO (data-loss window)", "s"),
    ("read_p50_ms", "User read latency p50 during incident", "ms"),
    ("read_p95_ms", "User read latency p95 during incident", "ms"),
]


def _fmt(v, unit: str) -> str:
    if v is None:
        return "—"
    return f"{v} {unit}".strip()


def banner(label: str) -> str:
    return SIM_BANNER if label == "SIMULATED" else AWS_BANNER


def drill_markdown(r: DrillResult) -> str:
    lines = [
        f"# Drill report: `{r.scenario}`, **{r.label}**",
        "",
        banner(r.label),
        "",
        f"- **Drill ID:** `{r.drill_id}` (run `{r.run_id}`)",
        f"- **Scenario:** {r.description}",
        f"- **Failed region:** `{r.source}`, **target:** `{r.target}`",
        f"- **Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')} on {platform.node() or 'local machine'}",
        f"- **Result:** {'✅ PASSED' if r.passed else '❌ FAILED'}",
        "",
        "## Checks",
        "",
        "| Check | Result | Observed |",
        "|---|---|---|",
    ]
    for c in r.checks:
        lines.append(f"| {c['check']} | {'✅' if c['passed'] else '❌'} | `{c['observed']}` |")
    lines += ["", "## Timeline (seconds after fault injection)", "", "| Event | t + s |", "|---|---|"]
    for k, v in r.events.items():
        lines.append(f"| {k} | {_fmt(v, 's')} |")
    lines += ["", "## Recovery metrics", "", "| Metric | Value |", "|---|---|"]
    for key, name, unit in METRIC_ROWS:
        lines.append(f"| {name} | {_fmt(r.metrics.get(key), unit)} |")
    lines.append(f"| Lost writes that arrived late after the region recovered | {r.late_arrivals} |")
    if r.retried_workflows:
        lines.append(f"| Workflows retried before the decisive one | {', '.join(r.retried_workflows)} |")
    if r.failover:
        lines += ["", "## Failover workflow steps", "", "| Step | Duration | Details |", "|---|---|---|"]
        for st in r.failover["steps"]:
            extra = {k: v for k, v in st.items() if k not in {"step", "duration_s"}}
            lines.append(f"| {st['step']} | {st['duration_s']} s | `{json.dumps(extra, default=str)[:160]}` |")
    if r.failback:
        fb = r.failback
        lines += [
            "",
            "## Controlled failback",
            "",
            f"- **Outcome:** `{fb['outcome']}`, total {fb['duration_s']} s, writes frozen for {_fmt(fb['write_freeze_s'], 's')}",
            f"- **Writes acknowledged during failback:** {fb['writes_acked']}, **lost:** {fb['lost_writes']}",
            "",
            "| Step | Duration |",
            "|---|---|",
        ]
        lines += [f"| {st['step']} | {st['duration_s']} s |" for st in fb["steps"]]
    lines += ["", "## Settings used", "", "```json", json.dumps(r.settings, indent=2), "```", ""]
    return "\n".join(lines)


def suite_markdown(label: str, results: list[DrillResult]) -> str:
    by_scenario: dict[str, list[DrillResult]] = {}
    for r in results:
        by_scenario.setdefault(r.scenario, []).append(r)
    lines = [f"# Drill suite summary, **{label}**", "", banner(label), ""]
    lines += [
        f"- **Drills run:** {len(results)}, **passed:** {sum(r.passed for r in results)}",
        f"- **Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Pass rate by scenario",
        "",
        "| Scenario | Runs | Passed |",
        "|---|---|---|",
    ]
    for name, rs in by_scenario.items():
        lines.append(f"| {name} | {len(rs)} | {sum(r.passed for r in rs)} |")
    for name, rs in by_scenario.items():
        if not any(r.failover for r in rs):
            continue
        lines += ["", f"## `{name}`: metrics across runs", "", "| Metric | n | median | p90 | min | max |", "|---|---|---|---|---|---|"]
        for key, title, unit in METRIC_ROWS:
            s = summarize([r.metrics.get(key) for r in rs])
            lines.append(f"| {title} | {s['n']} | {_fmt(s['median'], unit)} | {_fmt(s['p90'], unit)} | {_fmt(s['min'], unit)} | {_fmt(s['max'], unit)} |")
        fbs = [r.failback for r in rs if r.failback]
        if fbs:
            s = summarize([f["duration_s"] for f in fbs])
            lost = sum(f["lost_writes"] for f in fbs)
            lines.append(f"| Failback duration | {s['n']} | {_fmt(s['median'], 's')} | {_fmt(s['p90'], 's')} | {_fmt(s['min'], 's')} | {_fmt(s['max'], 's')} |")
            lines.append(f"| Writes lost across all failbacks | {len(fbs)} | {lost} | | | |")
    lines.append("")
    return "\n".join(lines)


def write_reports(out_dir: Path, results: list[DrillResult], label: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    for r in results:
        stem = f"{r.scenario}-{r.drill_id}"
        (out_dir / f"{stem}.json").write_text(json.dumps(r.to_dict(), indent=2, default=str), encoding="utf-8")
        (out_dir / f"{stem}.md").write_text(drill_markdown(r), encoding="utf-8")
    summary = out_dir / "SUMMARY.md"
    summary.write_text(suite_markdown(label, results), encoding="utf-8")
    return summary
