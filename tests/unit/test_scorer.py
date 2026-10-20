from drsystem.config import ScorerConfig
from drsystem.orchestrator.probe import ProbeResult
from drsystem.orchestrator.scorer import HealthScorer, ScorerState

from ..conftest import DR, HOME

CFG = ScorerConfig(window=6, failover_threshold=40, consecutive_bad=3, fast_path_hard_failures=3)


def ok(region, latency=50.0):
    return ProbeResult(region, 0.0, True, 200, latency, False, ready=True, active=region == HOME, epoch=1)


def err(region):
    return ProbeResult(region, 0.0, False, 500, 20.0, False, error="500")


def down(region):
    return ProbeResult(region, 0.0, False, None, 400.0, True, error="ReadTimeout")


def run(scorer, state, seq, standby=None, home=HOME, last_change=0.0, now=1000.0):
    d = None
    for p in seq:
        d = scorer.evaluate(state, p, standby or ok(DR), home, last_change, now)
    return d


def test_healthy_scores_high():
    s = HealthScorer(CFG, cooldown_s=10)
    d = run(s, ScorerState(), [ok(HOME)] * 6)
    assert d.action == "NONE" and d.score >= 95


def test_fast_path_after_consecutive_unreachable():
    s = HealthScorer(CFG, cooldown_s=10)
    st = ScorerState()
    assert run(s, st, [ok(HOME)] * 6 + [down(HOME)] * 2).action == "NONE"
    d = run(s, st, [down(HOME)])
    assert d.action == "FAILOVER" and d.fast_path


def test_sustained_errors_trigger_after_hysteresis():
    s = HealthScorer(CFG, cooldown_s=10)
    st = ScorerState()
    actions = [run(s, st, [p]).action for p in [ok(HOME)] * 6 + [err(HOME), ok(HOME)] * 2 + [err(HOME)] * 4]
    assert "FAILOVER" in actions
    assert actions.index("FAILOVER") >= 6 + 4 + 2  # needs 3 consecutive bad evaluations


def test_short_blip_does_not_fail_over():
    s = HealthScorer(CFG, cooldown_s=10)
    st = ScorerState()
    seq = [ok(HOME)] * 6 + [err(HOME)] * 2 + [ok(HOME)] * 6
    assert all(run(s, st, [p]).action == "NONE" for p in seq)


def test_refuses_when_standby_not_ready():
    s = HealthScorer(CFG, cooldown_s=10)
    d = run(s, ScorerState(), [down(HOME)] * 3, standby=down(DR))
    assert d.action == "ALERT" and "NOT ready" in d.reason


def test_no_automatic_action_away_from_non_home_region():
    s = HealthScorer(CFG, cooldown_s=10)
    d = run(s, ScorerState(), [down(DR)] * 3, standby=ok(HOME))
    assert d.action == "ALERT" and "human decision" in d.reason


def test_cooldown_blocks_repeat_action():
    s = HealthScorer(CFG, cooldown_s=10)
    d = run(s, ScorerState(), [down(HOME)] * 3, last_change=995.0, now=1000.0)
    assert d.action == "ALERT" and "cool-down" in d.reason


def test_latency_lowers_score():
    s = HealthScorer(CFG, cooldown_s=10)
    fast = s.score([ok(HOME, 50)] * 6)
    slow = s.score([ok(HOME, 1900)] * 6)
    assert fast > slow
