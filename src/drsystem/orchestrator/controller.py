"""DR controller: one ``tick()`` = probe both regions, score, decide, maybe launch failover.

State is persisted in the DR region's local store (``local/controller-state.json``), so a tick
can run in a fresh process each time (EventBridge Scheduler -> Lambda on AWS).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from ..config import Settings
from ..control import read_control
from ..events import EventLog
from ..notify import Notifier
from ..storage.base import NotFound, ObjectStore, get_json, put_json
from .probe import HttpProber
from .scorer import Decision, HealthScorer, ScorerState

STATE_KEY = "local/controller-state.json"


class FailoverLauncher(Protocol):
    def __call__(self, decision: Decision) -> object: ...


class Controller:
    def __init__(
        self,
        settings: Settings,
        control_store: ObjectStore,
        prober: HttpProber,
        launch_failover: FailoverLauncher,
        timeline: EventLog,
        notifier: Notifier,
        is_up: Callable[[], bool] = lambda: True,
    ) -> None:
        self.s = settings
        self.store = control_store
        self.prober = prober
        self.launch_failover = launch_failover
        self.timeline = timeline
        self.notifier = notifier
        self.is_up = is_up
        self.scorer = HealthScorer(settings.scorer, settings.workflow.cooldown_s)
        self.last_decision: Decision | None = None

    def _load(self) -> ScorerState:
        try:
            data, _ = get_json(self.store, STATE_KEY)
            return ScorerState.from_dict(data)
        except NotFound:
            return ScorerState()

    def _save(self, state: ScorerState) -> None:
        put_json(self.store, STATE_KEY, state.to_dict())

    def _sample(self, region: str) -> list:
        n = self.s.probe.samples_per_tick
        if n > 1 and hasattr(self.prober, "probe_many"):
            return self.prober.probe_many(region, n, self.s.probe.sample_gap_s)
        return [self.prober.probe(region)]

    def tick(self, now: float | None = None) -> Decision | None:
        if not self.is_up():
            return None  # the control plane's own region is down
        now = time.time() if now is None else now
        state = self._load()
        control, _ = read_control(self.store)
        active = control.active_region
        standby = self.s.peer_of(active)
        prev_ok = (state.history.get(active) or [{"ok": True}])[-1]["ok"]
        pa_list = self._sample(active)
        ps_list = self._sample(standby)
        failed = next((p for p in pa_list if not p.ok), None)
        if prev_ok and failed is not None:
            self.timeline.record(
                "first_failed_probe", region=active, error=failed.error, status=failed.status, hard=failed.hard_failure,
                samples=len(pa_list), failed_samples=sum(not p.ok for p in pa_list),
            )
        if not prev_ok and all(p.ok for p in pa_list):
            self.timeline.record("probe_recovered", region=active)
        last_change = 0.0 if control.changed_by == "bootstrap" else control.changed_at
        decision = self.scorer.evaluate(state, pa_list, ps_list, self.s.home_region, last_change, now)
        self.last_decision = decision
        self.timeline.record("score", region=active, score=decision.score, action=decision.action)

        if decision.action == "FAILOVER":
            self.timeline.record("decision", **decision.to_dict())
            previous_action_at = state.last_action_at
            state.last_action_at = now
            state.consecutive_bad = state.consecutive_hard = 0
            self._save(state)
            result = self.launch_failover(decision)
            if getattr(result, "outcome", None) in {"false_alarm", "lock_held"}:
                # Nothing changed, so no cool-down: if the failure persists, the next bad streak re-triggers.
                state.last_action_at = previous_action_at
                self._save(state)
            return decision
        if decision.action == "ALERT":
            # De-duplicate on a stable key: the reason text contains counters and changes every tick.
            key = f"{decision.code}:{decision.monitored}"
            if key != state.last_alert or now - state.last_alert_at >= self.s.workflow.alert_repeat_s:
                repeat = key == state.last_alert
                state.last_alert, state.last_alert_at = key, now
                self.timeline.record("alert", repeat=repeat, **decision.to_dict())
                self.notifier.notify(
                    "DR alert (no automatic action)" + (" [still active]" if repeat else ""), decision.reason, "WARNING"
                )
        self._save(state)
        return decision
