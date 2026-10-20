import pytest
from fastapi.testclient import TestClient

from drsystem.app.api import create_app
from drsystem.app.service import RegionService, WriteRejected
from drsystem.control import bootstrap_control, promote, set_write_freeze

from ..conftest import DR, HOME


class FakePeer:
    def __init__(self, state=None):
        self.state = state

    def peer_control(self):
        return self.state


@pytest.fixture
def svc(stores, faults):
    bootstrap_control(stores[HOME], HOME)
    stores[DR].put_replica(stores[HOME].get("control/active-region.json"))
    return {r: RegionService(r, stores[r], faults[r]) for r in (HOME, DR)}


def test_only_active_region_accepts_writes(svc):
    note, replayed = svc[HOME].create_note("t", "b", "key-1")
    assert note["written_region"] == HOME and not replayed
    with pytest.raises(WriteRejected) as exc:
        svc[DR].create_note("t", "b", "key-2")
    assert exc.value.code == "READ_ONLY" and exc.value.active_region == HOME


def test_idempotent_replay(svc):
    n1, _ = svc[HOME].create_note("t", "b", "same")
    n2, replayed = svc[HOME].create_note("t", "b", "same")
    assert replayed and n1["id"] == n2["id"]


def test_write_freeze_rejects(svc, stores):
    set_write_freeze(stores[HOME], 1, True, by="t", reason="r")
    with pytest.raises(WriteRejected) as exc:
        svc[HOME].write_seq("d1", 0)
    assert exc.value.code == "FROZEN"


def test_peer_fencing_rejects_stale_local_view(svc, stores):
    # DR has promoted itself (epoch 2) but HOME has not received the replicated control yet.
    promote(stores[DR], DR, 1, by="t", reason="r", demote_previous=True)
    from drsystem.control import read_control

    svc[HOME].peer = FakePeer(read_control(stores[DR])[0])
    with pytest.raises(WriteRejected) as exc:
        svc[HOME].write_seq("d1", 0)
    assert exc.value.code == "STALE_EPOCH" and exc.value.active_region == DR


def test_unreachable_peer_does_not_block_writes(svc):
    svc[HOME].peer = FakePeer(None)
    assert svc[HOME].write_seq("d1", 0)["region"] == HOME


def test_seq_write_is_idempotent(svc):
    a = svc[HOME].write_seq("d1", 5)
    b = svc[HOME].write_seq("d1", 5)
    assert not a["duplicate"] and b["duplicate"]


def test_api_health_and_demotion(svc, stores, faults):
    home = TestClient(create_app(svc[HOME]))
    dr = TestClient(create_app(svc[DR]))
    assert home.get("/health?expect=active").status_code == 200
    assert dr.get("/health?expect=active").status_code == 503
    assert dr.get("/health").status_code == 200  # ready standby
    promote(stores[HOME], DR, 1, by="t", reason="r", demote_previous=True)
    assert home.get("/whoami").json()["error"] == "DEMOTED"
    faults[HOME].set(health_fail=True)
    assert home.get("/health").status_code == 503


def test_api_write_rejection_and_validation(svc):
    dr = TestClient(create_app(svc[DR]))
    r = dr.post("/notes", json={"title": "x"}, headers={"Idempotency-Key": "k"})
    assert r.status_code == 503 and r.json()["error"] == "READ_ONLY"
    home = TestClient(create_app(svc[HOME]))
    assert home.post("/notes", json={"title": "x"}).status_code == 400  # missing idempotency key
    assert home.post("/notes", json={"title": "x"}, headers={"Idempotency-Key": "k"}).status_code == 201
    assert home.post("/notes", json={"title": "x"}, headers={"Idempotency-Key": "k"}).status_code == 200


def test_api_injected_errors(svc, faults):
    faults[HOME].set(error_rate=1.0)
    home = TestClient(create_app(svc[HOME]))
    assert home.get("/whoami").status_code == 500


def test_storage_down_makes_region_not_ready(svc, stores):
    stores[HOME].available = lambda: False
    home = TestClient(create_app(svc[HOME]))
    r = home.get("/health")
    assert r.status_code == 503 and r.json()["ready"] is False
