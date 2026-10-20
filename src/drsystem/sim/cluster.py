"""SIMULATED two-region deployment on one machine.

    edge (router, :8100) ── origin group ──> ap-south-1 app (:8101)  ⇄  ap-southeast-1 app (:8102)
                                             FileObjectStore A  <== replicator ==>  FileObjectStore B
    DR controller (runs "in" ap-southeast-1): prober -> scorer -> FailoverWorkflow

Every timeline event and report produced here is labelled SIMULATED.
"""

from __future__ import annotations

import shutil
import socket
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from ..app.api import create_app
from ..app.peer import HttpPeerView
from ..app.service import RegionService
from ..client import DRClient
from ..config import PROJECT_ROOT, RegionConfig, Settings
from ..control import CONTROL_KEY, bootstrap_control, read_control
from ..events import EventLog
from ..faults import InMemoryFaults
from ..notify import OutboxNotifier
from ..orchestrator.controller import Controller
from ..orchestrator.probe import HttpProber
from ..orchestrator.workflows import FailbackWorkflow, FailoverWorkflow, WorkflowResult
from ..storage.base import NotFound, StoreUnavailable
from ..storage.filestore import FileObjectStore
from . import SIMULATION_LABEL
from .replicator import SimulatedReplicator
from .router import create_router_app

WEB_DIR = PROJECT_ROOT / "web"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerThread:
    def __init__(self, app: FastAPI, port: int, name: str, host: str = "127.0.0.1") -> None:
        cfg = uvicorn.Config(app, host=host, port=port, log_level="warning", timeout_graceful_shutdown=1)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, name=name, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline or not self.thread.is_alive():
                raise RuntimeError(f"server {self.thread.name} failed to start")
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


class FaultIn(BaseModel):
    region: str
    outage: bool = False
    error_rate: float = 0.0
    latency_ms: int = 0
    storage_down: bool = False
    health_fail: bool = False


class FailbackIn(BaseModel):
    approved_by: str


