"""Golden tests for settlement caliber v1 — no Qlib required."""

from __future__ import annotations

import sys
from pathlib import Path

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))

from agent.settlement.settle import PriceSeries, settle_direction, settle_interval  # noqa: E402
from agent.core.scorecard import scorecard_for, wilson_interval  # noqa: E402


def _series(opens: dict, closes: dict) -> PriceSeries:
    return PriceSeries(open=opens, close=closes)


def test_direction_hit_up():
    # pred 08-01; entry 08-02 open 10; days 02..11 closes; excess positive
    opens = {f"2026-08-{d:02d}": 10.0 for d in range(2, 20)}
    closes = {f"2026-08-{d:02d}": 10.0 + 0.1 * (d - 2) for d in range(2, 20)}
    stock = _series(opens, closes)
    # flat bench
    bench = _series(opens, {d: 100.0 for d in closes})
    out = settle_direction(
        claim_direction="up",
        pred_date="2026-08-01",
        horizon=10,
        stock=stock,
        bench=bench,
        asof="2026-08-20",
    )
    assert out["ready"] and out["hit"] is True
    assert out["resolve_at"] == "2026-08-11"
    assert out["excess_return"] > 0


def test_direction_miss_and_halt_skip():
    opens = {
        "2026-08-02": 10.0,
        "2026-08-03": 10.0,
        "2026-08-04": 10.0,
        "2026-08-05": 10.0,
        "2026-08-06": 10.0,
        "2026-08-07": 10.0,
        "2026-08-08": 10.0,
        "2026-08-09": 10.0,
        "2026-08-10": 10.0,
        "2026-08-11": 10.0,
        "2026-08-12": 10.0,
    }
    closes = dict(opens)
    closes["2026-08-03"] = float("nan")  # halt — skip
    closes["2026-08-12"] = 9.0  # 10th valid close down
    # rebuild valid closes sequence: 02,04,05,06,07,08,09,10,11,12
    stock = _series(opens, closes)
    bench = _series(opens, {d: 100.0 for d in opens})
    out = settle_direction(
        claim_direction="up",
        pred_date="2026-08-01",
        horizon=10,
        stock=stock,
        bench=bench,
        asof="2026-08-12",
    )
    assert out["ready"]
    assert out["resolve_at"] == "2026-08-12"
    assert out["hit"] is False


def test_interval_pic():
    opens = {f"2026-08-{d:02d}": 10.0 for d in range(2, 15)}
    closes = {f"2026-08-{d:02d}": 10.5 for d in range(2, 15)}
    stock = _series(opens, closes)
    bench = _series(opens, {d: 100.0 for d in closes})
    out = settle_interval(
        low=0.0,
        high=0.10,
        pred_date="2026-08-01",
        horizon=5,
        stock=stock,
        bench=bench,
        asof="2026-08-20",
    )
    assert out["ready"] and out["hit"] is True


def test_waiting_entry():
    stock = _series({}, {})
    bench = _series({}, {})
    out = settle_direction(
        claim_direction="up",
        pred_date="2026-08-01",
        horizon=10,
        stock=stock,
        bench=bench,
        asof="2026-08-02",
    )
    assert not out["ready"] and out["reason"] == "waiting_entry"


def test_wilson_and_scorecard():
    lo, hi = wilson_interval(20, 40)
    assert lo is not None and hi is not None and lo < 0.5 < hi
    rows = [
        {
            "claim_type": "direction",
            "status": "resolved",
            "outcome": {"hit": True},
        }
        for _ in range(20)
    ] + [
        {
            "claim_type": "direction",
            "status": "resolved",
            "outcome": {"hit": False},
        }
        for _ in range(20)
    ]
    sc = scorecard_for(rows)
    assert sc["n"] == 40 and sc["sample_ok"] and sc["hit_rate"] == 0.5


def test_limit_annotation_and_st_early():
    from agent.core.events import EventFlags

    opens = {f"2026-08-{d:02d}": 10.0 for d in range(2, 20)}
    closes = {f"2026-08-{d:02d}": 10.0 for d in range(2, 20)}
    # big up day on resolve candidate → limit-up flag
    closes["2026-08-05"] = 12.0
    stock = _series(opens, closes)
    bench = _series(opens, {d: 100.0 for d in closes})
    flags = EventFlags(
        limit_up={"2026-08-05": True},
        st_event_day="2026-08-05",
    )
    out = settle_direction(
        claim_direction="up",
        pred_date="2026-08-01",
        horizon=10,
        stock=stock,
        bench=bench,
        asof="2026-08-20",
        flags=flags,
        instrument="SZ000001",
    )
    assert out["ready"]
    assert out.get("early_settle_reason") == "st"
    assert out["resolve_at"] == "2026-08-05"
    assert out.get("resolve_limit_up") is True
    assert any("涨停" in n for n in (out.get("event_notes") or []))


def test_feature_archive_roundtrip(tmp_path=None):
    from pathlib import Path
    import tempfile

    from agent.prediction.feature_archive import read_feature_snapshot, write_feature_snapshot

    root = Path(tempfile.mkdtemp())
    meta = write_feature_snapshot(
        pred_id="pred_test",
        day="2026-08-11",
        instrument="SZ000001",
        features={"score": 1.23, "direction": "up"},
        root=root,
    )
    assert meta["snapshot_ref"].startswith("parquet://")
    row = read_feature_snapshot("pred_test", "2026-08-11", root=root)
    assert row and abs(float(row["score"]) - 1.23) < 1e-9


if __name__ == "__main__":
    test_direction_hit_up()
    test_direction_miss_and_halt_skip()
    test_interval_pic()
    test_waiting_entry()
    test_wilson_and_scorecard()
    test_limit_annotation_and_st_early()
    test_feature_archive_roundtrip()
    print("OK all golden settlement tests passed")
