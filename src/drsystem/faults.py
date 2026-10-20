"""Fault flags read by the app on every request.

Locally the flags live in memory and are flipped by drills or the dashboard. On AWS the same
``FaultState`` is read from SSM Parameter Store (``drsystem.aws.faults_ssm``), so a real drill
can fail a real region without touching code.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, replace
from typing import Protocol


@dataclass(frozen=True)
class FaultState:
    outage: bool = False  # region unreachable: requests hang, replication stops
    error_rate: float = 0.0  # fraction of requests answered with HTTP 500 (gray failure)
    latency_ms: int = 0  # extra latency on every request (gray failure)
    storage_down: bool = False  # data store errors -> region not ready
    health_fail: bool = False  # only /health reports failure (control-path drill)

    @property
    def any(self) -> bool:
        return self != FaultState()

    def to_dict(self) -> dict:
        return asdict(self)


class FaultSource(Protocol):
    def current(self) -> FaultState: ...


class InMemoryFaults:
    def __init__(self) -> None:
        self._state = FaultState()
        self._lock = threading.Lock()

    def current(self) -> FaultState:
        with self._lock:
            return self._state

    def set(self, **changes) -> FaultState:
        with self._lock:
            self._state = replace(self._state, **changes)
            return self._state

    def clear(self) -> None:
        with self._lock:
            self._state = FaultState()
