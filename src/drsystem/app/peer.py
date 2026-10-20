"""Reads the peer region's control state over HTTP for peer fencing (short timeout, cached)."""

from __future__ import annotations

import threading
import time

import httpx

from ..control import ControlState


class HttpPeerView:
    def __init__(
        self,
        peer_endpoint: str,
        timeout_s: float = 0.3,
        cache_s: float = 1.0,
        unreachable_cache_s: float = 3.0,
        auth: httpx.Auth | None = None,
    ) -> None:
        self.auth = auth
        self.url = peer_endpoint.rstrip("/") + "/internal/control"
        self.timeout_s = timeout_s
        self.cache_s = cache_s
        self.unreachable_cache_s = unreachable_cache_s
        self._cached: tuple[float, ControlState | None] | None = None
        self._lock = threading.Lock()

    def peer_control(self) -> ControlState | None:
        now = time.monotonic()
        with self._lock:
            if self._cached:
                ttl = self.cache_s if self._cached[1] is not None else self.unreachable_cache_s
                if now - self._cached[0] < ttl:
                    return self._cached[1]
        try:
            resp = httpx.get(self.url, timeout=self.timeout_s, auth=self.auth)
            state = ControlState.from_dict(resp.json()) if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError, TypeError):
            state = None  # unreachable peer -> local view wins
        with self._lock:
            self._cached = (now, state)
        return state
