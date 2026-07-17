"""阶段4 净值计算（收盘后）：持仓收盘盯市 + 现金 → 组合净值，写入 nav/daily.csv。

  净值 = 现金 + Σ(持仓股数 × 当日收盘价)
  当日收益 = 净值 / 昨净值 - 1；超额 = 当日收益 - 中证500当日收益
  换手 = 当日成交额 / 净值（来自 fills）

用法：
    python compute_nav.py --day 2026-06-11
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

def paths(account: str | None) -> dict[str, Path]:
    d = C.ensure_account_dirs(account)
    return {
        "daily": d["nav"] / "daily.csv",
        "holdings": d["nav"] / "holdings.csv",
        "fills": d["fills"],
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--day", default=None)
    p.add_argument("--account", default=None)
    args = p.parse_args()
    day = args.day or C.latest_trading_day()
    pth = paths(args.account)

    acc = C.load_account(args.account)
    if acc is None:
        C.alert("CRIT", "账户未建立，无法计算净值", day)
        return 1
    cash = float(acc["cash"])

    hold = S.read_csv("holdings", pth["holdings"]) if pth["holdings"].exists() else pd.DataFrame(
        columns=["instrument", "shares", "last_price", "entry_date"])
    insts = hold["instrument"].tolist()
    px = C.close_prices(insts, day)

    def _mark(r) -> float:
        raw = px.get(r.instrument)
        try:
            p = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            p = 0.0
        if p <= 0:
            p = float(r.last_price or 0.0)
        return p

    position_value = float(sum(_mark(r) * int(r.shares) for r in hold.itertuples()))
    nav = round(cash + position_value, 2)

    # 历史 daily.csv（去掉本日重复，便于重跑）；前日净值按交易日对齐，避免重算中间日时
    # 误用「文件末行」（可能是更晚的一日）导致日收益爆炸。
    hist = S.read_csv("daily", pth["daily"]) if pth["daily"].exists() else pd.DataFrame(
        columns=list(S.SCHEMAS["daily"]))
    hist = hist[hist["date"] != day]
    if len(hist):
        hist = hist.sort_values("date")
    prev = C.prev_trading_day(day)
    if len(hist) and prev:
        matched = hist[hist["date"] == prev]
        prev_nav = float(matched["nav"].iloc[-1]) if len(matched) else float(hist["nav"].iloc[-1])
    elif len(hist):
        prev_nav = float(hist["nav"].iloc[-1])
    else:
        prev_nav = float(acc["start_capital"])

    daily_ret = nav / prev_nav - 1 if prev_nav else 0.0
    bench_ret = C.benchmark_return(day, prev) if prev else float("nan")
    # 建仓首日按收盘价入场，当日未承担持有期敞口，超额计 0（避免与基准全日收益错配）
    inception = len(hist) == 0
    excess = 0.0 if inception else (daily_ret - bench_ret if bench_ret == bench_ret
                                    else float("nan"))

    ff = pth["fills"] / f"{day}.csv"
    traded = float(S.read_csv("fills", ff)["amount"].abs().sum()) if ff.exists() else 0.0
    turnover = round(traded / nav, 4) if nav else 0.0

    row = {"date": day, "nav": nav, "cash": round(cash, 2),
           "position_value": round(position_value, 2), "n_pos": len(hold),
           "turnover": turnover, "daily_ret": round(daily_ret, 6),
           "bench_ret": round(bench_ret, 6) if bench_ret == bench_ret else 0.0,
           "excess_ret": round(excess, 6) if excess == excess else 0.0}
    new_row = pd.DataFrame([row])
    out = new_row if hist.empty else pd.concat([hist, new_row], ignore_index=True)
    out = out.sort_values("date")
    S.write_csv("daily", out, pth["daily"])

    cum = nav / float(acc["start_capital"]) - 1
    print(f"[OK] {day} 净值 {nav:,.2f}（现金 {cash:,.2f} + 持仓 {position_value:,.2f}，"
          f"{len(hold)} 只）｜当日 {daily_ret:+.2%} 超额 {excess:+.2%} 换手 {turnover:.1%}"
          f"｜累计 {cum:+.2%}")

    # 单日亏损硬熔断：写账户标志 + 剥掉当日订单 BUY（次日不再开新仓）
    halt = float(C.CFG.get("risk", {}).get("daily_loss_halt", 0.03))
    if daily_ret <= -halt:
        reason = f"daily_ret={daily_ret:.2%} <= -{halt:.0%}"
        C.set_halt(day, args.account, reason=reason)
        C.alert("CRIT", f"当日亏损 {daily_ret:.2%} 触及熔断阈值 {-halt:.0%}，"
                "已暂停开新仓（仅允许卖出）", day)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
