"""SIMULATED two-way asynchronous replication between two regional stores (stands in for S3 CRR).

Behaviour modelled on S3 CRR:
- asynchronous, with configurable lag and jitter;
- replicas are not re-replicated;
- conflicts resolve last-writer-wins on ``last_modified``;
- keys under excluded prefixes (``local/``) are region-local and never replicated;
- if the source or destination region is down, pending objects wait (they are not lost, but they
  are *unavailable* in the other region, which is exactly what creates RPO during a failover).
"""

from __future__ import annotations

import heapq
import itertools
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..storage.base import StoredObject, StoreUnavailable
from ..storage.filestore import FileObjectStore

EXCLUDED_PREFIXES = ("local/",)


@dataclass(frozen=True)
class ReplicationStats:
    source: str
    destination: str
    pending: int
    oldest_pending_age_s: float
    replicated_total: int


class SimulatedReplicator:
    def __init__(
        self,
        stores: dict[str, FileObjectStore],
        is_region_up: Callable[[str], bool],
        lag_s: float = 0.3,
        jitter_s: float = 0.1,
        seed: int | None = None,
    ) -> None:
        if len(stores) != 2:
            raise ValueError("exactly two stores required")
        self.stores = stores
        self.is_region_up = is_region_up
        self.lag_s = lag_s
        self.jitter_s = jitter_s
        self._rand = random.Random(seed)
        self._queue: list[tuple[float, int, str, str, StoredObject, float]] = []
        self._counter = itertools.count()
        self._lock = threading.Lock()
        self._replicated: dict[tuple[str, str], int] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        for name, store in stores.items():
            store.add_write_listener(self._make_listener(name))

    def _peer(self, region: str) -> str:
        return next(r for r in self.stores if r != region)

    def _make_listener(self, source: str):
        def on_write(obj: StoredObject) -> None:
            if obj.key.startswith(EXCLUDED_PREFIXES):
                return
            if obj.metadata.get("replication_status") == "REPLICA":
                return
            delay = max(0.0, self.lag_s + self._rand.uniform(-self.jitter_s, self.jitter_s))
            now = time.time()
            with self._lock:
                heapq.heappush(
                    self._queue, (now + delay, next(self._counter), source, self._peer(source), obj, now)
                )

        return on_write

    # -- lifecycle ------------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="sim-replicator", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.pump()
            self._stop.wait(0.02)

    # -- work -----------------------------------------------------------------------------
    def pump(self, now: float | None = None) -> int:
        """Ship every due object whose source and destination are up. Returns objects shipped."""
        now = time.time() if now is None else now
        shipped = 0
        deferred = []
        with self._lock:
            while self._queue and self._queue[0][0] <= now:
                item = heapq.heappop(self._queue)
                _, _, src, dst, obj, _ = item
                if not (self.is_region_up(src) and self.is_region_up(dst)):
                    deferred.append(item)
                    continue
                try:
                    self.stores[dst].put_replica(obj)
                except StoreUnavailable:
                    deferred.append(item)
                    continue
                self._replicated[(src, dst)] = self._replicated.get((src, dst), 0) + 1
                shipped += 1
            for item in deferred:
                heapq.heappush(self._queue, item)
        return shipped

    def stats(self, source: str, now: float | None = None) -> ReplicationStats:
        now = time.time() if now is None else now
        dst = self._peer(source)
        with self._lock:
            items = [i for i in self._queue if i[2] == source]
            oldest = min((i[5] for i in items), default=None)
            total = self._replicated.get((source, dst), 0)
        return ReplicationStats(
            source=source,
            destination=dst,
            pending=len(items),
            oldest_pending_age_s=0.0 if oldest is None else now - oldest,
            replicated_total=total,
        )

    def pending_keys(self, source: str) -> list[str]:
        with self._lock:
            return sorted({i[4].key for i in self._queue if i[2] == source})
