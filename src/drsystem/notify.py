"""Notifications. Locally: an outbox file. On AWS: SNS email (``drsystem.aws.notify_sns``)."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Protocol

log = logging.getLogger("drsystem.notify")


class Notifier(Protocol):
    def notify(self, subject: str, message: str, severity: str = "INFO") -> None: ...


class OutboxNotifier:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.sent: list[dict] = []
        self._lock = threading.Lock()

    def notify(self, subject: str, message: str, severity: str = "INFO") -> None:
        item = {"t": time.time(), "severity": severity, "subject": subject, "message": message}
        with self._lock:
            self.sent.append(item)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(item) + "\n")
        log.warning("[%s] %s: %s", severity, subject, message)
