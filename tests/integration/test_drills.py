"""End-to-end drills on the SIMULATED two-region system (offline, $0)."""

import json

import pytest

from drsystem.drills.report import drill_markdown, write_reports
from drsystem.drills.runner import DrillRunner
from drsystem.sim.cluster import LocalCluster

pytestmark = pytest.mark.integration


@pytest.fixture
def cluster(settings, tmp_path):
    c = LocalCluster(settings, workdir=tmp_path / "sim", evidence_dir=tmp_path / "evidence", auto_ports=True, serve_web=False)
    c.start()
    yield c
    c.stop()


def test_hard_outage_failover_and_controlled_failback(cluster, tmp_path):
    r = DrillRunner(cluster).run("hard-outage", failback=True)
    assert r.passed, r.checks
    m = r.metrics
    assert m["write_rto_s"] is not None and m["detection_s"] is not None
    assert r.failback["outcome"] == "failed_back" and r.failback["lost_writes"] == 0
    assert cluster.control_view()[cluster.home]["active_region"] == cluster.home
    # Evidence labelling
    assert r.label == "SIMULATED"
    md = drill_markdown(r)
    assert "SIMULATED RESULT, NOT AWS EVIDENCE" in md
    summary = write_reports(tmp_path / "reports", [r], "SIMULATED")
    assert "SIMULATED" in summary.read_text(encoding="utf-8")
    assert json.loads(next((tmp_path / "reports").glob("*.json")).read_text())["label"] == "SIMULATED"
    events = [json.loads(line) for line in (tmp_path / "evidence" / "events.jsonl").read_text().splitlines()]
    assert events and all(e["label"] == "SIMULATED" for e in events)


def test_gray_failure_triggers_failover(cluster):
    r = DrillRunner(cluster).run("gray-errors")
    assert r.passed, r.checks


def test_storage_failure_triggers_failover(cluster):
    r = DrillRunner(cluster).run("storage-down")
    assert r.passed, r.checks


def test_health_blip_does_not_fail_over(cluster):
    r = DrillRunner(cluster).run("health-blip")
    assert r.passed, r.checks
    assert r.failover is None


def test_unhealthy_standby_blocks_failover_and_alerts(cluster):
    r = DrillRunner(cluster).run("standby-unhealthy")
    assert r.passed, r.checks
    assert any("NOT ready" in n["message"] for n in cluster.notifier.sent)


def test_failback_requires_approval(cluster):
    res = cluster.failback(approved_by="")
    assert res.outcome == "not_approved"
    res = cluster.failback(approved_by="mahesh")
    assert res.outcome.startswith("precondition_failed")  # home is already active


def test_false_alarm_is_retried_not_dismissed(cluster, monkeypatch):
    """Force the first reassessment to wrongly report 'healthy' during a real gray failure.

    The controller must not treat that false alarm as a completed action (no cool-down), so the
    persisting failure re-triggers and the region still fails over within the drill window.
    """
    from drsystem.orchestrator.workflows import FailoverWorkflow

    original = FailoverWorkflow._reassess
    calls = {"n": 0}

    def flaky_reassess(self, source, target):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"source_failures": 0, "standby_ready": True, "confirmed": False, "forced": True}
        return original(self, source, target)

    monkeypatch.setattr(FailoverWorkflow, "_reassess", flaky_reassess)
    r = DrillRunner(cluster).run("gray-errors")
    outcomes = [w.outcome for w in cluster.workflows]
    assert outcomes[0] == "false_alarm" and "failed_over" in outcomes, outcomes
    assert cluster.timeline.first("false_alarm") is not None
    assert cluster.control_view()[cluster.standby]["active_region"] == cluster.standby
    assert r.metrics["write_rto_s"] is not None  # writes resumed in the DR region


@pytest.mark.parametrize("scenario", ["gray-errors", "health-blip"])
def test_multisample_probing_end_to_end(settings, tmp_path, scenario):
    """AWS-style probing (5 samples per tick, window of 10) on the simulated system."""
    from dataclasses import replace

    s = replace(
        settings,
        probe=replace(settings.probe, samples_per_tick=5, sample_gap_s=0.05),
        scorer=replace(settings.scorer, window=10),
    )
    c = LocalCluster(s, workdir=tmp_path / "sim", evidence_dir=tmp_path / "ev", auto_ports=True, serve_web=False)
    with c:
        r = DrillRunner(c).run(scenario)
    assert r.passed, r.checks
