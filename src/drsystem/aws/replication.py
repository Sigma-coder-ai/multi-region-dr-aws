"""Replication monitor without paid metrics: write a heartbeat object to the source bucket and
check when it appears in the destination bucket (S3 CRR replicates it like any object).

S3 Replication metrics and Replication Time Control are billed, so the lab uses heartbeats.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from ..storage.base import NotFound, ObjectStore, StoreUnavailable

HEARTBEAT_PREFIX = "replication-heartbeat/"


@dataclass(frozen=True)
class HeartbeatStats:
    source: str
    destination: str
    pending: int
    oldest_pending_age_s: float
    replicated_total: int = 0


class MarkerReplicationMonitor:
    def __init__(self, stores: dict[str, ObjectStore]) -> None:
        self.stores = stores
        self._inflight: dict[str, tuple[str, float]] = {}

    def _peer(self, region: str) -> str:
        return next(r for r in self.stores if r != region)

    def stats(self, source: str) -> HeartbeatStats:
        dst = self._peer(source)
        now = time.time()
        key, sent = self._inflight.get(source, (None, 0.0))
        if key:
            try:
                # The key is reused, so check that the replica is THIS heartbeat, not an older one.
                arrived = self.stores[dst].get(key).json().get("sent_at", 0) >= sent
            except (NotFound, StoreUnavailable, ValueError):
                arrived = False
            if not arrived:
                return HeartbeatStats(source, dst, 1, now - sent)
            del self._inflight[source]
            return HeartbeatStats(source, dst, 0, 0.0)  # everything written before `sent` has replicated
        try:
            key = f"{HEARTBEAT_PREFIX}{source}.json"
            self.stores[source].put(key, json.dumps({"sent_at": now}).encode())
            self._inflight[source] = (key, now)
        except StoreUnavailable:
            return HeartbeatStats(source, dst, 1, 0.0)
        return HeartbeatStats(source, dst, 1, 0.0)  # just sent: unknown until it arrives

    def pending_keys(self, source: str) -> list[str]:
        return [self._inflight[source][0]] if source in self._inflight else []
