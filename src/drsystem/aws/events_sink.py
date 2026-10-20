"""Durable timeline on AWS: each event is a small JSON object in the DR bucket.

Lambda invocations are short-lived, so in-memory events would be lost. Drill reports read the
events back to compute detection, decision and promotion times. Per-tick ``score`` events are
skipped (see ``EventLog.sink_skip``) to keep S3 request counts tiny.
"""

from __future__ import annotations

import json
import time
import uuid

from ..storage.base import ObjectStore

PREFIX = "local/events/"


class S3EventSink:
    def __init__(self, store: ObjectStore, prefix: str = PREFIX) -> None:
        self.store = store
        self.prefix = prefix

    def __call__(self, event: dict) -> None:
        day = time.strftime("%Y%m%d", time.gmtime(event["t"]))
        key = f"{self.prefix}{day}/{event['t']:.3f}-{uuid.uuid4().hex[:6]}.json"
        try:
            self.store.put(key, json.dumps(event, default=str).encode())
        except Exception:  # noqa: BLE001 - the timeline must never break recovery itself
            pass


def read_events(store: ObjectStore, since: float, prefix: str = PREFIX) -> list[dict]:
    days = {time.strftime("%Y%m%d", time.gmtime(t)) for t in (since, time.time())}
    out = []
    for day in sorted(days):
        for key in store.list(f"{prefix}{day}/"):
            ts = float(key.rsplit("/", 1)[1].split("-")[0])
            if ts >= since - 1:
                out.append(store.get(key).json())
    return sorted(out, key=lambda e: e["t"])
