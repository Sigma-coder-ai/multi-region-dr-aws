"""Real-AWS timings (not scaled). The controller ticks once a minute (EventBridge Scheduler minimum)."""

from __future__ import annotations

from ..config import (
    AppConfig,
    ProbeConfig,
    RegionConfig,
    ScorerConfig,
    Settings,
    WorkflowConfig,
)

LABELS = {"ap-south-1": "Mumbai", "ap-southeast-1": "Singapore"}


def aws_settings(home: str, endpoints: dict[str, str]) -> Settings:
    return Settings(
        mode="aws",
        home_region=home,
        regions=tuple(RegionConfig(r, LABELS.get(r, r), endpoint=u) for r, u in endpoints.items()),
        # interval_s only spaces re-probes inside a workflow; ticks come from the 1-minute schedule.
        # 5 samples per region per tick: estimates the error rate within one tick (gray failures),
        # window = the last 2 ticks of samples. Blips shorter than 3 ticks still cannot trigger failover.
        probe=ProbeConfig(interval_s=4.0, timeout_s=3.0, samples_per_tick=5, sample_gap_s=0.3),
        scorer=ScorerConfig(
            window=10, failover_threshold=40, consecutive_bad=3, fast_path_hard_failures=2,
            latency_good_ms=800, latency_bad_ms=3000,
        ),
        workflow=WorkflowConfig(
            drain_max_s=30.0, replication_ok_pending=0, cooldown_s=300.0, verify_timeout_s=120.0,
            failback_min_healthy_s=120.0, lock_ttl_s=600.0, alert_repeat_s=1800.0,
        ),
        app=AppConfig(peer_fencing=True, peer_timeout_s=1.0, peer_cache_s=2.0),
    )
