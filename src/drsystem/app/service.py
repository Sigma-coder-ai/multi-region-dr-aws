"""Notes & File Vault service logic for one region, including write fencing.

A write is accepted only if ALL of the following hold:
1. the local control object names this region as ``active_region``;
2. ``write_freeze`` is off (it is on during a controlled failback);
3. (peer fencing) the peer region, if reachable, does not report a *higher* epoch naming a
   different active region. This closes the split-brain window when a recovered region has not
   yet received the replicated control object. If the peer is unreachable, the local view wins
   (availability over consistency; documented limitation during a true network partition).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

from ..control import ControlState, read_control
from ..faults import FaultSource
from ..storage.base import NotFound, ObjectStore, PreconditionFailed, StoreUnavailable, get_json, put_json

MAX_BODY_BYTES = 64 * 1024  # keeps storage and replication costs negligible


class PeerControlView(Protocol):
    def peer_control(self) -> ControlState | None: ...


@dataclass
class WriteRejected(Exception):
    code: str  # READ_ONLY | FROZEN | STALE_EPOCH | UNAVAILABLE
    active_region: str | None
    epoch: int | None
    detail: str = ""

    def to_dict(self) -> dict:
        return {"error": self.code, "active_region": self.active_region, "epoch": self.epoch, "detail": self.detail}


class RegionService:
    def __init__(
        self,
        region: str,
        store: ObjectStore,
        faults: FaultSource,
        peer: PeerControlView | None = None,
        version: str = "0.1.0",
    ) -> None:
        self.region = region
        self.store = store
        self.faults = faults
        self.peer = peer
        self.version = version

    # -- control / health ---------------------------------------------------------------------
    def control(self) -> ControlState:
        state, _ = read_control(self.store)
        return state

    def is_demoted(self, state: ControlState | None = None) -> bool:
        state = state or self.control()
        return state.demoted_region == self.region and state.active_region != self.region

    def health(self) -> dict:
        fault = self.faults.current()
        checks: dict[str, str] = {}
        state: ControlState | None = None
        try:
            state = self.control()
            self.store.get(f"canary/{self.region}.json")
            checks["storage"] = "ok"
        except NotFound:
            checks["storage"] = "ok"  # store reachable; canary simply not written yet
        except StoreUnavailable as exc:
            checks["storage"] = f"unavailable: {exc}"
        checks["fault_flag"] = "health_fail" if fault.health_fail else "none"
        ready = checks["storage"] == "ok" and not fault.health_fail and state is not None
        active = bool(ready and state and state.active_region == self.region and not state.write_freeze)
        return {
            "region": self.region,
            "ready": ready,
            "active": active,
            "demoted": bool(state and self.is_demoted(state)),
            "epoch": state.epoch if state else None,
            "active_region": state.active_region if state else None,
            "write_freeze": state.write_freeze if state else None,
            "checks": checks,
            "version": self.version,
            "ts": time.time(),
        }

    # -- fencing --------------------------------------------------------------------------------
    def check_write_allowed(self) -> ControlState:
        try:
            state = self.control()
        except StoreUnavailable as exc:
            raise WriteRejected("UNAVAILABLE", None, None, str(exc)) from exc
        if state.active_region != self.region:
            raise WriteRejected("READ_ONLY", state.active_region, state.epoch)
        if state.write_freeze:
            raise WriteRejected("FROZEN", state.active_region, state.epoch, "controlled failback in progress")
        if self.peer is not None:
            peer_state = self.peer.peer_control()
            if peer_state and peer_state.epoch > state.epoch and peer_state.active_region != self.region:
                raise WriteRejected(
                    "STALE_EPOCH",
                    peer_state.active_region,
                    peer_state.epoch,
                    f"peer reports epoch {peer_state.epoch} > local {state.epoch}",
                )
        return state

    # -- notes ----------------------------------------------------------------------------------
    def create_note(self, title: str, body: str, idempotency_key: str) -> tuple[dict, bool]:
        """Returns (note, replayed). Replays return the original note for a repeated key."""
        if not idempotency_key:
            raise ValueError("Idempotency-Key header is required")
        if len(body.encode()) > MAX_BODY_BYTES:
            raise ValueError(f"body exceeds {MAX_BODY_BYTES} bytes")
        idem_key = "idem/" + hashlib.sha256(idempotency_key.encode()).hexdigest() + ".json"
        try:
            existing, _ = get_json(self.store, idem_key)
            return self.get_note(existing["note_id"]), True
        except NotFound:
            pass
        state = self.check_write_allowed()
        note_id = uuid.uuid4().hex[:16]
        note = {
            "id": note_id,
            "title": title,
            "body": body,
            "written_region": self.region,
            "written_epoch": state.epoch,
            "written_at": time.time(),
        }
        put_json(self.store, f"notes/{note_id}.json", note, if_none_match=True)
        try:
            put_json(self.store, idem_key, {"note_id": note_id}, if_none_match=True)
        except PreconditionFailed:
            pass
        return note, False

    def get_note(self, note_id: str) -> dict:
        data, _ = get_json(self.store, f"notes/{note_id}.json")
        return data

    def list_notes(self, limit: int = 50) -> list[dict]:
        keys = self.store.list("notes/")[-limit:]
        out = []
        for k in keys:
            try:
                out.append(json.loads(self.store.get(k).data))
            except NotFound:
                continue
        return out

    # -- drill sequence writes (RPO measurement) ----------------------------------------------
    def write_seq(self, drill_id: str, seq: int) -> dict:
        key = f"seq/{drill_id}/{seq:010d}.json"
        state = self.check_write_allowed()
        record = {"drill_id": drill_id, "seq": seq, "region": self.region, "epoch": state.epoch, "acked_at": time.time()}
        try:
            put_json(self.store, key, record, if_none_match=True)
            return {**record, "duplicate": False}
        except PreconditionFailed:
            existing, _ = get_json(self.store, key)
            return {**existing, "duplicate": True}
