"""A persistent dual failure must alert once, then only every ``alert_repeat_s`` (no email flood)."""

from dataclasses import replace

from drsystem.control import bootstrap_control
from drsystem.events import EventLog
from drsystem.notify import OutboxNotifier
from drsystem.orchestrator.controller import Controller
from drsystem.orchestrator.probe import ProbeResult

from ..conftest import DR, HOME


class FakeProber:
    def __init__(self, results):
        self.results = results

    def probe(self, region):
        return self.results[region]


def down(region):
    return ProbeResult(region, 0.0, False, None, 400.0, True, error="ReadTimeout")


def unhealthy(region):
    return ProbeResult(region, 0.0, False, 503, 10.0, False, error="503")


def make(settings, stores, prober):
    bootstrap_control(stores[DR], HOME)
    settings = replace(settings, workflow=replace(settings.workflow, alert_repeat_s=30.0))
    launched = []
    notifier = OutboxNotifier()
    c = Controller(settings, stores[DR], prober, launched.append, EventLog("SIMULATED"), notifier)
    return c, notifier, launched


def test_dual_failure_alerts_once_then_repeats_slowly(settings, stores):
    c, notifier, launched = make(settings, stores, FakeProber({HOME: down(HOME), DR: unhealthy(DR)}))
    for i in range(40):  # 20 s of ticks at 0.5 s
        c.tick(now=1000 + i * 0.5)
    assert launched == []  # never fail over to an unhealthy standby
    assert len(notifier.sent) == 1 and "NOT ready" in notifier.sent[0]["message"]
    for i in range(30):  # cross the 30 s repeat interval
        c.tick(now=1020 + i * 0.5)
    assert len(notifier.sent) == 2 and "[still active]" in notifier.sent[1]["subject"]


def test_failover_proceeds_once_standby_recovers(settings, stores):
    prober = FakeProber({HOME: down(HOME), DR: unhealthy(DR)})
    c, notifier, launched = make(settings, stores, prober)
    for i in range(6):
        c.tick(now=1000 + i * 0.5)
    assert len(notifier.sent) == 1
    # Mumbai stays down; Singapore recovers -> controller now fails over instead of alerting.
    prober.results[DR] = ProbeResult(DR, 0.0, True, 200, 10.0, False, ready=True)
    for i in range(3):
        c.tick(now=1003 + i * 0.5)
    assert len(launched) == 1 and launched[0].action == "FAILOVER"
    assert len(notifier.sent) == 1  # no extra alert noise


class Result:
    def __init__(self, outcome):
        self.outcome = outcome


def test_false_alarm_does_not_arm_cooldown(settings, stores):
    bootstrap_control(stores[DR], HOME)
    outcomes = ["false_alarm", "failed_over"]
    decisions = []

    def launcher(decision):
        decisions.append(decision)
        return Result(outcomes[len(decisions) - 1])

    prober = FakeProber({HOME: down(HOME), DR: ProbeResult(DR, 0.0, True, 200, 10.0, False, ready=True)})
    c = Controller(settings, stores[DR], prober, launcher, EventLog("SIMULATED"), OutboxNotifier())
    for i in range(8):
        c.tick(now=1000 + i * 0.5)
    # First launch was a false alarm; the persisting failure re-triggered without waiting for cool-down.
    assert [d.action for d in decisions] == ["FAILOVER", "FAILOVER"]
