"""Recovery workflows. Each step is recorded on the timeline; on AWS each class maps 1:1 to a
Step Functions state machine whose tasks call the same functions.

FailoverWorkflow (automatic):  lock -> reassess -> drain -> promote -> notify -> verify -> release
FailbackWorkflow (human-only):  lock -> preconditions -> freeze -> drain both ways -> verify data
                                -> promote home -> verify traffic -> (rollback on failure) -> release
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..client import DRClient
from ..config import Settings
from ..control import (
    FencingError,
    acquire_lock,
    promote,
    read_control,
    release_lock,
    set_write_freeze,
)
from ..events import EventLog
from ..notify import Notifier
from ..storage.base import ObjectStore
from .probe import HttpProber
from .scorer import Decision

VERIFY_PREFIXES = ("notes/", "seq/", "idem/")


class ReplicationMonitor(Protocol):
    def stats(self, source: str): ...  # -> object with .pending and .oldest_pending_age_s

    def pending_keys(self, source: str) -> list[str]: ...


@dataclass
class WorkflowResult:
    name: str
    run_id: str
    ok: bool
    outcome: str
    steps: list[dict] = field(default_factory=list)
    details: dict = field(default_factory=dict)


class _Base:
    name = "workflow"

    def __init__(
        self,
        settings: Settings,
        stores: dict[str, ObjectStore],
        control_store: ObjectStore,
        prober: HttpProber,
        replication: ReplicationMonitor,
        client_factory: Callable[[], DRClient],
        notifier: Notifier,
        timeline: EventLog,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.s = settings
        self.stores = stores
        self.control_store = control_store  # region-local store of the DR control plane (holds the lock)
        self.prober = prober
        self.replication = replication
        self.client_factory = client_factory
        self.notifier = notifier
        self.timeline = timeline
        self.sleep = sleep
        self.run_id = f"{self.name}-{uuid.uuid4().hex[:8]}"
        self.result = WorkflowResult(self.name, self.run_id, False, "running")

    def _step(self, step: str, fn: Callable[[], dict | None]) -> dict:
        t0 = time.time()
        self.timeline.record("wf_step_start", workflow=self.name, wf_run=self.run_id, step=step)
        details = fn() or {}
        dur = time.time() - t0
        rec = {"step": step, "duration_s": round(dur, 3), **details}
        self.result.steps.append(rec)
        self.timeline.record("wf_step_end", workflow=self.name, wf_run=self.run_id, **rec)
        return details

    def _wait_drained(self, source: str, max_s: float) -> dict:
        deadline = time.time() + max_s
        while True:
            st = self.replication.stats(source)
            if st.pending <= self.s.workflow.replication_ok_pending or time.time() >= deadline:
                return {
                    "source": source,
                    "pending": st.pending,
                    "oldest_pending_age_s": round(st.oldest_pending_age_s, 3),
                    "drained": st.pending <= self.s.workflow.replication_ok_pending,
                    "pending_keys_sample": self.replication.pending_keys(source)[:20],
                }
            self.sleep(0.05)

    def _verify_traffic(self, target: str, timeout_s: float) -> dict:
        client = self.client_factory()
        deadline = time.time() + timeout_s
        reads_at = writes_at = None
        try:
            while time.time() < deadline and (reads_at is None or writes_at is None):
                if reads_at is None:
                    try:
                        r = client.get("whoami")
                        if r.status_code == 200 and r.json().get("region") == target:
                            reads_at = time.time()
                            self.timeline.record("serving_target", region=target, wf_run=self.run_id)
                    except Exception:  # noqa: BLE001 - verification keeps polling through failures
                        pass
                if writes_at is None:
                    w = client.write("drill/seq", {"drill_id": f"verify-{self.run_id}", "seq": 0}, deadline_s=1.0)
                    if w.ok and w.region == target:
                        writes_at = w.acked_at
                        self.timeline.record("first_write_target", region=target, wf_run=self.run_id)
                if reads_at is None or writes_at is None:
                    self.sleep(0.1)
        finally:
            client.close()
        return {"reads_ok": reads_at is not None, "writes_ok": writes_at is not None, "target": target}

    def _finish(self, ok: bool, outcome: str) -> WorkflowResult:
        self.result.ok = ok
        self.result.outcome = outcome
        self.timeline.record("workflow_complete", workflow=self.name, wf_run=self.run_id, ok=ok, outcome=outcome)
        return self.result


class FailoverWorkflow(_Base):
    name = "failover"

    def run(self, decision: Decision) -> WorkflowResult:
        source, target = decision.monitored, decision.target
        self.result.details.update(decision=decision.to_dict())
        if not acquire_lock(self.control_store, self.run_id, self.s.workflow.lock_ttl_s):
            return self._finish(False, "lock_held")
        try:
            confirmed = self._step("reassess", lambda: self._reassess(source, target))
            if not confirmed["confirmed"]:
                self.timeline.record("false_alarm", region=source, wf_run=self.run_id)
                return self._finish(False, "false_alarm")
            drain = self._step("drain", lambda: self._wait_drained(source, self.s.workflow.drain_max_s))
            self.result.details["unreplicated_at_promotion"] = drain["pending"]
            promoted = self._step("promote", lambda: self._promote(target, decision))
            if not promoted["promoted"]:
                return self._finish(False, "fencing_conflict")
            self._step("notify", lambda: self._notify(source, target, decision, drain))
            verified = self._step("verify", lambda: self._verify_traffic(target, self.s.workflow.verify_timeout_s))
            if not (verified["reads_ok"] and verified["writes_ok"]):
                self.notifier.notify("CRITICAL: failover verification failed", str(verified), "CRITICAL")
                return self._finish(False, "verification_failed")
            return self._finish(True, "failed_over")
        finally:
            release_lock(self.control_store, self.run_id)

    REASSESS_PROBES = 3

    def _reassess(self, source: str, target: str) -> dict:
        # Purpose: rule out a failure that has already fully cleared. Any failed re-probe confirms it.
        # With 70% injected errors, 2 probes both succeeding happened ~9% of the time; 3 probes -> ~2.7%,
        # and a false alarm no longer arms the cool-down, so the controller simply re-triggers.
        checks = []
        for _ in range(self.REASSESS_PROBES):
            checks.append(self.prober.probe(source).ok)
            self.sleep(self.s.probe.interval_s / 2)
        standby_ok = self.prober.probe(target).ok
        return {"source_failures": checks.count(False), "standby_ready": standby_ok, "confirmed": (False in checks) and standby_ok}

    def _promote(self, target: str, decision: Decision) -> dict:
        store = self.stores[target]
        state, _ = read_control(store)
        if state.active_region == target:
            return {"promoted": True, "epoch": state.epoch, "note": "already active"}
        try:
            new = promote(store, target, state.epoch, by=self.run_id, reason=decision.reason, demote_previous=True)
        except FencingError as exc:
            self.timeline.record("fencing_conflict", detail=str(exc), wf_run=self.run_id)
            return {"promoted": False, "error": str(exc)}
        self.timeline.record("promoted", region=target, epoch=new.epoch, wf_run=self.run_id)
        return {"promoted": True, "epoch": new.epoch, "demoted": new.demoted_region}

    def _notify(self, source: str, target: str, decision: Decision, drain: dict) -> dict:
        msg = (
            f"Failed over {source} -> {target}. Trigger: {decision.reason}. "
            f"Unreplicated objects at promotion: {drain['pending']}. Label: {self.timeline.label}."
        )
        self.notifier.notify(f"DR failover to {target}", msg, "WARNING")
        return {"notified": True}


class FailbackWorkflow(_Base):
    name = "failback"

    def run(self, approved_by: str) -> WorkflowResult:
        home = self.s.home_region
        self.result.details["approved_by"] = approved_by
        if not approved_by:
            return self._finish(False, "not_approved")
        if not acquire_lock(self.control_store, self.run_id, self.s.workflow.lock_ttl_s):
            return self._finish(False, "lock_held")
        froze = False
        try:
            pre = self._step("preconditions", lambda: self._preconditions(home))
            if not pre["ok"]:
                return self._finish(False, f"precondition_failed: {pre['reason']}")
            current = self._current = pre["active"]
            self._step("freeze_writes", lambda: self._freeze(current, True))
            froze = True
            self._step("drain_to_home", lambda: self._wait_drained(current, self.s.workflow.verify_timeout_s))
            data = self._step("verify_data", lambda: self._verify_data(current, home))
            if not data["consistent"]:
                return self._finish(False, "data_mismatch")
            sync = self._step("await_control_sync", lambda: self._await_control_sync(current, home))
            if not sync["synced"]:
                return self._finish(False, "control_not_synced")
            self._step("promote_home", lambda: self._promote_home(home, sync["epoch"]))
            froze = False  # promotion clears the freeze
            verified = self._step("verify_traffic", lambda: self._verify_traffic(home, self.s.workflow.verify_timeout_s))
            if not (verified["reads_ok"] and verified["writes_ok"]):
                self._step("rollback", lambda: self._rollback(current, home))
                self.notifier.notify("CRITICAL: failback rolled back", str(verified), "CRITICAL")
                return self._finish(False, "rolled_back")
            self.notifier.notify(f"DR failback to {home} complete", f"approved by {approved_by}", "INFO")
            return self._finish(True, "failed_back")
        finally:
            if froze:
                try:
                    self._freeze(self._current, False)
                except Exception:  # noqa: BLE001 - best effort unfreeze, alert either way
                    self.notifier.notify("CRITICAL: could not unfreeze writes", self.run_id, "CRITICAL")
            release_lock(self.control_store, self.run_id)

    def _preconditions(self, home: str) -> dict:
        state, _ = read_control(self.control_store)
        if state.active_region == home:
            return {"ok": False, "reason": "home region already active", "active": state.active_region}
        since = time.time() - state.changed_at
        if since < self.s.workflow.cooldown_s:
            return {"ok": False, "reason": f"cool-down ({since:.1f}s)", "active": state.active_region}
        t_end = time.time() + self.s.workflow.failback_min_healthy_s
        probes = 0
        while time.time() < t_end:
            p = self.prober.probe(home)
            probes += 1
            if not p.ok:
                return {"ok": False, "reason": f"home unhealthy: {p.error or p.status}", "active": state.active_region}
            self.sleep(self.s.probe.interval_s)
        return {"ok": True, "active": state.active_region, "healthy_probes": probes}

    def _freeze(self, region: str, frozen: bool) -> dict:
        store = self.stores[region]
        state, _ = read_control(store)
        new = set_write_freeze(store, state.epoch, frozen, by=self.run_id, reason="controlled failback")
        self.timeline.record("write_freeze" if frozen else "write_unfreeze", region=region, epoch=new.epoch, wf_run=self.run_id)
        return {"frozen": frozen, "epoch": new.epoch}

    def _verify_data(self, source: str, home: str) -> dict:
        def snapshot(store: ObjectStore) -> dict[str, str]:
            out = {}
            for prefix in VERIFY_PREFIXES:
                for k in store.list(prefix):
                    out[k] = store.get(k).etag
            return out

        deadline = time.time() + self.s.workflow.verify_timeout_s
        while True:
            a, b = snapshot(self.stores[source]), snapshot(self.stores[home])
            missing = sorted(k for k in a if a[k] != b.get(k))
            if not missing or time.time() >= deadline:
                return {"consistent": not missing, "objects": len(a), "mismatched": missing[:20]}
            self.sleep(0.1)

    def _await_control_sync(self, source: str, home: str) -> dict:
        deadline = time.time() + self.s.workflow.verify_timeout_s
        src, _ = read_control(self.stores[source])
        while time.time() < deadline:
            dst, _ = read_control(self.stores[home])
            if dst.epoch == src.epoch:
                return {"synced": True, "epoch": dst.epoch}
            self.sleep(0.05)
        return {"synced": False, "epoch": src.epoch}

    def _promote_home(self, home: str, epoch: int) -> dict:
        new = promote(self.stores[home], home, epoch, by=self.run_id, reason="approved failback", demote_previous=False)
        self.timeline.record("promoted", region=home, epoch=new.epoch, wf_run=self.run_id)
        return {"epoch": new.epoch}

    def _rollback(self, previous: str, home: str) -> dict:
        sync = self._await_control_sync(home, previous)
        new = promote(self.stores[previous], previous, sync["epoch"], by=self.run_id, reason="failback rollback", demote_previous=True)
        self.timeline.record("rollback", region=previous, epoch=new.epoch, wf_run=self.run_id)
        return {"epoch": new.epoch}
