import time

import pytest

from drsystem.control import (
    CONTROL_KEY,
    FencingError,
    acquire_lock,
    bootstrap_control,
    promote,
    read_control,
    release_lock,
    set_write_freeze,
)
from drsystem.storage.base import NotFound, PreconditionFailed, StoreUnavailable

from ..conftest import DR, HOME


def test_conditional_writes(stores):
    s = stores[HOME]
    etag = s.put("a.txt", b"1", if_none_match=True)
    with pytest.raises(PreconditionFailed):
        s.put("a.txt", b"2", if_none_match=True)
    with pytest.raises(PreconditionFailed):
        s.put("a.txt", b"2", if_match="wrong")
    new = s.put("a.txt", b"2", if_match=etag)
    assert new != etag and s.get("a.txt").data == b"2"
    assert s.get("a.txt").metadata["written_region"] == HOME


def test_list_get_delete_and_invalid_keys(stores):
    s = stores[HOME]
    s.put("notes/1.json", b"{}")
    s.put("notes/2.json", b"{}")
    s.put("other/x", b"{}")
    assert s.list("notes/") == ["notes/1.json", "notes/2.json"]
    s.delete("notes/1.json")
    with pytest.raises(NotFound):
        s.get("notes/1.json")
    with pytest.raises(ValueError):
        s.put("../escape", b"x")


def test_unavailable_store_raises(stores):
    s = stores[HOME]
    s.available = lambda: False
    with pytest.raises(StoreUnavailable):
        s.get("x")


def test_replica_last_writer_wins(stores):
    a, b = stores[HOME], stores[DR]
    a.put("k", b"old")
    old = a.get("k")
    time.sleep(0.05)  # Windows clock resolution is ~15 ms; equal timestamps would make the test flaky
    b.put("k", b"newer")
    assert b.put_replica(old) is False  # older replica must not overwrite newer data
    assert b.get("k").data == b"newer"


def test_bootstrap_is_idempotent(stores):
    s1 = bootstrap_control(stores[HOME], HOME)
    s2 = bootstrap_control(stores[HOME], DR)
    assert s1.active_region == s2.active_region == HOME and s2.epoch == 1


def test_promote_increments_epoch_and_demotes(stores):
    bootstrap_control(stores[HOME], HOME)
    new = promote(stores[HOME], DR, 1, by="test", reason="r", demote_previous=True)
    assert (new.active_region, new.epoch, new.demoted_region) == (DR, 2, HOME)


def test_promote_with_stale_epoch_is_fenced(stores):
    bootstrap_control(stores[HOME], HOME)
    promote(stores[HOME], DR, 1, by="a", reason="r", demote_previous=True)
    with pytest.raises(FencingError):
        promote(stores[HOME], DR, 1, by="b", reason="r", demote_previous=True)


def test_freeze_and_failback_clears_flags(stores):
    bootstrap_control(stores[HOME], HOME)
    promote(stores[HOME], DR, 1, by="a", reason="r", demote_previous=True)
    frozen = set_write_freeze(stores[HOME], 2, True, by="fb", reason="r")
    assert frozen.write_freeze and frozen.epoch == 3
    back = promote(stores[HOME], HOME, 3, by="fb", reason="r", demote_previous=False)
    assert back.active_region == HOME and back.demoted_region is None and not back.write_freeze


def test_lock_exclusive_until_expiry(stores):
    s = stores[DR]
    assert acquire_lock(s, "a", ttl_s=10, now=100)
    assert not acquire_lock(s, "b", ttl_s=10, now=105)
    assert acquire_lock(s, "b", ttl_s=10, now=111)  # expired
    release_lock(s, "a")  # not the owner: no effect
    assert not acquire_lock(s, "c", ttl_s=10, now=112)
    release_lock(s, "b")
    assert acquire_lock(s, "c", ttl_s=10, now=113)


def test_control_key_is_replicated_lock_is_not():
    from drsystem.control import LOCK_KEY
    from drsystem.sim.replicator import EXCLUDED_PREFIXES

    assert not CONTROL_KEY.startswith(EXCLUDED_PREFIXES)
    assert LOCK_KEY.startswith(EXCLUDED_PREFIXES)
    assert read_control  # imported for API completeness
