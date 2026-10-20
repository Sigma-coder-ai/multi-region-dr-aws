"""Typed settings loaded from a TOML profile (see ``config/*.toml``)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = PROJECT_ROOT / "config" / "local-sim.toml"


@dataclass(frozen=True)
class RegionConfig:
    name: str
    label: str
    port: int = 0
    endpoint: str = ""  # base URL of the region's app (filled in for local sim; Lambda URL on AWS)


@dataclass(frozen=True)
class RouterConfig:
    port: int = 8100
    host: str = "127.0.0.1"  # interface the edge binds to; 0.0.0.0 when hosted (e.g. Railway)
    origin_timeout_s: float = 0.5  # origin-group attempt timeout before failing over (reads)
    regional_timeout_s: float = 2.0  # single-origin path behaviours (writes)


@dataclass(frozen=True)
class ReplicationConfig:
    lag_s: float = 0.3
    jitter_s: float = 0.1


@dataclass(frozen=True)
class ProbeConfig:
    interval_s: float = 0.5
    timeout_s: float = 0.4
    samples_per_tick: int = 1  # >1 = multi-sample probing (error-rate estimate within one tick)
    sample_gap_s: float = 0.2


@dataclass(frozen=True)
class ScorerConfig:
    window: int = 6
    failover_threshold: float = 40
    consecutive_bad: int = 3
    fast_path_hard_failures: int = 3
    latency_good_ms: float = 300
    latency_bad_ms: float = 2000


@dataclass(frozen=True)
class WorkflowConfig:
    drain_max_s: float = 2.0
    replication_ok_pending: int = 0
    cooldown_s: float = 10.0
    verify_timeout_s: float = 15.0
    failback_min_healthy_s: float = 3.0
    lock_ttl_s: float = 60.0
    alert_repeat_s: float = 300.0  # re-send an unchanged alert at most this often


@dataclass(frozen=True)
class AppConfig:
    peer_fencing: bool = True
    peer_timeout_s: float = 0.3
    peer_cache_s: float = 1.0


@dataclass(frozen=True)
class DrillConfig:
    write_interval_s: float = 0.05
    read_interval_s: float = 0.1
    write_deadline_s: float = 30.0
    settle_s: float = 1.5


@dataclass(frozen=True)
class Settings:
    mode: str = "local-sim"
    home_region: str = "ap-south-1"
    regions: tuple[RegionConfig, ...] = ()
    router: RouterConfig = field(default_factory=RouterConfig)
    replication: ReplicationConfig = field(default_factory=ReplicationConfig)
    probe: ProbeConfig = field(default_factory=ProbeConfig)
    scorer: ScorerConfig = field(default_factory=ScorerConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    app: AppConfig = field(default_factory=AppConfig)
    drill: DrillConfig = field(default_factory=DrillConfig)

    @property
    def is_simulated(self) -> bool:
        return self.mode != "aws"

    @property
    def evidence_label(self) -> str:
        return "SIMULATED" if self.is_simulated else "REAL-AWS"

    def region(self, name: str) -> RegionConfig:
        for r in self.regions:
            if r.name == name:
                return r
        raise KeyError(f"unknown region {name!r}")

    def peer_of(self, name: str) -> str:
        others = [r.name for r in self.regions if r.name != name]
        if len(others) != 1:
            raise ValueError("exactly two regions are supported")
        return others[0]

    @property
    def standby_region(self) -> str:
        return self.peer_of(self.home_region)

    def with_endpoints(self, endpoints: dict[str, str]) -> "Settings":
        regions = tuple(replace(r, endpoint=endpoints.get(r.name, r.endpoint)) for r in self.regions)
        return replace(self, regions=regions)


def _build(cls: type, data: dict[str, Any] | None):
    data = data or {}
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown keys for {cls.__name__}: {sorted(unknown)}")
    return cls(**data)


def load_settings(path: str | Path | None = None, **overrides: Any) -> Settings:
    path = Path(path) if path else DEFAULT_PROFILE
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    regions = tuple(_build(RegionConfig, r) for r in raw.pop("regions", []))
    settings = Settings(
        mode=raw.pop("mode", "local-sim"),
        home_region=raw.pop("home_region", "ap-south-1"),
        regions=regions,
        router=_build(RouterConfig, raw.pop("router", None)),
        replication=_build(ReplicationConfig, raw.pop("replication", None)),
        probe=_build(ProbeConfig, raw.pop("probe", None)),
        scorer=_build(ScorerConfig, raw.pop("scorer", None)),
        workflow=_build(WorkflowConfig, raw.pop("workflow", None)),
        app=_build(AppConfig, raw.pop("app", None)),
        drill=_build(DrillConfig, raw.pop("drill", None)),
    )
    if raw:
        raise ValueError(f"unknown top-level keys: {sorted(raw)}")
    if len(settings.regions) != 2:
        raise ValueError("exactly two regions are required")
    settings.region(settings.home_region)  # validates
    return replace(settings, **overrides) if overrides else settings
