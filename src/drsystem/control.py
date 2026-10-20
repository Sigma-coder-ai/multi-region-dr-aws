"""Replicated control state: which region holds write authority.

The control object lives in the *replicated data plane* (object store), not in a DNS or
traffic-control API, so failover needs no control-plane call. Every change is a conditional
write guarded by ``epoch``; two orchestrators can never both promote.

Fields
- ``active_region``  region allowed to accept writes
- ``epoch``          increases by one on every change (fencing token)
- ``demoted_region`` region removed by a failover; it answers 503 so traffic moves away from it.
                     Cleared on failback.
- ``write_freeze``   temporarily rejects all writes (used during controlled failback)
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, replace

from .storage.base import NotFound, ObjectStore, PreconditionFailed, get_json, put_json

CONTROL_KEY = "control/active-region.json"
LOCK_KEY = "local/control/lock.json"  # region-local, excluded from replication


class FencingError(Exception):
    """Promotion rejected because the observed epoch is stale."""


@dataclass(frozen=True)
class ControlState:
    active_region: str
    epoch: int
    changed_at: float
    changed_by: str
    reason: str
    demoted_region: str | None = None
    write_freeze: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ControlState":
        return cls(**d)


def read_control(store: ObjectStore) -> tuple[ControlState, str]:
    data, etag = get_json(store, CONTROL_KEY)
    return ControlState.from_dict(data), etag


def bootstrap_control(store: ObjectStore, active_region: str, by: str = "bootstrap") -> ControlState:
    """Create the control object if missing (no-op if it already exists)."""
    state = ControlState(active_region, 1, time.time(), by, "initial bootstrap")
    try:
        put_json(store, CONTROL_KEY, state.to_dict(), if_none_match=True)
    except PreconditionFailed:
        state, _ = read_control(store)
    return state


def _conditional_update(store: ObjectStore, expected_epoch: int, mutate) -> ControlState:
    try:
        current, etag = read_control(store)
    except NotFound as exc:
        raise FencingError("control object missing") from exc
    if current.epoch != expected_epoch:
        raise FencingError(f"expected epoch {expected_epoch}, found {current.epoch}")
    new = mutate(current)
    try:
        put_json(store, CONTROL_KEY, new.to_dict(), if_match=etag)
    except PreconditionFailed as exc:
        raise FencingError("concurrent control update") from exc
    return new


def promote(
    store: ObjectStore,
    new_active: str,
    expected_epoch: int,
    by: str,
    reason: str,
    *,
    demote_previous: bool,
) -> ControlState:
    def mutate(cur: ControlState) -> ControlState:
        return ControlState(
            active_region=new_active,
            epoch=cur.epoch + 1,
            changed_at=time.time(),
            changed_by=by,
            reason=reason,
            demoted_region=cur.active_region if demote_previous and cur.active_region != new_active else None,
            write_freeze=False,
        )

    return _conditional_update(store, expected_epoch, mutate)


def set_write_freeze(store: ObjectStore, expected_epoch: int, frozen: bool, by: str, reason: str) -> ControlState:
    return _conditional_update(
        store,
        expected_epoch,
        lambda cur: replace(
            cur, epoch=cur.epoch + 1, write_freeze=frozen, changed_at=time.time(), changed_by=by, reason=reason
        ),
    )


# -- workflow lock (region-local, conditional writes, TTL) ------------------------------------
@dataclass(frozen=True)
class Lock:
    owner: str
    expires_at: float


def acquire_lock(store: ObjectStore, owner: str, ttl_s: float, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    lock = Lock(owner, now + ttl_s)
    try:
        put_json(store, LOCK_KEY, asdict(lock), if_none_match=True)
        return True
    except PreconditionFailed:
        pass
    try:
        data, etag = get_json(store, LOCK_KEY)
    except NotFound:
        return acquire_lock(store, owner, ttl_s, now)
    if data["expires_at"] > now and data["owner"] != owner:
        return False
    try:
        put_json(store, LOCK_KEY, asdict(lock), if_match=etag)
        return True
    except PreconditionFailed:
        return False


def release_lock(store: ObjectStore, owner: str) -> None:
    try:
        data, _ = get_json(store, LOCK_KEY)
    except NotFound:
        return
    if data["owner"] == owner:
        store.delete(LOCK_KEY)