class LocalCluster:
    def __init__(
        self,
        settings: Settings,
        workdir: str | Path | None = None,
        evidence_dir: str | Path | None = None,
        run_id: str | None = None,
        auto_ports: bool = False,
        controller_enabled: bool = True,
        serve_web: bool = True,
    ) -> None:
        if not settings.is_simulated:
            raise ValueError("LocalCluster only runs simulated profiles")
        if auto_ports:
            regions = tuple(replace(r, port=free_port()) for r in settings.regions)
            settings = replace(settings, regions=regions, router=replace(settings.router, port=free_port()))
        endpoints = {r.name: f"http://127.0.0.1:{r.port}" for r in settings.regions}
        self.settings = settings.with_endpoints(endpoints)
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.workdir = Path(workdir or PROJECT_ROOT / ".sim" / self.run_id)
        self.evidence_dir = Path(evidence_dir or PROJECT_ROOT / "evidence" / "simulated" / self.run_id)
        self.controller_enabled = controller_enabled
        self.serve_web = serve_web
        self.home = self.settings.home_region
        self.standby = self.settings.standby_region
        self.faults = {r.name: InMemoryFaults() for r in self.settings.regions}
        self.timeline = EventLog(SIMULATION_LABEL, self.evidence_dir / "events.jsonl", run_id=self.run_id)
        self.notifier = OutboxNotifier(self.evidence_dir / "notifications.jsonl")
        self.last_workflow: WorkflowResult | None = None
        self.workflows: list[WorkflowResult] = []
        self._servers: list[ServerThread] = []
        self._stop = threading.Event()
        self._loop: threading.Thread | None = None
        self._wf_lock = threading.Lock()

    # -- wiring ------------------------------------------------------------------------------
    @property
    def edge_url(self) -> str:
        return f"http://127.0.0.1:{self.settings.router.port}"

    def endpoint(self, region: str) -> str:
        return self.settings.region(region).endpoint

    def region_up(self, region: str) -> bool:
        return not self.faults[region].current().outage

    def client(self) -> DRClient:
        return DRClient(self.edge_url, timeout_s=self.settings.router.regional_timeout_s + 0.5)

    def start(self) -> "LocalCluster":
        if self.workdir.exists():
            shutil.rmtree(self.workdir)
        self.stores: dict[str, FileObjectStore] = {}
        for r in self.settings.regions:
            store = FileObjectStore(self.workdir / r.name, r.name)
            f = self.faults[r.name]
            store.available = lambda f=f: not (f.current().outage or f.current().storage_down)
            self.stores[r.name] = store
        state = bootstrap_control(self.stores[self.home], self.home)
        self.stores[self.standby].put_replica(self.stores[self.home].get(CONTROL_KEY))  # initial sync
        self.replicator = SimulatedReplicator(
            self.stores, self.region_up, self.settings.replication.lag_s, self.settings.replication.jitter_s
        )
        self.replicator.start()
        endpoints = {r.name: r.endpoint for r in self.settings.regions}
        for r in self.settings.regions:
            peer = None
            if self.settings.app.peer_fencing:
                peer = HttpPeerView(
                    endpoints[self.settings.peer_of(r.name)], self.settings.app.peer_timeout_s, self.settings.app.peer_cache_s
                )
            svc = RegionService(r.name, self.stores[r.name], self.faults[r.name], peer)
            self._servers.append(ServerThread(create_app(svc, endpoints, self.settings.mode), r.port, f"app-{r.name}"))
        router = create_router_app(
            endpoints,
            primary=self.home,
            origin_timeout_s=self.settings.router.origin_timeout_s,
            regional_timeout_s=self.settings.router.regional_timeout_s,
            web_dir=WEB_DIR if self.serve_web and WEB_DIR.exists() else None,
            extra=self._sim_routes,
        )
        self.router_app = router
        self._servers.append(ServerThread(router, self.settings.router.port, "edge-router", self.settings.router.host))
        for s in self._servers:
            s.start()
        self.prober = HttpProber(endpoints, self.settings.probe.timeout_s)
        self.controller = Controller(
            self.settings,
            self.stores[self.standby],
            self.prober,
            self._launch_failover,
            self.timeline,
            self.notifier,
            is_up=lambda: self.region_up(self.standby),
        )
        self.timeline.record("cluster_started", home=self.home, standby=self.standby, epoch=state.epoch, edge=self.edge_url)
        if self.controller_enabled:
            self._loop = threading.Thread(target=self._control_loop, name="dr-controller", daemon=True)
            self._loop.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        for f in self.faults.values():
            f.clear()  # release hung "outage" requests so servers shut down quickly
        if self._loop:
            self._loop.join(timeout=20)
        for s in reversed(self._servers):
            s.stop()
        self.replicator.stop()
        self.prober.close()
        self.timeline.record("cluster_stopped")

    def __enter__(self) -> "LocalCluster":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- controller ----------------------------------------------------------------------------
    def _control_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.controller.tick()
            except Exception as exc:  # noqa: BLE001 - the loop must survive transient errors
                self.timeline.record("controller_error", error=repr(exc))
            self._stop.wait(self.settings.probe.interval_s)

    def _workflow_kwargs(self) -> dict:
        return dict(
            settings=self.settings,
            stores=self.stores,
            control_store=self.stores[self.standby],
            prober=self.prober,
            replication=self.replicator,
            client_factory=self.client,
            notifier=self.notifier,
            timeline=self.timeline,
        )

    def _launch_failover(self, decision) -> WorkflowResult:
        with self._wf_lock:
            result = FailoverWorkflow(**self._workflow_kwargs()).run(decision)
            self.last_workflow = result
            self.workflows.append(result)
            return result

    def failback(self, approved_by: str) -> WorkflowResult:
        with self._wf_lock:
            result = FailbackWorkflow(**self._workflow_kwargs()).run(approved_by)
            self.last_workflow = result
            self.workflows.append(result)
            return result

    # -- faults ----------------------------------------------------------------------------------
    def inject(self, region: str, **fault) -> None:
        self.faults[region].set(**fault)
        self.timeline.record("fault_injected", region=region, fault=self.faults[region].current().to_dict())

    def clear(self, region: str) -> None:
        self.faults[region].clear()
        self.timeline.record("fault_cleared", region=region)

    # -- views -----------------------------------------------------------------------------------
    def control_view(self) -> dict:
        out = {}
        for r, store in self.stores.items():
            try:
                out[r] = read_control(store)[0].to_dict()
            except (StoreUnavailable, NotFound) as exc:
                out[r] = {"error": type(exc).__name__}
        return out

    def status(self) -> dict:
        d = self.controller.last_decision
        return {
            "label": SIMULATION_LABEL,
            "run_id": self.run_id,
            "home": self.home,
            "standby": self.standby,
            "regions": {
                r.name: {
                    "label": r.label,
                    "faults": self.faults[r.name].current().to_dict(),
                    "replication_out": vars(self.replicator.stats(r.name)),
                }
                for r in self.settings.regions
            },
            "control": self.control_view(),
            "last_decision": d.to_dict() if d else None,
            "last_workflow": vars(self.last_workflow) if self.last_workflow else None,
            "router": self.router_app.state.stats,
            "events": [e for e in self.timeline.events() if e["kind"] not in {"score", "wf_step_start"}][-25:],
        }

    def _sim_routes(self, app: FastAPI) -> None:
        @app.get("/_sim/status")
        def sim_status():
            return self.status()

        @app.post("/_sim/fault")
        def sim_fault(body: FaultIn):
            fault = body.model_dump()
            region = fault.pop("region")
            self.faults[region].clear()
            self.inject(region, **fault)
            return {"ok": True}

        @app.post("/_sim/clear/{region}")
        def sim_clear(region: str):
            self.clear(region)
            return {"ok": True}

        @app.post("/_sim/failback")
        def sim_failback(body: FailbackIn):
            threading.Thread(target=self.failback, args=(body.approved_by,), daemon=True).start()
            return {"ok": True, "started": True}


__all__ = ["LocalCluster", "RegionConfig"]
