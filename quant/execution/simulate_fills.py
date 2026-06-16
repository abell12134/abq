"""研究模拟线：按次日开盘价自动模拟成交并应用到账户。

口径：
  - 执行日 day 默认取最新交易日；
  - 读取前一交易日 order_day 的调仓单；
  - 按 day 的真实开盘价（$open/$factor）生成 fills/day.csv；
  - 开盘状态过滤（贴近实盘摩擦，避免研究线系统性偏乐观）：
      * 停牌（当日无开盘价）：买卖都不成交（shares=0）；
      * 开盘一字/接近涨停（open/prev_close-1 ≥ 涨幅限制）：买入不成交；
      * 开盘接近跌停（open/prev_close-1 ≤ -跌幅限制）：卖出不成交；
    未成交记为 0 股，由 reconcile 暴露为订单/目标差异（与实盘口径一致）。
  - 调用 record_fills.apply_fills(day, account) 更新持仓、现金账本（幂等防重复）。

用法：
    python execution/simulate_fills.py --account research_sim_100k --day 2026-06-11
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

QUANT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QUANT / "contracts"))
sys.path.insert(0, str(QUANT / "ops"))
sys.path.insert(0, str(QUANT / "execution"))
import common as C  # noqa: E402
import schemas as S  # noqa: E402
from record_fills import apply_fills  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--account", required=True)
    p.add_argument("--day", default=None, help="执行日，默认最新交易日")
    p.add_argument("--order-day", default=None, help="订单日，默认执行日前一交易日")
    p.add_argument("--force", action="store_true",
                   help="强制重算已模拟过的成交（人工修账时用）")
    args = p.parse_args()
    day = args.day or C.latest_trading_day()
    order_day = args.order_day or C.prev_trading_day(day)
    if not order_day:
        C.alert("CRIT", f"[{args.account}] 无法确定 {day} 的前一交易日，不能模拟成交", day)
        return 1

    dirs = C.ensure_account_dirs(args.account)
    order_src = dirs["orders"] / f"{order_day}.csv"
    if not order_src.exists():
        C.alert("WARN", f"[{args.account}] 缺少订单 {order_src.name}，模拟成交跳过", day)
        return 0

    orders = S.read_csv("orders", order_src)
    if orders.empty:
        fills = pd.DataFrame(columns=["instrument", "side", "shares", "price", "amount", "fee"])
    else:
        insts = orders["instrument"].tolist()
        op = C.open_prices(insts, day)         # 执行日开盘价（成交价）
        pc = C.close_prices(insts, order_day)  # 订单日收盘价（判定涨跌停基准）
        blocked = 0
        rows = []
        for r in orders.itertuples():
            price = float(op.get(r.instrument, 0.0))
            prev = float(pc.get(r.instrument, 0.0))
            shares = int(r.shares)
            # 停牌：当日无开盘价，买卖都无法成交
            if price <= 0:
                shares = 0
            elif prev > 0:
                chg = price / prev - 1
                lim = C._limit_pct(r.instrument) * 0.97
                side = str(r.side).upper()
                # 开盘涨停买不进 / 开盘跌停卖不出
                if (side == "BUY" and chg >= lim) or (side == "SELL" and chg <= -lim):
                    shares = 0
            if shares == 0:
                blocked += 1
            amount = round(shares * price, 2)
            rows.append({
                "instrument": r.instrument,
                "side": r.side,
                "shares": shares,
                "price": round(price, 2),
                "amount": amount,
                "fee": 0.0,  # apply_fills 会按统一费率重算，保持与实盘线一致
            })
        fills = pd.DataFrame(rows)
        if blocked:
            C.alert("INFO", f"[{args.account}] {day} 开盘停牌/涨跌停导致 {blocked} 笔未成交",
                    day)

    out = dirs["fills"] / f"{day}.csv"
    S.write_csv("fills", fills, out)
    filled = int((fills["shares"] > 0).sum()) if not fills.empty else 0
    print(f"[OK] {args.account} {day} 已按 {order_day} 订单模拟成交 "
          f"{filled}/{len(fills)} 笔: {out}")
    return apply_fills(day, args.account, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
