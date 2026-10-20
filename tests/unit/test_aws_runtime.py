"""Offline tests for the real-deployment runtime pieces (signing, direct client, heartbeat, event sink)."""

import boto3
import httpx

from drsystem.aws.client import AwsDirectClient
from drsystem.aws.events_sink import S3EventSink, read_events
from drsystem.aws.replication import MarkerReplicationMonitor
from drsystem.aws.sigv4 import LambdaUrlSigV4, region_from_url
from drsystem.events import EventLog

from ..conftest import DR, HOME

U = {HOME: "https://aaa.lambda-url.ap-south-1.on.aws/", DR: "https://bbb.lambda-url.ap-southeast-1.on.aws/"}


def test_region_from_function_url():
    assert region_from_url(U[DR] + "health") == DR


def test_sigv4_adds_authorization_header():
    session = boto3.Session(aws_access_key_id="AKIDEXAMPLE", aws_secret_access_key="secret", region_name=HOME)
    seen = {}

    def handler(request: httpx.Request):
        seen.update(request.headers)
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler), auth=LambdaUrlSigV4(session))
    client.post(U[HOME] + "drill/seq", json={"drill_id": "d", "seq": 1})
    assert seen["authorization"].startswith("AWS4-HMAC-SHA256") and "ap-south-1/lambda/aws4_request" in seen["authorization"]


def test_direct_client_reads_fail_over_like_cloudfront():
    def handler(request: httpx.Request):
        if "aaa" in request.url.host:
            return httpx.Response(503, json={"error": "DEMOTED"})
        return httpx.Response(200, json={"region": DR})

    c = AwsDirectClient(U, HOME, auth=None)
    c.http = httpx.Client(transport=httpx.MockTransport(handler))
    assert c.get("whoami").json()["region"] == DR
    assert c._write_url(DR, "notes") == U[DR] + "notes"


def test_heartbeat_requires_a_fresh_arrival(stores):
    mon = MarkerReplicationMonitor(stores)
    assert mon.stats(HOME).pending == 1  # just sent, unknown yet
    assert mon.stats(HOME).pending == 1  # not replicated
    stores[DR].put_replica(stores[HOME].get("replication-heartbeat/ap-south-1.json"))
    assert mon.stats(HOME).pending == 0


def test_stale_heartbeat_is_not_mistaken_for_a_new_one(stores):
    mon = MarkerReplicationMonitor(stores)
    mon.stats(HOME)
    stores[DR].put_replica(stores[HOME].get("replication-heartbeat/ap-south-1.json"))
    assert mon.stats(HOME).pending == 0
    assert mon.stats(HOME).pending == 1  # new heartbeat sent; the old replica must not count
    assert mon.stats(HOME).pending == 1


def test_event_sink_round_trip(stores):
    log = EventLog("REAL-AWS", sink=S3EventSink(stores[DR]))
    t0 = log.record("score", score=1)["t"]
    log.record("decision", action="FAILOVER")
    log.record("promoted", region=DR)
    kinds = [e["kind"] for e in read_events(stores[DR], since=t0)]
    assert kinds == ["decision", "promoted"]  # per-tick scores are not persisted
