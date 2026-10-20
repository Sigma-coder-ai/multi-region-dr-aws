"""Timeline / event log. Every event carries the evidence label (SIMULATED or REAL-AWS)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class EventLog:
    def __init__(
        self,
        label: str,
        path: str | Path | None = None,
        run_id: str = "",
        sink=None,
        sink_skip: frozenset[str] = frozenset({"score", "wf_step_start"}),
    ) -> None:
        self.sink = sink  # optional callable(event) for durable storage (S3 on AWS)
        self.sink_skip = sink_skip
        if label not in {"SIMULATED", "REAL-AWS"}:
            raise ValueError("label must be SIMULATED or REAL-AWS")
        self.label = label
        self.run_id = run_id
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, kind: str, **details: Any) -> dict[str, Any]:
        event = {"t": time.time(), "kind": kind, "label": self.label, "run_id": self.run_id, **details}
        with self._lock:
            self._events.append(event)
            if self.path:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, default=str) + "\n")
        if self.sink is not None and kind not in self.sink_skip:
            self.sink(event)
        return event

    def events(self, kinds: set[str] | None = None, since: float | None = None) -> list[dict[str, Any]]:
        with self._lock:
            out = list(self._events)
        if kinds:
            out = [e for e in out if e["kind"] in kinds]
        if since is not None:
            out = [e for e in out if e["t"] >= since]
        return out

    def first(self, kind: str, since: float | None = None, **match: Any) -> dict[str, Any] | None:
        for e in self.events({kind}, since):
            if all(e.get(k) == v for k, v in match.items()):
                return e
        return None
