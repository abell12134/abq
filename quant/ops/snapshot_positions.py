"""个股每日收盘快照（收盘后、纯离线）：把「操作过的股票」每天收盘价与涨跌幅记录下来。

标的池 = 当前持仓 ∪ 最近一个月成交过的股票（含已卖出），全部取本地 qlib EOD，
不联网。每只每天记录：真实收盘价（$close/$factor 后复权还原）与当日涨跌幅（按复权
价 $close 计算，避免除权日失真），写入 data/accounts/<账户>/nav/positions_daily.csv。

  chg_pct(%) = ($close_t / $close_{t-1} - 1) * 100

用法：
    python snapshot_positions.py --account live_manual_10k              # 记录最新交易日
    python snapshot_positions.py --account live_manual_10k --day 2026-07-16
    python snapshot_positions.py --account live_manual_10k --backfill    # 回填最近一个月
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

QUANT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QUANT / "contracts"))
sys.path.insert(0, str(QUANT / "ops"))
import common as C  # noqa: E402
import schemas as S  # noqa: E402

WINDOW = 22  # 约一个月的交易日


def _positions_daily_path(account: str | None) -> Path:
    return C.ensure_account_dirs(account)["nav"] / "positions_daily.csv"


def _trading_days_upto(day: str, n: int) -> list[str]:
    cal = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in C.calendar()]
    cal = [d for d in cal if d <= day]
    return cal[-n:] if cal else []


def _held_instruments(account: str | None) -> list[str]:
    f = C.account_subdirs(account)["nav"] / "holdings.csv"
    if not f.exists():
        return []
    h = S.read_csv("holdings", f)
    return h["instrument"].astype(str).tolist()


def _traded_instruments(account: str | None, since: str) -> list[str]:
    """最近一个月成交过（含已卖出）的标的：扫已完成的 fills 文件。"""
    fills_dir = C.account_subdirs(account)["fills"]
    if not fills_dir.exists():
        return []
    insts: set[str] = set()
    for f in sorted(fills_dir.glob("????-??-??.csv")):
        if f.stem < since or not f.with_suffix(".done").exists():
            continue
        try:
            df = S.read_csv("fills", f)
        except Exception:
            continue
        if not df.empty:
            insts.update(df["instrument"].astype(str).tolist())
    return sorted(insts)


def _pending_buy_instruments(account: str | None) -> list[str]:
    """最新调仓清单里待买入（BUY）的标的：让待操作标的也有近一月收盘历史。"""
    odir = C.account_subdirs(account)["orders"]
    if not odir.exists():
        return []
    days = sorted(f.stem for f in odir.glob("????-??-??.csv"))
    if not days:
        return []
    of = odir / f"{days[-1]}.csv"
    try:
        o = S.read_csv("orders", of)
    except Exception:
        return []
    if o.empty:
        return []
    return o[o["side"].astype(str).str.upper() == "BUY"]["instrument"].astype(str).tolist()


def universe(account: str | None, window_start: str) -> list[str]:
    insts = (set(_held_instruments(account))
             | set(_traded_instruments(account, window_start))
             | set(_pending_buy_instruments(account)))
    return sorted(insts)


def _features(instruments: list[str], start: str, end: str) -> pd.DataFrame:
    """一次性取窗口内复权价与真实收盘价：index=(instrument, datetime)。"""
    C.init_qlib()
    from qlib.data import D
    if not instruments:
        return pd.DataFrame()
    df = D.features(list(instruments), ["$close", "$close/$factor"],
                    start_time=start, end_time=end)
    if df is None or df.empty:
        return pd.DataFrame()
    df.columns = ["adj_close", "raw_close"]
    return df


def build_rows(account: str | None, days: list[str]) -> pd.DataFrame:
    if not days:
        return pd.DataFrame(columns=list(S.SCHEMAS["positions_daily"]))
    insts = universe(account, days[0])
    if not insts:
        return pd.DataFrame(columns=list(S.SCHEMAS["positions_daily"]))

    # 多取一根，供窗口首日算涨跌幅
    prev = C.prev_trading_day(days[0])
    start = prev or days[0]
    feats = _features(insts, start, days[-1])
    if feats.empty:
        return pd.DataFrame(columns=list(S.SCHEMAS["positions_daily"]))

    day_set = set(days)
    rows = []
    for inst, sub in feats.groupby(level="instrument"):
        sub = sub.droplevel("instrument").sort_index()
        sub["chg"] = sub["adj_close"].pct_change() * 100
        for ts, r in sub.iterrows():
            d = pd.Timestamp(ts).strftime("%Y-%m-%d")
            if d not in day_set:
                continue
            raw, chg = r["raw_close"], r["chg"]
            if pd.isna(raw):
                continue  # 停牌/无数据当日跳过
            rows.append({
                "date": d,
                "instrument": str(inst),
                "close": round(float(raw), 3),
                "chg_pct": round(float(chg), 3) if pd.notna(chg) else 0.0,
            })
    return pd.DataFrame(rows, columns=list(S.SCHEMAS["positions_daily"]))


def merge_write(account: str | None, new: pd.DataFrame) -> Path:
    path = _positions_daily_path(account)
    hist = S.read_csv("positions_daily", path) if path.exists() else None
    combined = new if hist is None or hist.empty else pd.concat(
        [hist, new], ignore_index=True)
    # 同一 (date, instrument) 以最新写入为准，便于重跑
    combined = combined.drop_duplicates(subset=["date", "instrument"], keep="last")
    combined = combined.sort_values(["date", "instrument"]).reset_index(drop=True)
    S.write_csv("positions_daily", combined, path)
    return path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--day", default=None)
    p.add_argument("--account", default=None)
    p.add_argument("--backfill", action="store_true",
                   help="回填最近一个月（默认只记录 --day 当天）")
    p.add_argument("--window", type=int, default=WINDOW,
                   help="回填/标的池的交易日窗口（默认 22 ≈ 一个月）")
    args = p.parse_args()
    day = args.day or C.latest_trading_day()

    days = _trading_days_upto(day, args.window) if args.backfill else [day]
    if not days:
        print(f"[WARN] 无可用交易日（day={day}）")
        return 0

    rows = build_rows(args.account, days)
    if rows.empty:
        print(f"[OK] {day} 无操作标的或无收盘数据，跳过个股快照")
        return 0

    path = merge_write(args.account, rows)
    n_inst = rows["instrument"].nunique()
    span = f"{rows['date'].min()}~{rows['date'].max()}" if len(days) > 1 else day
    print(f"[OK] 个股收盘快照 {span}：{n_inst} 只 × {rows['date'].nunique()} 天 "
          f"→ {path}（共写 {len(rows)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
