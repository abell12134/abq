"""实盘达标案例断言：候选覆盖 + 事后收益 + 历史裁判复盘（零 LLM）。

用法：
  python overlays/swing_hunter/assert_live_cases.py
  python overlays/swing_hunter/assert_live_cases.py --cases data/overlays/swing_hunter/eval/cases/live_wins.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))

from overlays.swing_hunter.schema import (  # noqa: E402
    HIT_PCT,
    HIT_PCT_TIER2,
    HIT_PCT_TIER3,
    ROOT,
)
from overlays.swing_hunter import candidates as CD  # noqa: E402
from overlays.swing_hunter.pattern_mine import load_patterns  # noqa: E402

DEFAULT_CASES = ROOT / "eval" / "cases" / "live_wins.yaml"


def _tier_of(ret: float) -> int:
    if ret >= HIT_PCT_TIER3:
        return 3
    if ret >= HIT_PCT_TIER2:
        return 2
    if ret >= HIT_PCT:
        return 1
    return 0


def _load_cases(path: Path) -> list[dict[str, Any]]:
    import yaml
    data = yaml.safe_load(path.read_text()) or {}
    return list(data.get("cases") or [])


def _hist_judge_action(day: str, instrument: str, pass_label: str) -> str | None:
    """从已有 eval traces 读裁判 action（若存在）。"""
    traces = ROOT / "eval" / day / "traces"
    if not traces.exists():
        return None
    # 常见命名：SH600601_pass1_local.json
    for p in traces.glob(f"{instrument}_*.json"):
        if pass_label.replace("pass1_local", "pass1") not in p.stem and pass_label not in p.stem:
            # 宽松：pass1 匹配 pass1_local
            if "pass1" in pass_label and "pass1" not in p.stem:
                continue
            if "pass2" in pass_label and "pass2" not in p.stem:
                continue
        try:
            d = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for step in d.get("steps") or []:
            if step.get("role") != "judge_raw":
                continue
            try:
                j = json.loads(step["content"])
                return str(j.get("action") or "")
            except (json.JSONDecodeError, TypeError):
                continue
        pred = d.get("prediction") or {}
        if pred.get("action"):
            return str(pred["action"])
    return None


def assert_case(case: dict[str, Any], account: str) -> dict[str, Any]:
    inst = str(case["instrument"]).upper()
    signal_day = str(case["signal_day"])
    ret = float(case.get("result_return") or 0.0)
    expected_tier = int(case.get("expected_tier") or 0)
    actual_tier = _tier_of(ret)

    built = CD.build_candidates(signal_day, account=account)
    in_pool = any(c["instrument"] == inst for c in built["candidates"])
    cand = next((c for c in built["candidates"] if c["instrument"] == inst), None)

    patterns = load_patterns(limit=0, statuses={"live_case", "live"})
    pid = case.get("pattern_id")
    in_patterns = any(p.get("id") == pid for p in patterns) if pid else False

    hist = case.get("historical_eval") or {}
    observed = None
    if hist.get("day"):
        observed = _hist_judge_action(
            str(hist["day"]), inst, str(hist.get("pass") or "pass1_local"))

    checks = {
        "in_candidate_pool": in_pool,
        "tier_ok": actual_tier >= expected_tier,
        "pattern_present": (in_patterns if pid else True),
    }
    ok = all(checks.values())
    return {
        "id": case.get("id"),
        "instrument": inst,
        "name": case.get("name"),
        "signal_day": signal_day,
        "ok": ok,
        "checks": checks,
        "result_return": ret,
        "actual_tier": actual_tier,
        "expected_tier": expected_tier,
        "rule_score": cand.get("rule_score") if cand else None,
        "from_signal": cand.get("from_signal") if cand else None,
        "from_momentum": cand.get("from_momentum") if cand else None,
        "pattern_hits": cand.get("pattern_hits") if cand else None,
        "historical_action": observed,
        "historical_expected": hist.get("observed_action"),
        "historical_note": hist.get("note"),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="短线猎手实盘案例断言（零 LLM）")
    p.add_argument("--cases", default=str(DEFAULT_CASES))
    p.add_argument("--account", default="live_manual_10k")
    args = p.parse_args()

    path = Path(args.cases)
    if not path.exists():
        print(f"[FAIL] 案例文件不存在: {path}")
        return 1
    cases = _load_cases(path)
    if not cases:
        print("[FAIL] 无案例")
        return 1

    rows = [assert_case(c, args.account) for c in cases]
    n_ok = sum(1 for r in rows if r["ok"])
    print(f"[assert_live_cases] {n_ok}/{len(rows)} 通过")
    for r in rows:
        mark = "OK" if r["ok"] else "FAIL"
        print(f"  [{mark}] {r['id']} {r['instrument']} {r.get('name') or ''} "
              f"pool={r['checks']['in_candidate_pool']} "
              f"tier={r['actual_tier']}>={r['expected_tier']} "
              f"pattern={r['checks']['pattern_present']} "
              f"rule={r.get('rule_score')} "
              f"hist_action={r.get('historical_action')}")
        if r.get("historical_note") and r.get("historical_action"):
            print(f"         复盘: 历史裁判={r['historical_action']} "
                  f"(期望记录={r.get('historical_expected')}) — {r['historical_note']}")

    out = ROOT / "eval" / "cases" / "assert_report.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] 报告 → {out}")
    return 0 if n_ok == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
