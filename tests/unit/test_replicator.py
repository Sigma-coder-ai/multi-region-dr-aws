from drsystem.sim.replicator import SimulatedReplicator
from drsystem.storage.base import NotFound

from ..conftest import DR, HOME


def make(stores, up):
    return SimulatedReplicator(stores, lambda r: up[r], lag_s=1.0, jitter_s=0.0, seed=1)


def test_replicates_after_lag_both_ways(stores):
    up = {HOME: True, DR: True}
    rep = make(stores, up)
    stores[HOME].put("notes/a.json", b"A")
    stores[DR].put("notes/b.json", b"B")
    obj_time = stores[HOME].get("notes/a.json").last_modified
    assert rep.pump(now=obj_time + 0.5) == 0
    assert rep.stats(HOME).pending == 1
    assert rep.pump(now=obj_time + 5) == 2
    assert stores[DR].get("notes/a.json").metadata["replication_status"] == "REPLICA"
    assert stores[HOME].get("notes/b.json").data == b"B"
    assert rep.stats(HOME).replicated_total == 1


def test_replicas_are_not_re_replicated(stores):
    up = {HOME: True, DR: True}
    rep = make(stores, up)
    stores[HOME].put("k", b"x")
    t = stores[HOME].get("k").last_modified
    rep.pump(now=t + 5)
    assert rep.stats(DR).pending == 0


def test_local_prefix_not_replicated(stores):
    rep = make(stores, {HOME: True, DR: True})
    stores[HOME].put("local/state.json", b"x")
    assert rep.stats(HOME).pending == 0


def test_outage_strands_pending_objects_until_recovery(stores):
    up = {HOME: True, DR: True}
    rep = make(stores, up)
    stores[HOME].put("seq/1", b"1")
    t = stores[HOME].get("seq/1").last_modified
    up[HOME] = False
    assert rep.pump(now=t + 5) == 0
    try:
        stores[DR].get("seq/1")
        raise AssertionError("should not have replicated during outage")
    except NotFound:
        pass
    assert rep.stats(HOME, now=t + 5).oldest_pending_age_s >= 4.9
    assert rep.pending_keys(HOME) == ["seq/1"]
    up[HOME] = True
    assert rep.pump(now=t + 6) == 1
    assert stores[DR].get("seq/1").data == b"1"
