"""阶段4 对账（收盘后）：订单(计划) vs 成交(实际)，目标持仓 vs 实际持仓。

检查项：
  - 未成交 / 部分成交 / 计划外成交（数量层面）
  - 成交价相对参考价的不利滑点（买高/卖低）
  - 执行后实际持仓与目标持仓的偏离（按市值占比，超 position_deviation_alert 告警）

验收要求"对账零差错"指：除价格差异与已知风控拦截外，不应出现无法解释的
数量错配。发现问题写入报告并告警（非零退出），供盘前人工核查。

用法：
    python reconcile.py --day 2026-06-11
"""

from __future__ import annotations

import argparse
import datetime as dt
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
        "target": d["target_position"],
        "holdings": d["nav"] / "holdings.csv",
        "reports": d["reports"],
    }


def reconcile_orders(day: str, account: str | None = None,
                     order_day: str | None = None) -> tuple[list[str], list[dict], list[dict]]:
    p = paths(account)
    order_day = order_day or day
    orders = S.read_csv("orders", p["orders"] / f"{order_day}.csv")
    ff = p["fills"] / f"{day}.csv"
    fills = S.read_csv("fills", ff) if ff.exists() else pd.DataFrame(
        columns=["instrument", "side", "shares", "price", "amount", "fee"])
    key = ["instrument", "side"]
    o = orders.groupby(key)["shares"].sum().rename("planned")
    pr = orders.set_index(key)["ref_price"]
    f = fills.groupby(key)["shares"].sum().rename("filled")
    fp = fills.groupby(key)["price"].mean().rename("fill_price")
    m = pd.concat([o, f], axis=1).fillna(0.0)
    m["ref_price"] = pr.groupby(level=[0, 1]).first()
    m["fill_price"] = fp

    issues, slips = [], []
    for (inst, side), r in m.iterrows():
        planned, filled = int(r["planned"]), int(r["filled"])
        if planned > 0 and filled == 0:
            issues.append({"instrument": inst, "side": side, "type": "未成交",
                           "planned": planned, "filled": filled})
        elif filled < planned:
            issues.append({"instrument": inst, "side": side, "type": "部分成交",
                           "planned": planned, "filled": filled})
        elif planned == 0 and filled > 0:
            issues.append({"instrument": inst, "side": side, "type": "计划外成交",
                           "planned": planned, "filled": filled})
        if filled > 0 and r["ref_price"] == r["ref_price"] and r["ref_price"] > 0:
            if side.upper() == "BUY":
                slip = r["fill_price"] / r["ref_price"] - 1
            else:
                slip = 1 - r["fill_price"] / r["ref_price"]
            slips.append({"instrument": inst, "side": side,
                          "ref": round(r["ref_price"], 2),
                          "fill": round(r["fill_price"], 2),
                          "slip_pct": round(slip * 100, 3)})
    return [f"{i['type']}: {i['side']} {i['instrument']} 计划{i['planned']}→成交{i['filled']}"
            for i in issues], issues, slips


def reconcile_holdings(day: str, account: str | None = None,
                       order_day: str | None = None) -> list[dict]:
    p = paths(account)
    order_day = order_day or day
    tf = p["target"] / f"{order_day}.csv"
    if not tf.exists() or not p["holdings"].exists():
        return []
    target = S.read_csv("target_position", tf).set_index("instrument")
    actual = S.read_csv("holdings", p["holdings"]).set_index("instrument")
    insts = sorted(set(target.index) | set(actual.index))
    px = C.close_prices(insts, day)
    acc = C.load_account(account)
    nav = sum(float(px.get(i, 0)) * int(actual.at[i, "shares"])
              for i in actual.index) + max(float(acc["cash"]), 0) if acc else 1.0
    nav = nav or 1.0
    thr = C.CFG["risk"]["position_deviation_alert"]
    devs = []
    for inst in insts:
        ts = int(target.at[inst, "shares"]) if inst in target.index else 0
        asx = int(actual.at[inst, "shares"]) if inst in actual.index else 0
        if ts == asx:
            continue
        price = float(px.get(inst, 0))
        dev_val = abs(ts - asx) * price
        devs.append({"instrument": inst, "target": ts, "actual": asx,
                     "dev_pct": round(dev_val / nav * 100, 3),
                     "alert": dev_val / nav > thr})
    return devs


def render(day: str, issues: list[dict], slips: list[dict], devs: list[dict],
           order_day: str | None = None) -> str:
    adverse = [s for s in slips if s["slip_pct"] > 0]
    avg_slip = round(sum(s["slip_pct"] for s in slips) / len(slips), 3) if slips else 0.0
    alerts = [d for d in devs if d["alert"]]
    clean = not issues and not alerts
    L = [
        "# 每日对账报告",
        "",
        f"- 交易日: {day}　生成: {dt.datetime.now():%Y-%m-%d %H:%M}",
        f"- 订单日: {order_day or day}",
        f"- 结论: {'**对账通过（零错配）**' if clean else '**发现差异，需人工核查**'}",
        "",
        "## 订单 vs 成交",
    ]
    if issues:
        L += ["| 类型 | 方向 | 标的 | 计划 | 成交 |", "|---|---|---|---|---|"]
        L += [f"| {i['type']} | {i['side']} | {i['instrument']} | {i['planned']} | {i['filled']} |"
              for i in issues]
    else:
        L.append("全部订单按计划成交，无未成交/部分/计划外。")
    L += ["", f"## 滑点（{len(slips)} 笔，均值 {avg_slip}%，不利 {len(adverse)} 笔）"]
    if slips:
        L += ["| 标的 | 方向 | 参考价 | 成交价 | 滑点% |", "|---|---|---|---|---|"]
        L += [f"| {s['instrument']} | {s['side']} | {s['ref']} | {s['fill']} | {s['slip_pct']:+} |"
              for s in sorted(slips, key=lambda x: -x["slip_pct"])[:15]]
    L += ["", "## 目标持仓 vs 实际持仓"]
    if devs:
        L += ["| 标的 | 目标 | 实际 | 市值偏离% | 告警 |", "|---|---|---|---|---|"]
        L += [f"| {d['instrument']} | {d['target']} | {d['actual']} | {d['dev_pct']} | "
              f"{'ALERT' if d['alert'] else ''} |" for d in devs]
    else:
        L.append("实际持仓与目标持仓一致。")
    return "\n".join(L)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--day", default=None)
    p.add_argument("--order-day", default=None, help="订单日，默认等于成交日；次日开盘成交时传前一交易日")
    p.add_argument("--account", default=None)
    args = p.parse_args()
    day = args.day or C.latest_trading_day()

    msgs, issues, slips = reconcile_orders(day, args.account, args.order_day)
    devs = reconcile_holdings(day, args.account, args.order_day)
    report = render(day, issues, slips, devs, args.order_day)
    reports = paths(args.account)["reports"]
    reports.mkdir(parents=True, exist_ok=True)
    out = reports / f"reconcile_{day}.md"
    out.write_text(report)
    print(report)

    alerts = [d for d in devs if d["alert"]]
    if issues:
        C.alert("WARN", f"对账发现 {len(issues)} 项订单差异：{'; '.join(msgs[:5])}", day)
    if alerts:
        C.alert("WARN", f"{len(alerts)} 只持仓偏离目标超阈值："
                f"{', '.join(d['instrument'] for d in alerts)}", day)
    print(f"\n[OK] 报告 {out}")
    return 0 if (not issues and not alerts) else 2


if __name__ == "__main__":
    raise SystemExit(main())
