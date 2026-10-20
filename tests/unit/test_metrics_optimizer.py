from drsystem.drills.metrics import ReadSample, WriteRecord, compute, longest_failure_window, summarize
from drsystem.drills.optimizer import Candidate, Overheads, estimate, optimize

from ..conftest import DR, HOME


def w(seq, acked, region, ok=True):
    return WriteRecord(seq, acked - 0.01, acked if ok else None, region if ok else None, ok, 1)


def test_rto_rpo_computation():
    t0 = 100.0
    writes = [w(0, 99.0, HOME), w(1, 99.5, HOME), w(2, 99.9, HOME), w(3, 106.0, DR)]
    reads = [
        ReadSample(99.0, True, HOME, 10),
        ReadSample(100.2, False, None, 500),
        ReadSample(100.8, False, None, 500),
        ReadSample(101.5, True, DR, 600, True),
    ]
    present_in_target = {0}  # seq 1 and 2 were acked by HOME but not yet replicated
    m = compute(t0, 100.5, 102.0, 104.0, HOME, DR, writes, reads, present_in_target)
    assert m.detection_s == 0.5 and m.decision_s == 1.5 and m.promotion_s == 2.0
    assert m.read_rto_s == 1.5 and m.read_outage_s == 1.3
    assert m.write_rto_s == 6.0
    assert m.lost_writes == 2 and m.rpo_s == 0.5  # t0 (100.0) - earliest lost ack (99.5)


def test_no_loss_means_zero_rpo():
    m = compute(100, 101, 102, 103, HOME, DR, [w(0, 99, HOME)], [], {0})
    assert m.lost_writes == 0 and m.rpo_s == 0.0


def test_failure_window_open_at_end():
    reads = [ReadSample(1, True, HOME, 1), ReadSample(2, False, None, 1), ReadSample(5, False, None, 1)]
    assert longest_failure_window(reads, since=0) == 3.0


def test_summarize():
    s = summarize([3, 1, 2, None])
    assert s["n"] == 3 and s["median"] == 2 and s["min"] == 1 and s["max"] == 3


def test_optimizer_respects_targets_and_free_tier():
    results = optimize(target_rto_s=300, target_rpo_s=60, replication_lag_s=15)
    best = next(e for e in results if e.meets_targets)
    assert best.write_rto_s <= 300 and best.monthly_cost_usd == 0.0
    assert best.candidate.probe_interval_s == 60  # 1-minute schedule is enough and stays free


def test_optimizer_sub_minute_probing_costs_more_lambda():
    ov = Overheads()
    slow = estimate(Candidate(60, 3, 0, 10, 3), ov, 15, 300, 60)
    fast = estimate(Candidate(10, 3, 0, 10, 3), ov, 15, 300, 60)
    assert fast.lambda_gb_s > slow.lambda_gb_s and fast.write_rto_s < slow.write_rto_s


def test_hard_outage_rpo_is_replication_lag_regardless_of_drain():
    ov = Overheads()
    e = estimate(Candidate(60, 3, 30, 10, 3), ov, 15, 300, 60)
    assert e.worst_case_rpo_s == 15 and e.gray_failure_rpo_s == 0
