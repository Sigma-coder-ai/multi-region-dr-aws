"""Object store interface modelled on the S3 semantics the DR design relies on.

- Strong read-after-write consistency within one region.
- Conditional writes: ``if_match`` (ETag must match) and ``if_none_match`` (key must not exist).
  S3 supports both on PutObject, so promotion/locking logic is identical locally and on AWS.
- Replicas carry ``replication_status = "REPLICA"`` and are never re-replicated (as with S3 CRR).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class NotFound(KeyError):
    pass


class PreconditionFailed(Exception):
    """Conditional write rejected (S3: HTTP 412)."""


class StoreUnavailable(Exception):
    """The store cannot be reached (simulated or real regional failure)."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    data: bytes
    etag: str
    last_modified: float
    metadata: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.data)


@runtime_checkable
class ObjectStore(Protocol):
    region: str

    def get(self, key: str) -> StoredObject: ...

    def put(
        self,
        key: str,
        data: bytes,
        *,
        if_match: str | None = None,
        if_none_match: bool = False,
        metadata: dict[str, str] | None = None,
    ) -> str: ...

    def list(self, prefix: str = "") -> list[str]: ...

    def delete(self, key: str) -> None: ...


def put_json(store: ObjectStore, key: str, value: Any, **kwargs: Any) -> str:
    return store.put(key, json.dumps(value, sort_keys=True).encode(), **kwargs)


def get_json(store: ObjectStore, key: str) -> tuple[Any, str]:
    obj = store.get(key)
    return obj.json(), obj.etag
