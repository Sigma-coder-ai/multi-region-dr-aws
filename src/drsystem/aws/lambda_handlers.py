"""Lambda entry points for the real deployment (Option A).

Environment variables (set by ``scripts/aws_deploy.py``)
  DR_REGION          this function's region, e.g. ap-south-1
  DR_HOME_REGION     home (primary) region
  DR_BUCKETS         JSON {"ap-south-1": "<bucket>", "ap-southeast-1": "<bucket>"}
  DR_ENDPOINTS       JSON {"ap-south-1": "https://<id>.lambda-url.ap-south-1.on.aws/", ...}
  DR_FAULT_PARAM     SSM parameter holding this region's fault flags (app only)
  DR_TOPIC_ARN       SNS topic for alerts (controller only)

The failover workflow runs inline in the controller invocation (timeout 300 s). Its steps are the
same as the planned Step Functions state machine and are recorded durably in the DR bucket.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache


def _env_json(name: str) -> dict:
    return json.loads(os.environ[name])


@lru_cache(maxsize=1)
def _app_handler():
    from mangum import Mangum

    from ..app.api import create_app
    from ..app.peer import HttpPeerView
    from ..app.service import RegionService
    from .faults_ssm import SsmFaults
    from .s3store import S3ObjectStore
    from .sigv4 import LambdaUrlSigV4

    region = os.environ["DR_REGION"]
    buckets, endpoints = _env_json("DR_BUCKETS"), _env_json("DR_ENDPOINTS")
    peer_region = next(r for r in endpoints if r != region)
    service = RegionService(
        region,
        S3ObjectStore(buckets[region], region),
        SsmFaults(os.environ["DR_FAULT_PARAM"], region),
        HttpPeerView(endpoints[peer_region], timeout_s=1.0, cache_s=2.0, auth=LambdaUrlSigV4()),
    )
    return Mangum(create_app(service, endpoints, mode="aws"), lifespan="off")


def _watchdog():
    # Diagnostics: dump all thread stacks to the log if an invocation stalls (DR_DEBUG_HANG_S).
    secs = float(os.environ.get("DR_DEBUG_HANG_S", "0"))
    if secs:
        import faulthandler
        import sys

        faulthandler.dump_traceback_later(secs, exit=False, file=sys.stderr)
    return secs


def app_handler(event, context):
    """Lambda function URL handler for the regional API."""
    armed = _watchdog()
    try:
        return _app_handler()(event, context)
    finally:
        if armed:
            import faulthandler

            faulthandler.cancel_dump_traceback_later()


def build_control_plane(region: str | None = None):
    """Shared wiring for the controller Lambda and the operator's laptop (drills, failback)."""
    from ..events import EventLog
    from ..orchestrator.probe import HttpProber
    from .client import AwsDirectClient
    from .events_sink import S3EventSink
    from .replication import MarkerReplicationMonitor
    from .s3store import S3ObjectStore
    from .settings import aws_settings
    from .sigv4 import LambdaUrlSigV4

    home = os.environ["DR_HOME_REGION"]
    buckets, endpoints = _env_json("DR_BUCKETS"), _env_json("DR_ENDPOINTS")
    settings = aws_settings(home, endpoints)
    dr = region or settings.standby_region
    stores = {r: S3ObjectStore(b, r) for r, b in buckets.items()}
    auth = LambdaUrlSigV4()
    timeline = EventLog("REAL-AWS", sink=S3EventSink(stores[dr]))
    return {
        "settings": settings,
        "stores": stores,
        "control_store": stores[dr],
        "prober": HttpProber(endpoints, settings.probe.timeout_s, auth=auth),
        "replication": MarkerReplicationMonitor(stores),
        "client_factory": lambda: AwsDirectClient(endpoints, home, auth, timeout_s=6.0),
        "timeline": timeline,
    }


def controller_tick_handler(event, context):
    """EventBridge Scheduler target (rate 1 minute) in the DR region."""
    from ..orchestrator.controller import Controller
    from ..orchestrator.workflows import FailoverWorkflow
    from .notify_sns import SnsNotifier

    region = os.environ["DR_REGION"]
    cp = build_control_plane(region)
    notifier = SnsNotifier(os.environ["DR_TOPIC_ARN"], region)

    def launch(decision):
        return FailoverWorkflow(notifier=notifier, **cp).run(decision)

    controller = Controller(
        cp["settings"], cp["control_store"], cp["prober"], launch, cp["timeline"], notifier
    )
    decision = controller.tick()
    return decision.to_dict() if decision else {"action": "SKIPPED"}
