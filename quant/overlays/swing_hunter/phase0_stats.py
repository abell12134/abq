"""Phase 0：历史候选池自然 hit 率（零 LLM 成本）。

复现 swing_hunter 候选池主路径：每个信号日 T 取 LGBM score 排名前 N（默认 30），
硬伤过滤（创业板/科创板权限、T+1 开盘不可买），T+1 开盘价入场，统计 10 个交易日内：

  hit     任意收盘 ≥ entry×(1+HIT_PCT)
  stopped 任意收盘 ≤ entry×(1-STOP_PCT) 且先于 hit
  expired 满 HORIZON 日未 hit 也未 stopped

另输出随机对照组（同 universe 随机抽 N）与「全市场当日可交易股」基线。

用法：
  python overlays/swing_hunter/phase0_stats.py
  python overlays/swing_hunter/phase0_stats.py --start 2024-01-02 --end 2026-06-30 --topk 30
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yaml

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))
sys.path.insert(0, str(QUANT / "ops"))

from overlays.swing_hunter.schema import (  # noqa: E402
    HIT_PCT,
    HIT_PCT_TIER2,
    HIT_PCT_TIER3,
    HORIZON_DAYS,
    STOP_PCT,
    SIGNAL_TOP_N,
)

TZ = ZoneInfo("Asia/Shanghai")
LIMIT = 0.095
BOARD_PREFIXES = {
    "chinext": ("SZ300", "SZ301"),
    "star": ("SH688",),
}


def board_excluded(inst: str, boards: list[str]) -> bool:
    return any(inst.startswith(p) for b in boards
               for p in BOARD_PREFIXES.get(str(b).lower(), ()))


def load_signals(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["datetime"])
    df["instrument"] = df["instrument"].astype(str).str.upper()
    df["day"] = df["datetime"].dt.normalize()
    df["rank"] = df.groupby("day")["score"].rank(ascending=False, method="first")
    return df


def next_trading_days(cal: list[pd.Timestamp], day: pd.Timestamp, n: int) -> list[pd.Timestamp]:
    idx = cal.index(day)
    return cal[idx + 1: idx + 1 + n]


def evaluate_one(
    inst: str,
    signal_day: pd.Timestamp,
    entry_open: float,
    closes: pd.Series,
    horizon: int,
) -> dict:
    """closes: index=datetime, value=close from entry day through horizon days."""
    hit_pct, stop_pct = HIT_PCT, STOP_PCT
    result = "expired"
    result_ret = float(closes.iloc[-1]) / entry_open - 1.0 if len(closes) else 0.0
    hit_tier = 0
    days_held = 0
    mfe, mae = None, None

    for i, (dt, close) in enumerate(closes.items()):
        if pd.isna(close):
            continue
        days_held += 1
        ret = float(close) / entry_open - 1.0
        mfe = ret if mfe is None else max(mfe, ret)
        mae = ret if mae is None else min(mae, ret)
        if ret >= HIT_PCT_TIER3:
            hit_tier = max(hit_tier, 3)
        elif ret >= HIT_PCT_TIER2:
            hit_tier = max(hit_tier, 2)
        elif ret >= hit_pct:
            hit_tier = max(hit_tier, 1)
        if ret >= hit_pct:
            result, result_ret = "hit", ret
            break
        if ret <= -stop_pct:
            result, result_ret = "stopped", ret
            break
    if result == "expired" and len(closes):
        result_ret = float(closes.dropna().iloc[-1]) / entry_open - 1.0

    return {
        "instrument": inst,
        "signal_day": signal_day.strftime("%Y-%m-%d"),
        "entry_day": closes.index[0].strftime("%Y-%m-%d") if len(closes) else None,
        "entry_open": round(entry_open, 4),
        "result": result,
        "result_ret": round(result_ret, 4),
        "hit_tier": hit_tier,
        "days_held": days_held,
        "mfe": round(mfe, 4) if mfe is not None else None,
        "mae": round(mae, 4) if mae is not None else None,
    }


def build_price_panel(
    instruments: list[str],
    start: str,
    end: str,
) -> pd.DataFrame:
    from ensure_qlib_data import extract, resolve_provider_uri
    import qlib
    from qlib.data import D

    cfg = yaml.safe_load((QUANT / "configs" / "global.yaml").read_text())
    extract(force=False)
    uri = resolve_provider_uri(cfg["paths"]["qlib_data"])
    qlib.init(provider_uri=uri, region="cn")
    fields = ["$open/$factor", "$close/$factor"]
    panel = D.features(instruments, fields, start_time=start, end_time=end)
    panel.columns = ["open", "close"]
    return panel


def run_stats(
    start: str,
    end: str,
    topk: int,
    exclude_boards: list[str],
    signals_path: Path,
    random_seed: int = 42,
) -> dict:
    sig = load_signals(signals_path)
    days = sorted(sig[(sig["day"] >= pd.Timestamp(start)) & (sig["day"] <= pd.Timestamp(end))]["day"].unique())
    if not days:
        raise RuntimeError(f"信号区间 {start}~{end} 无数据")

    # 候选 instrument 全集
    pool_insts = set()
    day_top: dict[pd.Timestamp, list[str]] = {}
    day_universe: dict[pd.Timestamp, list[str]] = {}
    for d in days:
        sub = sig[sig["day"] == d]
        day_universe[d] = sub["instrument"].tolist()
        top = sub.nsmallest(topk, "rank")["instrument"].tolist()
        day_top[d] = top
        pool_insts.update(top)

    cal_start = (pd.Timestamp(start) - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    cal_end = (pd.Timestamp(end) + pd.Timedelta(days=HORIZON_DAYS + 20)).strftime("%Y-%m-%d")
    panel = build_price_panel(sorted(pool_insts), cal_start, cal_end)

    import qlib
    from qlib.data import D
    cal = [pd.Timestamp(x) for x in D.calendar(freq="day", start_time=start, end_time=cal_end)]

    rng = np.random.default_rng(random_seed)
    rows_lgbm: list[dict] = []
    rows_rand: list[dict] = []
    skip_reasons: dict[str, int] = {}

    for d in days:
        if d not in cal:
            continue
        cal_idx = cal.index(d)
        if cal_idx + 1 >= len(cal):
            continue
        entry_day = cal[cal_idx + 1]
        horizon_days = cal[cal_idx + 1: cal_idx + 1 + HORIZON_DAYS + 1]
        if len(horizon_days) < 2:
            continue

        # 随机对照：同日 universe 随机 topk
        univ = day_universe[d]
        if len(univ) >= topk:
            rand_pick = rng.choice(univ, size=topk, replace=False).tolist()
        else:
            rand_pick = univ

        for pool_name, inst_list in [("lgbm_top", day_top[d]), ("random", rand_pick)]:
            for inst in inst_list:
                if board_excluded(inst, exclude_boards):
                    skip_reasons["board"] = skip_reasons.get("board", 0) + 1
                    continue
                try:
                    sub = panel.xs(inst, level="instrument").sort_index()
                except KeyError:
                    skip_reasons["no_data"] = skip_reasons.get("no_data", 0) + 1
                    continue
                if entry_day not in sub.index:
                    skip_reasons["no_entry_day"] = skip_reasons.get("no_entry_day", 0) + 1
                    continue
                prev_day = cal[cal_idx]
                if prev_day not in sub.index:
                    skip_reasons["no_prev"] = skip_reasons.get("no_prev", 0) + 1
                    continue
                prev_close = sub.loc[prev_day, "close"]
                entry_open = sub.loc[entry_day, "open"]
                if pd.isna(entry_open) or pd.isna(prev_close):
                    skip_reasons["suspended"] = skip_reasons.get("suspended", 0) + 1
                    continue
                if float(entry_open) >= float(prev_close) * (1 + LIMIT):
                    skip_reasons["limit_up"] = skip_reasons.get("limit_up", 0) + 1
                    continue
                close_span = sub.loc[horizon_days[0]: horizon_days[-1], "close"]
                ev = evaluate_one(inst, d, float(entry_open), close_span, HORIZON_DAYS)
                ev["pool"] = pool_name
                if pool_name == "lgbm_top":
                    rows_lgbm.append(ev)
                else:
                    rows_rand.append(ev)

    def summarize(rows: list[dict], label: str) -> dict:
        if not rows:
            return {"label": label, "n": 0}
        df = pd.DataFrame(rows)
        done = df[df["result"].isin(["hit", "stopped", "expired"])]
        hits = done[done["result"] == "hit"]
        stopped = done[done["result"] == "stopped"]
        tier2 = hits[hits["hit_tier"] >= 2]
        tier3 = hits[hits["hit_tier"] >= 3]
        rets = done["result_ret"].astype(float)
        return {
            "label": label,
            "n": len(done),
            "signal_days": len(days),
            "hit": len(hits),
            "hit_rate": round(len(hits) / len(done), 4) if len(done) else None,
            "hit_tier2": len(tier2),
            "hit_tier2_rate": round(len(tier2) / len(done), 4) if len(done) else None,
            "hit_tier3": len(tier3),
            "hit_tier3_rate": round(len(tier3) / len(done), 4) if len(done) else None,
            "stopped": len(stopped),
            "stopped_rate": round(len(stopped) / len(done), 4) if len(done) else None,
            "expired": len(done[done["result"] == "expired"]),
            "avg_return": round(float(rets.mean()), 4) if len(rets) else None,
            "median_return": round(float(rets.median()), 4) if len(rets) else None,
            "avg_mfe": round(float(df["mfe"].dropna().mean()), 4) if df["mfe"].notna().any() else None,
            "avg_days_held": round(float(df["days_held"].mean()), 2) if len(df) else None,
        }

    lgbm_sum = summarize(rows_lgbm, f"LGBM Top{topk}")
    rand_sum = summarize(rows_rand, f"随机 Top{topk}")

    # 按年拆分
    by_year: dict[str, dict] = {}
    if rows_lgbm:
        dfy = pd.DataFrame(rows_lgbm)
        dfy["year"] = dfy["signal_day"].str[:4]
        for yr, g in dfy.groupby("year"):
            by_year[yr] = summarize(g.to_dict("records"), f"LGBM Top{topk} {yr}")

    return {
        "period": f"{start} ~ {end}",
        "topk": topk,
        "exclude_boards": exclude_boards,
        "horizon_days": HORIZON_DAYS,
        "hit_pct": HIT_PCT,
        "stop_pct": STOP_PCT,
        "signals_path": str(signals_path),
        "skip_reasons": skip_reasons,
        "lgbm_top": lgbm_sum,
        "random": rand_sum,
        "by_year": by_year,
        "sample_rows": rows_lgbm[:5],
    }


def write_report(stats: dict, out: Path) -> None:
    l, r = stats["lgbm_top"], stats["random"]
    lines = [
        "# 短线猎手 Phase 0：历史候选池自然 hit 率",
        "",
        f"> 生成时间：{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} · "
        "**零 LLM** · 收盘口径验证",
        "",
        "## 口径",
        "",
        f"- 信号日 T 取 LGBM score **Top{stats['topk']}**（`latest_pred.csv`）",
        f"- 硬伤过滤：板块排除 {stats['exclude_boards'] or '无'}；"
        f"T+1 停牌/一字涨停开盘不买",
        f"- 入场：**T+1 开盘价**（后复权）",
        f"- **hit**：{stats['horizon_days']} 个交易日内任意收盘 ≥ +{stats['hit_pct']*100:.0f}%",
        f"- **stopped**：收盘 ≤ −{stats['stop_pct']*100:.0f}% 且先于 hit",
        f"- 区间：**{stats['period']}**",
        "",
        "## 主结果：LGBM 强势池 vs 随机对照",
        "",
        "| 指标 | LGBM Top" + str(stats["topk"]) + " | 随机 Top" + str(stats["topk"]) + " |",
        "|------|------|------|",
        f"| 可评估笔数 | {l.get('n', 0)} | {r.get('n', 0)} |",
        f"| hit 率（收盘 ≥+10%） | {_pct(l.get('hit_rate'))} ({l.get('hit', 0)} 笔) | {_pct(r.get('hit_rate'))} ({r.get('hit', 0)} 笔) |",
        f"| +15% 档 hit | {_pct(l.get('hit_tier2_rate'))} | {_pct(r.get('hit_tier2_rate'))} |",
        f"| +20% 档 hit | {_pct(l.get('hit_tier3_rate'))} | {_pct(r.get('hit_tier3_rate'))} |",
        f"| 止损率 | {_pct(l.get('stopped_rate'))} | {_pct(r.get('stopped_rate'))} |",
        f"| 到期未达标 | {l.get('expired', '—')} | {r.get('expired', '—')} |",
        f"| 结算均收益 | {_pct(l.get('avg_return'))} | {_pct(r.get('avg_return'))} |",
        f"| 结算中位收益 | {_pct(l.get('median_return'))} | {_pct(r.get('median_return'))} |",
        f"| 平均 MFE（最高收盘） | {_pct(l.get('avg_mfe'))} | {_pct(r.get('avg_mfe'))} |",
        f"| 平均持有计数日 | {l.get('avg_days_held', '—')} | {r.get('avg_days_held', '—')} |",
        "",
        "## 按年拆分（LGBM Top" + str(stats["topk"]) + "）",
        "",
        "| 年份 | 笔数 | hit率 | +15% | 止损率 | 均收益 |",
        "|------|------|-------|------|--------|--------|",
    ]
    for yr, s in sorted(stats.get("by_year", {}).items()):
        lines.append(
            f"| {yr} | {s.get('n', 0)} | {_pct(s.get('hit_rate'))} | "
            f"{_pct(s.get('hit_tier2_rate'))} | {_pct(s.get('stopped_rate'))} | "
            f"{_pct(s.get('avg_return'))} |"
        )
    lines += [
        "",
        "## 过滤跳过统计",
        "",
        "```json",
        __import__("json").dumps(stats.get("skip_reasons", {}), ensure_ascii=False, indent=2),
        "```",
        "",
        "## 解读提示",
        "",
        "- 若 LGBM Top30 hit 率 **显著高于** 随机对照 → 量化强势池有料，值得叠 LLM/催化",
        "- 全市场随机基线约在 **5%~10%**（视行情）；高于此才有研究价值",
        "- 本统计 **不含** 事件催化池、LLM 过滤；真实 swing_hunter 精度需样本外再验",
        "- 样本 ≥60 笔后才开始谈「有效率」；当前仅作池子可行性判断",
        "",
        "---",
        "*研究用途，不构成投资建议。*",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _pct(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:+.2f}%"
    except (TypeError, ValueError):
        return str(v)


def main() -> int:
    p = argparse.ArgumentParser(description="Phase 0 历史候选池自然 hit 率")
    p.add_argument("--start", default="2024-01-02")
    p.add_argument("--end", default="2026-06-30")
    p.add_argument("--topk", type=int, default=SIGNAL_TOP_N)
    p.add_argument("--signals", default=str(QUANT / "data" / "signals" / "latest_pred.csv"))
    p.add_argument("--no-board-filter", action="store_true",
                   help="不过滤创业板/科创板（全板块统计）")
    args = p.parse_args()

    boards = [] if args.no_board_filter else ["chinext", "star"]
    stats = run_stats(
        args.start, args.end, args.topk, boards, Path(args.signals),
    )
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    out = QUANT / "data" / "reports" / f"swing_phase0_{day}.md"
    write_report(stats, out)

    l, r = stats["lgbm_top"], stats["random"]
    print(f"[Phase0] 区间 {stats['period']}")
    print(f"  LGBM Top{args.topk}: n={l.get('n')} hit_rate={l.get('hit_rate')} "
          f"tier2={l.get('hit_tier2_rate')} stopped={l.get('stopped_rate')} "
          f"avg_ret={l.get('avg_return')}")
    print(f"  随机对照: n={r.get('n')} hit_rate={r.get('hit_rate')} avg_ret={r.get('avg_return')}")
    print(f"[OK] 报告 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
