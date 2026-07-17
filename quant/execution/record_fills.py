"""阶段4 成交回填（路线一：人工下单后录入实际成交）。

两步用法：
  1) 收盘前生成回填模板（按当日订单预填，人工把价格/数量改成实际成交）：
        python record_fills.py --day 2026-06-11 --template
     生成 data/fills/2026-06-11.csv，列：instrument,side,shares,price,amount,fee
     （amount/fee 留 0 即自动按价×量与费率计算；如要用券商实际费用，直接填 fee）
  2) 录入完毕后应用，更新持仓与现金账本：
        python record_fills.py --day 2026-06-11 --apply
     - 买入：现金 -= 成交额+费用；卖出：现金 += 成交额-费用
     - 新建仓位写入 entry_date=当日（供 hold_thresh 计持有期）
     - 持仓 last_price 用当日收盘盯市
     产出：data/nav/holdings.csv(+.done)、data/nav/account.json

首次开户（注入初始资金、建立现金账本）：
        python record_fills.py --init-capital 100000 --day 2026-06-11
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
        "orders": d["orders"],
        "fills": d["fills"],
        "holdings": d["nav"] / "holdings.csv",
    }


def make_template(day: str, account: str | None = None, order_day: str | None = None) -> None:
    p = paths(account)
    order_day = order_day or day
    of = p["orders"] / f"{order_day}.csv"
    orders = S.read_csv("orders", of)
    tmpl = pd.DataFrame({
        "instrument": orders["instrument"], "side": orders["side"],
        "shares": orders["shares"], "price": orders["ref_price"],
        "amount": 0.0, "fee": 0.0,
    })
    p["fills"].mkdir(parents=True, exist_ok=True)
    out = p["fills"] / f"{day}.csv"
    tmpl.to_csv(out, index=False)
    print(f"[OK] 回填模板已生成: {out}")
    if order_day != day:
        print(f"  → 模板使用订单日 {order_day}，成交记录日期为 {day}")
    print("  → 按实际成交修改 price/shares（未成交的行把 shares 改为 0）；")
    print("  → fee 留 0 自动计算，或填券商实际费用；改完执行 --apply")


def load_holdings(account: str | None = None) -> pd.DataFrame:
    f = paths(account)["holdings"]
    if not f.exists():
        return pd.DataFrame(columns=["instrument", "shares", "last_price", "entry_date"])
    return S.read_csv("holdings", f)


def apply_fills(day: str, account: str | None = None, force: bool = False) -> int:
    p = paths(account)
    acc = C.load_account(account)
    if acc is None:
        C.alert("CRIT", "账户未建立，请先 --init-capital 建账", day)
        return 1
    # 幂等防护：成交一旦应用即写入 last_fill_date；重复应用会把现金/持仓重复计算，
    # 故同日或更早的成交默认拒绝再次应用（确需重算用 --force，且需自行回滚账本）。
    last = acc.get("last_fill_date")
    if last and day <= last and not force:
        C.alert("WARN", f"[{account or 'legacy'}] {day} 成交已应用过"
                f"(last_fill_date={last})，跳过以防重复扣账；确需重算请用 --force", day)
        return 0
    ff = p["fills"] / f"{day}.csv"
    fills = S.read_csv("fills", ff)
    fills = fills[fills["shares"] > 0].copy()  # shares=0 视为未成交

    # 成交额与费用：amount 总按 价×量 重算；fee 为 0 时按费率计算（>0 则用录入值）
    fills["amount"] = (fills["shares"] * fills["price"]).round(2)
    fills["fee"] = [f if f and f > 0 else C.fill_fee(s, a)
                    for f, s, a in zip(fills["fee"], fills["side"], fills["amount"])]

    hold = load_holdings(account)
    pos = {r.instrument: {"shares": int(r.shares), "entry_date": r.entry_date}
           for r in hold.itertuples()}
    cash = float(acc["cash"])

    for r in fills.itertuples():
        side, sh, amt, fee = r.side.upper(), int(r.shares), float(r.amount), float(r.fee)
        if side == "BUY":
            cash -= amt + fee
            cur = pos.get(r.instrument, {"shares": 0, "entry_date": day})
            cur = {"shares": cur["shares"] + sh,
                   "entry_date": cur.get("entry_date") or day}
            if cur["entry_date"] in (None, "", "None"):
                cur["entry_date"] = day
            pos[r.instrument] = cur
        else:  # SELL
            cash += amt - fee
            cur = pos.get(r.instrument)
            if not cur:
                C.alert("WARN", f"卖出未持有标的 {r.instrument}，已忽略", day)
                continue
            cur["shares"] -= sh
            if cur["shares"] <= 0:
                pos.pop(r.instrument)
            else:
                pos[r.instrument] = cur

    # 收盘盯市：缺价时保留原 last_price，禁止写 0（增量补数漏票时会把净值打穿）
    prev_px = {r.instrument: float(r.last_price) for r in hold.itertuples()}
    insts = list(pos)
    px = C.close_prices(insts, day)
    rows = []
    for inst, v in pos.items():
        raw = px.get(inst)
        try:
            mark = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            mark = 0.0
        if mark <= 0:
            mark = float(prev_px.get(inst) or 0.0)
            if mark <= 0:
                C.alert("WARN", f"盯市缺价 {inst}，last_price 暂记 0", day)
        rows.append({"instrument": inst, "shares": int(v["shares"]),
                     "last_price": round(mark, 2),
                     "entry_date": v.get("entry_date") or day})
    new_hold = pd.DataFrame(rows, columns=["instrument", "shares", "last_price",
                                           "entry_date"])
    S.write_csv("holdings", new_hold, p["holdings"])
    S.write_csv("fills", fills, ff)  # 回写带 amount/fee 的规范成交记录 + .done

    acc["cash"] = round(cash, 2)
    acc["last_fill_date"] = day
    C.save_account(acc, account)

    buys = fills[fills.side.str.upper() == "BUY"]
    sells = fills[fills.side.str.upper() == "SELL"]
    print(f"[OK] {day} 回填完成：买入 {len(buys)} 笔 / 卖出 {len(sells)} 笔；"
          f"持仓 {len(new_hold)} 只，现金 {cash:,.2f} 元，总费用 {fills['fee'].sum():,.2f} 元")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--day", default=None, help="交易日，默认数据最新交易日")
    p.add_argument("--template", action="store_true", help="按订单生成回填模板")
    p.add_argument("--apply", action="store_true", help="应用成交，更新持仓/现金")
    p.add_argument("--init-capital", type=float, default=None, help="首次建账注入资金")
    p.add_argument("--account", default=None,
                   help="账户名，如 research_sim_100k / live_manual_10k")
    p.add_argument("--order-day", default=None,
                   help="生成模板时读取的订单日；默认等于 --day")
    p.add_argument("--force", action="store_true",
                   help="强制重新应用已应用过的成交（重复扣账风险，仅用于人工修账）")
    args = p.parse_args()
    day = args.day or C.latest_trading_day()

    if args.init_capital is not None:
        C.init_account(args.init_capital, day, args.account)
        who = f"账户 {args.account} " if args.account else ""
        print(f"[OK] {who}已建立：初始资金 {args.init_capital:,.2f} 元（{day}）")
        return 0
    if args.template:
        make_template(day, args.account, args.order_day)
        return 0
    if args.apply:
        return apply_fills(day, args.account, args.force)
    p.error("需指定 --template / --apply / --init-capital 之一")


if __name__ == "__main__":
    raise SystemExit(main())
