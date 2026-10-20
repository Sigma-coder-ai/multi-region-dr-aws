"""SIMULATED edge router that mimics CloudFront origin-group failover.

Behaviour copied from CloudFront:
- ``/api/*`` GET/HEAD/OPTIONS go to an origin group: always try the PRIMARY origin first; on a
  connection error, timeout, or HTTP 500/502/503/504, retry the same request on the SECONDARY.
- Other methods on ``/api/*`` are NOT failed over (CloudFront origin groups only fail over
  GET/HEAD/OPTIONS); writes reach the primary origin only.
- ``/r/<region>/*`` path behaviours route to one specific regional origin (used for writes by the
  client, which picks the active region itself: client-side write failover).
- ``/`` serves the static DR Live Monitor page (S3 origin on AWS).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

FAILOVER_STATUSES = {500, 502, 503, 504}
FAILOVER_METHODS = {"GET", "HEAD", "OPTIONS"}
HOP_HEADERS = {"content-length", "transfer-encoding", "connection", "content-encoding"}


def create_router_app(
    origins: dict[str, str],
    primary: str,
    origin_timeout_s: float = 0.5,
    regional_timeout_s: float = 2.0,
    web_dir: Path | None = None,
    extra: Callable[[FastAPI], None] | None = None,
) -> FastAPI:
    if primary not in origins or len(origins) != 2:
        raise ValueError("router needs exactly two origins including the primary")
    secondary = next(r for r in origins if r != primary)
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.client = httpx.AsyncClient(timeout=origin_timeout_s)
        app.state.regional_client = httpx.AsyncClient(timeout=regional_timeout_s)
        yield
        await app.state.client.aclose()
        await app.state.regional_client.aclose()

    app = FastAPI(title="SIMULATED CloudFront router", lifespan=lifespan)
    app.state.stats = {"requests": 0, "failovers": 0}

    async def forward(region: str, request: Request, path: str, regional: bool = False) -> Response:
        url = origins[region].rstrip("/") + "/" + path
        body = await request.body()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS | {"host"}}
        client = app.state.regional_client if regional else app.state.client
        resp = await client.request(request.method, url, content=body, headers=headers, params=request.query_params)
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_HEADERS}
        out_headers["x-origin"] = region
        return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)

    @app.api_route("/api/{path:path}", methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"])
    async def api(path: str, request: Request):
        app.state.stats["requests"] += 1
        if request.method not in FAILOVER_METHODS:
            try:
                return await forward(primary, request, path)
            except httpx.HTTPError:
                return JSONResponse({"error": "ORIGIN_UNREACHABLE", "origin": primary}, status_code=504)
        try:
            resp = await forward(primary, request, path)
            if resp.status_code not in FAILOVER_STATUSES:
                return resp
        except httpx.HTTPError:
            pass
        app.state.stats["failovers"] += 1
        try:
            resp = await forward(secondary, request, path)
            resp.headers["x-origin-failover"] = "1"
            return resp
        except httpx.HTTPError:
            return JSONResponse({"error": "ALL_ORIGINS_UNREACHABLE"}, status_code=504)

    @app.api_route("/r/{region}/{path:path}", methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"])
    async def regional(region: str, path: str, request: Request):
        if region not in origins:
            return JSONResponse({"error": "UNKNOWN_REGION"}, status_code=404)
        try:
            return await forward(region, request, path, regional=True)
        except httpx.HTTPError:
            return JSONResponse({"error": "ORIGIN_UNREACHABLE", "origin": region}, status_code=504)

    if extra:
        extra(app)

    if web_dir is not None:

        @app.get("/")
        async def index():
            return FileResponse(web_dir / "index.html")

        @app.get("/static/{name}")
        async def static(name: str):
            p = (web_dir / name).resolve()
            if p.parent != web_dir.resolve() or not p.is_file():
                return JSONResponse({"error": "NOT_FOUND"}, status_code=404)
            return FileResponse(p)

    return app
