"""Unit tests for ta_veto schema / policy (no LLM, no qlib)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))

from overlays.ta_veto.schema import (  # noqa: E402
    VetoDecision,
    VetoFile,
    apply_veto_policy,
    load_vetoed_instruments,
    write_veto_file,
)


def test_apply_veto_policy_threshold_and_tags(tmp_path):
    cands = ["SH600000", "SH600001", "SZ000001"]
    decisions = [
        VetoDecision("SH600000", "veto", 0.9, ["财务恶化"], ["净利骤降"]),
        VetoDecision("SH600001", "veto", 0.95, ["随便说说"], ["无有效标签应忽略"]),
        VetoDecision("SZ000001", "veto", 0.5, ["停牌风险"], ["置信度不足"]),
    ]
    got = apply_veto_policy(decisions, candidates=cands, confidence_threshold=0.7, max_vetoes=2)
    assert got == ["SH600000"]


def test_daily_cap_keeps_one_buyable():
    cands = ["SH600000", "SH600001"]
    decisions = [
        VetoDecision("SH600000", "veto", 0.99, ["造假嫌疑"], ["a"]),
        VetoDecision("SH600001", "veto", 0.98, ["重大诉讼"], ["b"]),
    ]
    # max_vetoes=5 but len(cands)-1=1
    got = apply_veto_policy(decisions, candidates=cands, confidence_threshold=0.7, max_vetoes=5)
    assert len(got) == 1
    assert got[0] == "SH600000"


def test_fail_open_load(tmp_path):
    vf = VetoFile(
        date="2026-07-16",
        status="fail_open",
        fail_reason="no key",
        candidates=["SH600000"],
        decisions=[VetoDecision("SH600000", "veto", 0.99, ["财务恶化"], ["x"])],
        vetoed=["SH600000"],
    )
    write_veto_file(vf, base=tmp_path)
    assert load_vetoed_instruments("2026-07-16", base=tmp_path) == set()


def test_ok_load_reapplies_policy(tmp_path):
    vf = VetoFile(
        date="2026-07-16",
        status="ok",
        candidates=["SH600000", "SH600001"],
        decisions=[
            VetoDecision("SH600000", "veto", 0.9, ["停牌风险"], ["疑似停牌"]),
            VetoDecision("SH600001", "pass", 0.8, [], ["ok"]),
        ],
        vetoed=["SH600000", "SH600001"],  # 手改脏数据应被策略纠正
        confidence_threshold=0.7,
        max_vetoes=1,
    )
    write_veto_file(vf, base=tmp_path)
    assert load_vetoed_instruments("2026-07-16", base=tmp_path) == {"SH600000"}
    raw = json.loads((tmp_path / "2026-07-16.json").read_text())
    assert raw["status"] == "ok"


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        test_apply_veto_policy_threshold_and_tags(p)
        test_daily_cap_keeps_one_buyable()
        test_fail_open_load(p)
        test_ok_load_reapplies_policy(p)
    print("ALL SCHEMA TESTS OK")
