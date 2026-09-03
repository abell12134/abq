"""盘后全量快照编排：pool → limit_stats → cycle/ladder/strong → daily/<day>.json。

用法：
  python overlays/market_board/run_board.py                 # 最新交易日
  python overlays/market_board/run_board.py --day 2026-08-31
  python overlays/market_board/run_board.py --force         # 有快照也重跑

接入：ops/run_daily.py evening 末端调用（fail-open：任何子模块失败记入 errors，
不阻断其他模块，退出码恒 0；仅池子/行情级失败才非零退出）。
纯展示层：不改 orders/，不写信号。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))
sys.path.insert(0, str(QUANT / "ops"))

import common as C  # noqa: E402

from overlays.market_board import cycle, ladder, limit_stats, pool as pool_mod  # noqa: E402
from overlays.market_board import fundflow, news, rotation, store, strong, themes, unlock  # noqa: E402


def _index_env_series(day: str) -> list[dict]:
    """sector_pulse 兼容用的指数行（name/chg_5d/chg_20d）。"""
    out = []
    try:
        C.reset_qlib()
        from qlib.data import D
        cal = [str(d)[:10] for d in C.calendar()]
        idx = cal.index(day)
        start = cal[max(0, idx - 30)]
        for inst, name in (("SH000300", "沪深300"), ("SH000905", "中证500"),
                           ("SH000001", "上证指数"), ("SZ399006", "创业板指")):
            df = D.features([inst], ["$close"], start_time=start, end_time=day)
            if df is None or df.empty:
                continue
            closes = df.droplevel("instrument").iloc[:, 0].dropna().astype(float)
            if len(closes) < 21:
                continue
            last = float(closes.iloc[-1])
            out.append({
                "instrument": inst, "name": name, "close": round(last, 2),
                "chg_1d": round((last / float(closes.iloc[-2]) - 1) * 100, 2),
                "chg_5d": round((last / float(closes.iloc[-6]) - 1) * 100, 2),
                "chg_20d": round((last / float(closes.iloc[-21]) - 1) * 100, 2),
            })
    except Exception:  # noqa: BLE001
        pass
    return out


def _index_env(day: str) -> dict:
    """指数环境定位：benchmark 相对 20 日线 / 20 日涨跌 / 量能变化（本地 qlib）。"""
    out = {"available": False}
    try:
        C.reset_qlib()
        from qlib.data import D
        bench = C.CFG["universe"].get("benchmark", "SH000905")
        cal = [str(d)[:10] for d in C.calendar()]
        if day not in cal:
            return out
        idx = cal.index(day)
        start = cal[max(0, idx - 45)]
        df = D.features([bench], ["$close", "$volume"], start_time=start, end_time=day)
        if df is None or df.empty:
            return out
        df = df.droplevel("instrument").dropna()
        if len(df) < 21:
            return out
        closes = df.iloc[:, 0].astype(float)
        vols = df.iloc[:, 1].astype(float)
        last = float(closes.iloc[-1])
        ma20 = float(closes.iloc[-20:].mean())
        ret20 = last / float(closes.iloc[-21]) - 1
        v5 = float(vols.iloc[-5:].mean())
        v20 = float(vols.iloc[-20:].mean())
        vol_ratio = v5 / v20 if v20 > 0 else 1.0
        if last > ma20 * 1.02:
            trend = "上升"
        elif last < ma20 * 0.98:
            trend = "下降"
        else:
            trend = "震荡"
        score = 50 + (30 if last > ma20 else -30) + min(max(ret20 * 100 * 2, -20), 20)
        score += 10 if vol_ratio > 1.1 else (-10 if vol_ratio < 0.9 else 0)
        score = max(0, min(100, score))
        out = {
            "available": True,
            "benchmark": bench,
            "close": round(last, 2),
            "ma20": round(ma20, 2),
            "above_ma20": last > ma20,
            "ret20": round(ret20, 4),
            "vol_ratio": round(vol_ratio, 2),
            "trend": trend,
            "env_score": round(score, 1),
            "env_label": "适宜进攻" if score >= 65 else ("谨慎" if score >= 45 else "防御"),
        }
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
    return out


def run(day: str | None = None, force: bool = False, lookback: int = limit_stats.LOOKBACK_DAYS) -> int:
    day = day or C.latest_trading_day()
    if not force:
        old = store.load_daily(day)
        if old and old.get("status") == "ok":
            print(f"[OK] {day} 快照已存在（--force 覆盖）")
            return 0

    errors: list[str] = []
    pool = pool_mod.load_pool(day)
    if not pool:
        print("[FATAL] 池子为空")
        return 1
    print(f"[1/5] 池子 {len(pool)} 只")

    print("[2/5] 涨停/连板统计")
    stats = limit_stats.compute(pool, day, lookback=lookback)
    errors.extend(stats.errors)

    print("[3/5] 情绪周期 / 30日周期")
    cyc = cycle.compute(stats.daily)

    print("[4/5] 连板梯队 / 强势评分")
    lad = ladder.compute(stats)
    stg = strong.compute(stats)

    print("[5/8] 指数环境")
    env = _index_env(day)

    # 涨跌榜（盘后口径，来自本地收盘）——提前计算，P3 活跃票集合要用
    latest = stats.days[-1] if stats.days else day
    ups, downs = [], []
    if stats.rows is not None and not stats.rows.empty:
        today = stats.rows[stats.rows["day"] == latest].sort_values("ret", ascending=False)
        cols = ["instrument", "name", "industry", "ret", "close", "streak",
                "limit_up", "limit_type", "amount"]
        ups = today.head(20)[cols].to_dict("records")
        downs = today.tail(20)[cols].iloc[::-1].to_dict("records")

    # ---------- P3 外延模块（全部 fail-open，失败只记 errors） ----------
    latest_day = stats.days[-1] if stats.days else day
    limit_rows = [r for r in stats.stocks_latest if r.limit_up]
    limit_codes = {r.instrument[2:] for r in limit_rows}
    strong_codes = {s["instrument"][2:] for s in stg.stocks[:30]}
    active_insts = sorted({r.instrument for r in limit_rows}
                          | {s["instrument"] for s in stg.stocks[:30]}
                          | {r["instrument"] for r in (ups + downs)[:40]})

    print("[6/8] 题材热度（东财概念/行业板块）")
    themes_data: dict = {"available": False}
    try:
        themes_data = themes.compute_themes(pool, limit_codes, strong_codes)
        if not themes_data.get("available"):
            errors.append("题材热度: 东财板块接口不可用")
    except Exception as e:  # noqa: BLE001
        errors.append(f"题材热度: {e}")

    print("[7/8] 板块轮动 / 资金流向 / 解禁雷区")
    rot = rotation.compute(stats)
    flow: dict = {"available": False}
    try:
        flow = fundflow.compute_fundflow(pool)
        if not flow.get("available"):
            errors.append("资金流向: 东财接口不可用")
    except Exception as e:  # noqa: BLE001
        errors.append(f"资金流向: {e}")

    unlock_alerts: dict = {}
    try:
        unlock.refresh_all()
        unlock_alerts = unlock.alerts_for([p["instrument"] for p in pool])
    except Exception as e:  # noqa: BLE001
        errors.append(f"解禁查询: {e}")
    # 解禁标注注入梯队与强势榜
    for t in lad.tiers:
        for s in t["stocks"]:
            ua = unlock_alerts.get(s["instrument"])
            if ua:
                s["unlock_alert"] = ua
    for s in lad.first_boards:
        ua = unlock_alerts.get(s["instrument"])
        if ua:
            s["unlock_alert"] = ua
    for s in stg.stocks:
        ua = unlock_alerts.get(s["instrument"])
        if ua:
            s["unlock_alert"] = ua

    print("[8/8] 快讯流 + sector_pulse 兼容产出")
    feed: dict = {"available": False}
    try:
        feed = news.load_feed(pool, lookback_days=3)
    except Exception as e:  # noqa: BLE001
        errors.append(f"快讯: {e}")

    latest_stat = stats.daily[-1].to_dict() if stats.daily else {}

    # sector_pulse 兼容产出（修复看板「市场热度」生成端缺失）
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        pulse = themes.sector_pulse_compat(
            themes_data,
            [{"instrument": r.instrument, "name": r.name, "ret": round(r.ret * 100, 2),
              "streak": r.streak, "limit_type": r.limit_type} for r in limit_rows],
            indices=_index_env_series(day))
        pulse["generated"] = _dt.now(_ZI("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
        pulse["signal_day"] = latest
        if pulse.get("industries_top") or pulse.get("concepts_top"):
            out_path = C.data_path("reports", f"sector_pulse_{latest.replace('-', '')}.json")
            import json as _json
            out_path.write_text(_json.dumps(pulse, ensure_ascii=False, indent=1) + "\n")
            print(f"  [兼容] sector_pulse 已产出 {out_path.name}")
    except Exception as e:  # noqa: BLE001
        errors.append(f"sector_pulse 兼容产出: {e}")

    # 行业分布（涨停）
    ind_dist: dict[str, int] = {}
    for r in stats.stocks_latest:
        if r.limit_up:
            ind_dist[r.industry] = ind_dist.get(r.industry, 0) + 1

    payload = {
        "status": "ok" if not errors else "partial",
        "errors": errors,
        "day": latest,
        "pool": {"size": len(pool)},
        "thermometer": latest_stat,
        "cycle": cyc.to_dict(),
        "ladder": lad.to_dict(),
        "strong": stg.to_dict(),
        "index_env": env,
        "gainers": ups,
        "losers": downs,
        "industry_dist": sorted(
            ({"industry": k, "count": v} for k, v in ind_dist.items()),
            key=lambda x: -x["count"]),
        "daily_stats": [d.to_dict() for d in stats.daily[-30:]],
        # ---- P3 ----
        "themes": themes_data,
        "rotation": rot.to_dict(),
        "fundflow": flow,
        "unlock_alerts": sorted(({"instrument": k, **v} for k, v in unlock_alerts.items()),
                                key=lambda x: (x["days_left"], -(x["ratio_pct"] or 0))),
        "news": feed,
        "disclaimer": "池内统计，纯展示，不改订单；不构成投资建议。",
    }
    path = store.save_daily(day, payload)
    print(f"[OK] 快照已写入 {path}（{len(errors)} 个非致命错误）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--lookback", type=int, default=limit_stats.LOOKBACK_DAYS)
    args = ap.parse_args()
    return run(day=args.day, force=args.force, lookback=args.lookback)


if __name__ == "__main__":
    sys.exit(main())
