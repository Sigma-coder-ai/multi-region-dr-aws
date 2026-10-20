"""Local filesystem object store: one directory per simulated region.

Objects live at ``<root>/objects/<key>`` with a ``.meta.json`` sidecar, so the state of each
simulated region can be inspected with a file browser during a demo.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from .base import NotFound, PreconditionFailed, StoredObject, StoreUnavailable

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()

WriteListener = Callable[[StoredObject], None]


def _lock_for(root: Path) -> threading.RLock:
    key = str(root.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


class FileObjectStore:
    def __init__(self, root: str | Path, region: str) -> None:
        self.root = Path(root)
        self.region = region
        self._objects = self.root / "objects"
        self._objects.mkdir(parents=True, exist_ok=True)
        self._lock = _lock_for(self.root)
        self._listeners: list[WriteListener] = []
        self.available: Callable[[], bool] = lambda: True

    # -- listeners (used by the simulated replicator) -------------------------------------
    def add_write_listener(self, fn: WriteListener) -> None:
        self._listeners.append(fn)

    # -- helpers ---------------------------------------------------------------------------
    def _check(self) -> None:
        if not self.available():
            raise StoreUnavailable(f"store in {self.region} is unavailable")

    def _path(self, key: str) -> Path:
        if not key or key.startswith("/") or ".." in key.split("/"):
            raise ValueError(f"invalid key {key!r}")
        return self._objects / key

    @staticmethod
    def _meta_path(p: Path) -> Path:
        return p.with_name(p.name + ".meta.json")

    def _read(self, key: str) -> StoredObject | None:
        p = self._path(key)
        mp = self._meta_path(p)
        if not p.exists() or not mp.exists():
            return None
        meta = json.loads(mp.read_text())
        return StoredObject(
            key=key,
            data=p.read_bytes(),
            etag=meta["etag"],
            last_modified=meta["last_modified"],
            metadata=meta.get("metadata", {}),
        )

    def _write(self, obj: StoredObject) -> None:
        p = self._path(obj.key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp-{uuid.uuid4().hex}")
        tmp.write_bytes(obj.data)
        os.replace(tmp, p)
        meta = {"etag": obj.etag, "last_modified": obj.last_modified, "metadata": obj.metadata}
        mtmp = p.with_name(p.name + f".mtmp-{uuid.uuid4().hex}")
        mtmp.write_text(json.dumps(meta))
        os.replace(mtmp, self._meta_path(p))

    # -- ObjectStore API -------------------------------------------------------------------
    def get(self, key: str) -> StoredObject:
        self._check()
        with self._lock:
            obj = self._read(key)
        if obj is None:
            raise NotFound(key)
        return obj

    def put(
        self,
        key: str,
        data: bytes,
        *,
        if_match: str | None = None,
        if_none_match: bool = False,
        metadata: dict[str, str] | None = None,
    ) -> str:
        self._check()
        with self._lock:
            current = self._read(key)
            if if_none_match and current is not None:
                raise PreconditionFailed(f"{key} already exists")
            if if_match is not None and (current is None or current.etag != if_match):
                raise PreconditionFailed(f"{key} etag mismatch")
            obj = StoredObject(
                key=key,
                data=data,
                etag=uuid.uuid4().hex,
                last_modified=time.time(),
                metadata={**(metadata or {}), "written_region": self.region},
            )
            self._write(obj)
        for fn in self._listeners:
            fn(obj)
        return obj.etag

    def put_replica(self, obj: StoredObject) -> bool:
        """Apply a replicated object (last-writer-wins on ``last_modified``, like S3 CRR)."""
        self._check()
        with self._lock:
            current = self._read(obj.key)
            if current is not None and current.last_modified > obj.last_modified:
                return False
            replica = StoredObject(
                key=obj.key,
                data=obj.data,
                etag=obj.etag,
                last_modified=obj.last_modified,
                metadata={**obj.metadata, "replication_status": "REPLICA"},
            )
            self._write(replica)
            return True

    def list(self, prefix: str = "") -> list[str]:
        self._check()
        keys: list[str] = []
        with self._lock:
            for p in self._objects.rglob("*"):
                if not p.is_file() or ".meta.json" in p.name or ".tmp-" in p.name or ".mtmp-" in p.name:
                    continue
                key = p.relative_to(self._objects).as_posix()
                if key.startswith(prefix):
                    keys.append(key)
        return sorted(keys)

    def delete(self, key: str) -> None:
        self._check()
        with self._lock:
            p = self._path(key)
            for f in (p, self._meta_path(p)):
                if f.exists():
                    f.unlink()
