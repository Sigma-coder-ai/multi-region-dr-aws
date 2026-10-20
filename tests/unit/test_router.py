import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from drsystem.sim.cluster import ServerThread, free_port
from drsystem.sim.router import create_router_app


def origin(name: str, state: dict) -> FastAPI:
    app = FastAPI()

    @app.api_route("/whoami", methods=["GET", "POST"])
    def whoami():
        if state.get("status"):
            return JSONResponse({"error": "X"}, status_code=state["status"])
        return {"region": name}

    return app


@pytest.fixture
def edge():
    a_state, b_state = {}, {}
    pa, pb, pr = free_port(), free_port(), free_port()
    servers = [ServerThread(origin("A", a_state), pa, "a"), ServerThread(origin("B", b_state), pb, "b")]
    router = create_router_app({"A": f"http://127.0.0.1:{pa}", "B": f"http://127.0.0.1:{pb}"}, primary="A", origin_timeout_s=0.5)
    servers.append(ServerThread(router, pr, "edge"))
    for s in servers:
        s.start()
    yield f"http://127.0.0.1:{pr}", a_state, b_state, servers
    for s in servers:
        s.stop()


def test_get_prefers_primary(edge):
    url, *_ = edge
    r = httpx.get(url + "/api/whoami")
    assert r.json()["region"] == "A" and "x-origin-failover" not in r.headers


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_get_fails_over_on_5xx(edge, status):
    url, a, _, _ = edge
    a["status"] = status
    r = httpx.get(url + "/api/whoami")
    assert r.json()["region"] == "B" and r.headers["x-origin-failover"] == "1"


def test_get_does_not_fail_over_on_4xx(edge):
    url, a, _, _ = edge
    a["status"] = 404
    assert httpx.get(url + "/api/whoami").status_code == 404


def test_post_is_not_failed_over(edge):
    url, a, _, _ = edge
    a["status"] = 503
    r = httpx.post(url + "/api/whoami")
    assert r.status_code == 503  # CloudFront origin groups only fail over GET/HEAD/OPTIONS


def test_unreachable_primary_fails_over(edge):
    url, _, _, servers = edge
    servers[0].stop()
    assert httpx.get(url + "/api/whoami", timeout=5).json()["region"] == "B"


def test_regional_path_behaviour(edge):
    url, *_ = edge
    assert httpx.post(url + "/r/B/whoami").json()["region"] == "B"
    assert httpx.get(url + "/r/Z/whoami").status_code == 404
