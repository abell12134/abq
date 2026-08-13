"""Tests for lookahead / PIT critic assertions."""

from __future__ import annotations

import sys
from pathlib import Path

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))

from agent.prediction.critic_assert import assert_emit_day, assert_prediction_record  # noqa: E402


def test_reject_future_day(tmp_path: Path | None = None):
    # Without qlib, still reject mismatched filename
    p = QUANT / "data" / "signals" / "2026-08-11.csv"
    res = assert_emit_day("2099-01-01", signal_path=p if p.exists() else None)
    # either missing file or future day error
    assert not res.ok


def test_reject_latest_fallback():
    alt = QUANT / "data" / "signals" / "latest_pred.csv"
    res = assert_emit_day("2026-08-11", signal_path=alt if alt.exists() else None)
    if alt.exists():
        assert not res.ok
        assert any("latest_pred" in e for e in res.errors)


def test_pred_record_pit():
    pred = {
        "pred_date": "2026-08-11",
        "claim": {"direction": "up"},
        "feature_snapshot": {
            "feature_version": "x",
            "pit_timestamp": "2026-08-12T15:00:00+08:00",
            "content_hash": "abc",
            "snapshot_ref": "ref",
        },
    }
    res = assert_prediction_record(pred)
    assert not res.ok
    assert any("PIT" in e for e in res.errors)


def test_pred_record_ok():
    pred = {
        "pred_date": "2026-08-11",
        "claim": {"direction": "up"},
        "feature_snapshot": {
            "feature_version": "x",
            "pit_timestamp": "2026-08-11T15:00:00+08:00",
            "content_hash": "abc",
            "snapshot_ref": "ref",
        },
    }
    res = assert_prediction_record(pred)
    assert res.ok


if __name__ == "__main__":
    test_reject_future_day()
    test_reject_latest_fallback()
    test_pred_record_pit()
    test_pred_record_ok()
    print("OK critic_assert tests")
