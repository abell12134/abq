"""口径纯函数测试（无 qlib / 无 LLM）。"""

from __future__ import annotations

import sys
from pathlib import Path

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))

from overlays.sector_forecast.schema import (  # noqa: E402
    TOP_K,
    align_stock_sector,
    evidence_lines,
    industry_bias,
    pick_claims,
    shadow_gate,
    stock_bias,
    window_days,
    window_end,
)
import pandas as pd  # noqa: E402
from overlays.sector_forecast.features import _fwd_sum  # noqa: E402


def test_window_from_t_plus_one():
    cal = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
    assert window_days(cal, "2026-09-01", 2) == ["2026-09-02", "2026-09-03"]
    assert window_end(cal, "2026-09-01", 2) == "2026-09-03"
    assert window_end(cal, "2026-09-03", 2) is None


def test_pick_claims_no_overlap_and_topk():
    rows = [
        {"industry": f"I{i}", "n_stocks": 5, "p_beat": 0.9 - i * 0.05, "expected_excess": 0.01}
        for i in range(10)
    ]
    got = pick_claims(rows, top_k=TOP_K)
    ups = [x["industry"] for x in got if x["action"] == "up"]
    av = [x["industry"] for x in got if x["action"] == "avoid"]
    assert ups == ["I0", "I1", "I2"]
    assert av == ["I9", "I8", "I7"]
    assert set(ups).isdisjoint(av)


def test_skip_thin_and_unknown():
    rows = [
        {"industry": "未知", "n_stocks": 10, "p_beat": 0.99, "expected_excess": 0.2},
        {"industry": "电子", "n_stocks": 2, "p_beat": 0.98, "expected_excess": 0.2},
        {"industry": "银行", "n_stocks": 8, "p_beat": 0.7, "expected_excess": 0.01},
        {"industry": "医药", "n_stocks": 8, "p_beat": 0.2, "expected_excess": -0.01},
    ]
    got = pick_claims(rows, top_k=3)
    inds = {x["industry"] for x in got}
    assert "未知" not in inds
    assert "电子" not in inds
    assert "银行" in inds and "医药" in inds


def test_shadow_gate():
    sh, _ = shadow_gate(model_hit=0.55, chase_hit=0.58, oos_days=80)
    assert sh is True
    sh, _ = shadow_gate(model_hit=0.62, chase_hit=0.55, oos_days=80)
    assert sh is False
    sh, _ = shadow_gate(model_hit=0.90, chase_hit=0.10, oos_days=10)
    assert sh is True


def test_evidence_mentions_numbers():
    lines = evidence_lines({
        "ret_5d": 0.03, "ret_20d": -0.04, "accel": 0.04,
        "rs_10d": 0.02, "pct_up": 0.6, "limit_up_share": 0.05, "n_stocks": 12,
    })
    assert lines
    assert any("早期切换" in x or "5日" in x for x in lines)


def test_align():
    assert stock_bias({"state": "holding"}, {"merged_direction": "up"}) == "bull"
    assert industry_bias({"action": "up"}, {"action": "neutral"}) == "up"
    assert "共振" in align_stock_sector("bull", "up")
    assert "逆势" in align_stock_sector("bull", "avoid")
    assert "板块有戏" in align_stock_sector("mid", "up")


def test_fwd_sum_is_t_plus_one_window():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
    got = _fwd_sum(s, 2)
    assert got.iloc[0] == 5.0   # 2+3
    assert got.iloc[1] == 7.0   # 3+4
    assert pd.isna(got.iloc[-1])


def test_merge_brief_cannot_change_rank():
    from overlays.sector_forecast.brief import merge_brief
    claims = [{
        "industry": "C39电子", "horizon_days": 10, "action": "up", "rank": 1,
        "p_beat": 0.66, "expected_excess": 0.01, "shadow": False,
    }]
    by_ind = {"C39电子": {"h10": {"action": "up", "p_beat": 0.66, "rank": 1}}}
    merge_brief(claims, by_ind, {
        "headline": "测试",
        "items": [{
            "industry": "C39电子", "horizon_days": 10,
            "stance": "challenge", "thesis": "消息面偏空",
            "risks": ["拥挤"],
            "p_beat": 0.99, "action": "avoid", "rank": 9,
        }],
    })
    c = claims[0]
    assert c["p_beat"] == 0.66 and c["action"] == "up" and c["rank"] == 1
    assert c["llm"]["stance"] == "challenge"
    assert c["llm"]["thesis"] == "消息面偏空"
    assert by_ind["C39电子"]["h10"]["p_beat"] == 0.66
    assert by_ind["C39电子"]["h10"]["llm"]["stance"] == "challenge"


def test_merge_brief_matches_display_name_and_horizon_text():
    from overlays.sector_forecast.brief import merge_brief
    claims = [{
        "industry": "C39计算机、通信和其他电子设备制造业",
        "horizon_days": 10, "action": "up", "rank": 1, "p_beat": 0.6,
    }]
    by_ind = {"C39计算机、通信和其他电子设备制造业": {"h10": {"p_beat": 0.6}}}
    out = merge_brief(claims, by_ind, {
        "headline": "电子偏强",
        "items": [{
            "industry": "计算机、通信和其他电子设备制造业",
            "horizon_days": "10日",
            "stance": "质疑",
            "thesis": "拥挤",
            "risks": ["回调"],
        }],
    })
    assert out["n_attached"] == 1
    assert claims[0]["p_beat"] == 0.6
    assert claims[0]["llm"]["stance"] == "challenge"


def test_parse_prefers_items_over_sentiment_example():
    from overlays.llm_json import parse_json_object
    text = (
        'thinking {"sentiment": "bull", "score": 1, "headline": "假"}\n'
        '{"headline": "真", "items": [{"industry": "C39电子",'
        ' "horizon_days": 10, "stance": "agree", "thesis": "ok", "risks": []}]}'
    )
    obj = parse_json_object(text, prefer_keys=frozenset({"items", "headline"}))
    assert obj and obj.get("headline") == "真" and obj.get("items")


if __name__ == "__main__":
    test_window_from_t_plus_one()
    test_pick_claims_no_overlap_and_topk()
    test_skip_thin_and_unknown()
    test_shadow_gate()
    test_evidence_mentions_numbers()
    test_align()
    test_fwd_sum_is_t_plus_one_window()
    test_merge_brief_cannot_change_rank()
    test_merge_brief_matches_display_name_and_horizon_text()
    test_parse_prefers_items_over_sentiment_example()
    print("ok")
