"""Multi-sample probing (AWS profile: 5 samples per region per tick, window = 10 samples)."""

from drsystem.config import ScorerConfig
from drsystem.orchestrator.probe import HttpProber, ProbeResult
from drsystem.orchestrator.scorer import HealthScorer, ScorerState

from ..conftest import DR, HOME

CFG = ScorerConfig(window=10, failover_threshold=40, consecutive_bad=3, fast_path_hard_failures=2,
                   latency_good_ms=800, latency_bad_ms=3000)


def ok(r):
    return ProbeResult(r, 0.0, True, 200, 150.0, False, ready=True)


def err(r):
    return ProbeResult(r, 0.0, False, 500, 40.0, False, error="INJECTED_ERROR")


def down(r):
    return ProbeResult(r, 0.0, False, None, 3000.0, True, error="ReadTimeout")


def tick(scorer, state, samples, now):
    return scorer.evaluate(state, samples, [ok(DR)] * 5, HOME, 0.0, now)


def warm(scorer, state):
    for i in range(3):
        assert tick(scorer, state, [ok(HOME)] * 5, 1000 + i * 60).action == "NONE"


def test_gray_failure_fails_over_within_three_ticks():
    s, st = HealthScorer(CFG, cooldown_s=300), ScorerState()
    warm(s, st)
    pattern = [err(HOME), err(HOME), ok(HOME), err(HOME), err(HOME)]  # ~80% errors, one success per tick
    actions = [tick(s, st, pattern, 2000 + i * 60).action for i in range(4)]
    assert "FAILOVER" in actions[:3], actions


def test_one_lucky_success_does_not_reset_the_evidence():
    s, st = HealthScorer(CFG, cooldown_s=300), ScorerState()
    warm(s, st)
    tick(s, st, [err(HOME)] * 5, 2000)
    lucky = [ok(HOME), err(HOME), err(HOME), err(HOME), err(HOME)]  # starts with a success
    d = tick(s, st, lucky, 2060)
    assert st.consecutive_bad == 2 and d.action == "NONE"
    assert tick(s, st, lucky, 2120).action == "FAILOVER"


def test_two_tick_blip_does_not_fail_over():
    s, st = HealthScorer(CFG, cooldown_s=300), ScorerState()
    warm(s, st)
    blip = [ProbeResult(HOME, 0.0, False, 503, 30.0, False, error="503")] * 5
    actions = [tick(s, st, blip, 2000).action, tick(s, st, blip, 2060).action]
    actions += [tick(s, st, [ok(HOME)] * 5, 2120 + i * 60).action for i in range(3)]
    assert "FAILOVER" not in actions, actions


def test_unreachable_region_uses_fast_path():
    s, st = HealthScorer(CFG, cooldown_s=300), ScorerState()
    warm(s, st)
    assert tick(s, st, [down(HOME), down(HOME)], 2000).action == "NONE"
    d = tick(s, st, [down(HOME), down(HOME)], 2060)
    assert d.action == "FAILOVER" and d.fast_path


def test_standby_must_pass_a_majority_of_samples():
    s, st = HealthScorer(CFG, cooldown_s=300), ScorerState()
    warm(s, st)
    flaky_standby = [ok(DR), err(DR), err(DR), err(DR), ok(DR)]
    d = None
    for i in range(2):
        d = s.evaluate(st, [down(HOME)] * 2, flaky_standby, HOME, 0.0, 2000 + i * 60)
    assert d.action == "ALERT" and d.code == "STANDBY_NOT_READY"


def test_probe_many_stops_early_when_unreachable():
    class P(HttpProber):
        def __init__(self):
            self.calls = 0

        def probe(self, region):
            self.calls += 1
            return down(region)

    p = P()
    out = p.probe_many(HOME, 5, gap_s=0, sleep=lambda s: None)
    assert len(out) == 2 and p.calls == 2
