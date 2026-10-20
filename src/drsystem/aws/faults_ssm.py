"""Fault flags from SSM Parameter Store (standard tier, free). Used for REAL-AWS drills.

The parameter holds JSON such as ``{"error_rate": 0.7}``. A missing parameter means no fault.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import fields
from typing import Any

from ..faults import FaultState


class SsmFaults:
    def __init__(self, parameter_name: str, region: str, client: Any | None = None, cache_s: float = 2.0) -> None:
        import boto3

        self.name = parameter_name
        self.ssm = client or boto3.client("ssm", region_name=region)
        self.cache_s = cache_s
        self._cached: tuple[float, FaultState] | None = None
        self._lock = threading.Lock()

    def current(self) -> FaultState:
        now = time.monotonic()
        with self._lock:
            if self._cached and now - self._cached[0] < self.cache_s:
                return self._cached[1]
        try:
            raw = self.ssm.get_parameter(Name=self.name)["Parameter"]["Value"]
            data = json.loads(raw)
            allowed = {f.name for f in fields(FaultState)}
            state = FaultState(**{k: v for k, v in data.items() if k in allowed})
        except Exception:  # noqa: BLE001 - missing/invalid parameter means "no fault"
            state = FaultState()
        with self._lock:
            self._cached = (now, state)
        return state
