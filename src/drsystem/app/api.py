"""HTTP API for one region (FastAPI). Runs under uvicorn locally and under Lambda (Mangum) on AWS.

Routes
  GET  /health[?expect=active]   deep health; 200 when ready (and active, if requested), else 503
  GET  /whoami                   which region answered (dashboard polls this)
  GET  /config                   active region + per-region endpoints (clients use it for write routing)
  GET  /internal/control         raw control state (peer fencing; IAM-auth on AWS)
  POST /notes                    create a note (Idempotency-Key header required; fenced)
  GET  /notes, /notes/{id}       read notes (any non-demoted region)
  POST /drill/seq                sequence write used to measure RPO (fenced)
"""

from __future__ import annotations

import asyncio
import os
import random

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..storage.base import NotFound, StoreUnavailable
from .service import RegionService, WriteRejected

# Simulated unreachable region: callers hit their own timeout first. Kept short on Lambda (env override)
# so hung invocations do not hold the account's small concurrency quota for long.
OUTAGE_HANG_S = float(os.environ.get("DR_OUTAGE_HANG_S", "30"))


class NoteIn(BaseModel):
    title: str = Field(max_length=200)
    body: str = ""


class SeqIn(BaseModel):
    drill_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    seq: int = Field(ge=0)


def create_app(service: RegionService, endpoints: dict[str, str] | None = None, mode: str = "local-sim") -> FastAPI:
    app = FastAPI(title=f"drsystem {service.region}", version=service.version)
    endpoints = endpoints or {}

    @app.middleware("http")
    async def fault_injection(request: Request, call_next):
        fault = service.faults.current()
        if fault.outage:
            # Hang like an unreachable region until the caller times out (or the outage ends).
            loop = asyncio.get_running_loop()
            deadline = loop.time() + OUTAGE_HANG_S
            while service.faults.current().outage and loop.time() < deadline:
                await asyncio.sleep(0.05)
            return JSONResponse({"error": "OUTAGE"}, status_code=503)
        if fault.latency_ms:
            await asyncio.sleep(fault.latency_ms / 1000)
        if fault.error_rate and random.random() < fault.error_rate:
            return JSONResponse({"error": "INJECTED_ERROR", "region": service.region}, status_code=500)
        response = await call_next(request)
        response.headers["x-dr-region"] = service.region
        return response

    def rejected(exc: WriteRejected) -> JSONResponse:
        status = 503 if exc.code in {"READ_ONLY", "FROZEN", "STALE_EPOCH", "UNAVAILABLE"} else 409
        return JSONResponse({**exc.to_dict(), "region": service.region}, status_code=status)

    def demoted_response() -> JSONResponse | None:
        try:
            if service.is_demoted():
                return JSONResponse({"error": "DEMOTED", "region": service.region}, status_code=503)
        except (StoreUnavailable, NotFound):
            return JSONResponse({"error": "UNAVAILABLE", "region": service.region}, status_code=503)
        return None

    @app.get("/health")
    def health(expect: str = "ready"):
        h = service.health()
        ok = h["ready"] and (h["active"] if expect == "active" else True)
        return JSONResponse(h, status_code=200 if ok else 503)

    @app.get("/whoami")
    def whoami():
        if (r := demoted_response()) is not None:
            return r
        state = service.control()
        return {"region": service.region, "active_region": state.active_region, "epoch": state.epoch, "mode": mode}

    @app.get("/config")
    def config():
        if (r := demoted_response()) is not None:
            return r
        state = service.control()
        return {
            "active_region": state.active_region,
            "epoch": state.epoch,
            "write_freeze": state.write_freeze,
            "endpoints": endpoints,
            "served_by": service.region,
        }

    @app.get("/internal/control")
    def internal_control():
        try:
            return service.control().to_dict()
        except (StoreUnavailable, NotFound):
            return JSONResponse({"error": "UNAVAILABLE"}, status_code=503)

    @app.post("/notes", status_code=201)
    def create_note(note: NoteIn, idempotency_key: str = Header(default="")):
        try:
            created, replayed = service.create_note(note.title, note.body, idempotency_key)
        except WriteRejected as exc:
            return rejected(exc)
        except ValueError as exc:
            return JSONResponse({"error": "BAD_REQUEST", "detail": str(exc)}, status_code=400)
        return JSONResponse({**created, "replayed": replayed}, status_code=200 if replayed else 201)

    @app.get("/notes")
    def list_notes(limit: int = 50):
        if (r := demoted_response()) is not None:
            return r
        return {"region": service.region, "notes": service.list_notes(min(max(limit, 1), 200))}

    @app.get("/notes/{note_id}")
    def get_note(note_id: str):
        if (r := demoted_response()) is not None:
            return r
        try:
            return service.get_note(note_id)
        except NotFound:
            return JSONResponse({"error": "NOT_FOUND"}, status_code=404)

    @app.post("/drill/seq")
    def drill_seq(body: SeqIn):
        try:
            return service.write_seq(body.drill_id, body.seq)
        except WriteRejected as exc:
            return rejected(exc)

    @app.exception_handler(StoreUnavailable)
    async def store_unavailable(_: Request, exc: StoreUnavailable):
        return JSONResponse({"error": "UNAVAILABLE", "region": service.region, "detail": str(exc)}, status_code=503)

    return app
