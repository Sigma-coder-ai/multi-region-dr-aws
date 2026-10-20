"""Composite health score and failover decision with hysteresis and safety guards.

Score (0-100, higher is healthier) for the region currently holding write authority:
  65%  availability   share of successful probes in the window; >=95% -> full, <=50% -> zero
  20%  latency        p90 of successful probes; <= good -> full, >= bad -> zero (linear between)
  15%  readiness      latest probe reported ready

Decision rules
  - FAILOVER when score < threshold for ``consecutive_bad`` evaluations in a row (hysteresis),
    or immediately after ``fast_path_hard_failures`` consecutive unreachable probes.
  - Guards (raise ALERT instead): standby not ready; automatic action only away from the home
    region (failback is human-approved); cool-down after any control change.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field

from ..config import ScorerConfig
from .probe import ProbeResult

W_AVAIL, W_LATENCY, W_READY = 0.65, 0.20, 0.15


@dataclass
class ScorerState:
    history: dict[str, list[dict]] = field(default_factory=dict)
    consecutive_bad: int = 0
    consecutive_hard: int = 0
    last_action_at: float = 0.0
    last_score: float = 100.0
    last_alert: str = ""  # "<code>:<monitored region>" of the last alert sent
    last_alert_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ScorerState":
        return cls(**d)


@dataclass(frozen=True)
class Decision:
    action: str  # NONE | FAILOVER | ALERT
    reason: str
    score: float
    monitored: str
    target: str
    fast_path: bool = False
    code: str = ""  # stable alert category: STANDBY_NOT_READY | NOT_HOME_REGION | COOLDOWN

    def to_dict(self) -> dict:
        return asdict(self)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


class HealthScorer:
    def __init__(self, cfg: ScorerConfig, cooldown_s: float) -> None:
        self.cfg = cfg
        self.cooldown_s = cooldown_s

    def score(self, history: list[ProbeResult]) -> float:
        if not history:
            return 100.0
        avail = sum(p.ok for p in history) / len(history)
        avail_score = _clamp((avail - 0.5) / 0.45)
        ok_lat = [p.latency_ms for p in history if p.ok]
        if ok_lat:
            p90 = statistics.quantiles(ok_lat, n=10)[-1] if len(ok_lat) > 1 else ok_lat[0]
            span = self.cfg.latency_bad_ms - self.cfg.latency_good_ms
            lat_score = _clamp(1 - (p90 - self.cfg.latency_good_ms) / span)
        else:
            lat_score = 0.0
        ready_score = 1.0 if history[-1].ready else 0.0
        return round(100 * (W_AVAIL * avail_score + W_LATENCY * lat_score + W_READY * ready_score), 1)

    def evaluate(
        self,
        state: ScorerState,
        active_probe: ProbeResult | list[ProbeResult],
        standby_probe: ProbeResult | list[ProbeResult],
        home_region: str,
        last_control_change_at: float,
        now: float,
    ) -> Decision:
        # One evaluation per tick. A tick may carry several samples per region (multi-sample probing):
        # all of them enter the sliding window, so one lucky success no longer resets the evidence.
        active_samples = active_probe if isinstance(active_probe, list) else [active_probe]
        standby_samples = standby_probe if isinstance(standby_probe, list) else [standby_probe]
        active, standby = active_samples[0].region, standby_samples[0].region
        for samples in (active_samples, standby_samples):
            hist = state.history.setdefault(samples[0].region, [])
            hist.extend(p.to_dict() for p in samples)
            del hist[: -self.cfg.window]
        history = [ProbeResult.from_dict(d) for d in state.history[active]]
        score = self.score(history)
        state.last_score = score
        bad = score < self.cfg.failover_threshold
        # With several samples per tick, a tick where every sample is healthy is strong evidence the
        # failure has cleared, even if older bad samples still sit in the window. (With one sample per
        # tick a single success proves little, so the score alone decides.)
        if len(active_samples) >= 3 and all(p.ok for p in active_samples):
            bad = False
        state.consecutive_bad = state.consecutive_bad + 1 if bad else 0
        tick_unreachable = all(p.hard_failure for p in active_samples)
        state.consecutive_hard = state.consecutive_hard + 1 if tick_unreachable else 0
        standby_ok = sum(p.ok for p in standby_samples) * 2 > len(standby_samples)  # majority of samples

        fast = state.consecutive_hard >= self.cfg.fast_path_hard_failures
        sustained = state.consecutive_bad >= self.cfg.consecutive_bad
        if not (fast or sustained):
            state.last_alert = ""
            why = f"score {score} (bad streak {state.consecutive_bad}/{self.cfg.consecutive_bad})"
            return Decision("NONE", why, score, active, standby)

        trigger = (
            f"{state.consecutive_hard} consecutive unreachable probes"
            if fast
            else f"score {score} < {self.cfg.failover_threshold} for {state.consecutive_bad} evaluations"
        )
        if not standby_ok:
            return Decision(
                "ALERT", f"{trigger}; standby {standby} NOT ready, refusing failover", score, active, standby, fast, "STANDBY_NOT_READY"
            )
        if active != home_region:
            return Decision(
                "ALERT", f"{trigger}; {active} is not the home region, human decision required", score, active, standby, fast,
                "NOT_HOME_REGION",
            )
        since_change = now - max(state.last_action_at, last_control_change_at)
        if since_change < self.cooldown_s:
            return Decision(
                "ALERT", f"{trigger}; in cool-down ({since_change:.1f}s < {self.cooldown_s}s)", score, active, standby, fast, "COOLDOWN"
            )
        return Decision("FAILOVER", trigger, score, active, standby, fast)
