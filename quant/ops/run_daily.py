"""阶段4 每日流水线编排（路线一：手动下单）。

把"除下单/回填录入外"的环节串成一条可重跑的流水线，任一步失败即告警并中止下游。

  evening   收盘后：更新数据 → 生成信号 → 调仓清单(含UMP否决/风控预检) → 健康检查
            产出推送给人，次日开盘人工在同花顺下单
            （未显式 --day 时，update_daily 成功后自动刷新为最新交易日）
  （人工）   按清单下单 → 收盘后 record_fills.py --apply 录入实际成交
  postclose 当晚：健康检查(成交已回填) → 对账 → 计算净值 → 出日报

用法：
    python run_daily.py --stage evening --account research_sim_100k --ump
    python run_daily.py --stage postclose --account research_sim_100k
    python run_daily.py --stage evening --account live_manual_10k --ump
    python run_daily.py --stage postclose --account live_manual_10k
    python run_daily.py --stage evening --skip-data   # 离线/演示：跳过数据更新
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

QUANT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QUANT / "ops"))
import common as C  # noqa: E402

PY = sys.executable


def step(name: str, cmd: list[str], day: str, fatal: bool = True) -> bool:
    print(f"\n========== [{name}] {' '.join(cmd[1:])} ==========")
    r = subprocess.run(cmd)
    if r.returncode != 0:
        lvl = "CRIT" if fatal else "WARN"
        C.alert(lvl, f"流水线步骤失败：{name}（exit {r.returncode}）", day)
        return False
    return True


def evening(args, day: str) -> int:
    cfg = C.account_config(args.account) if args.account else C.CFG
    if not args.skip_data:
        if not step("更新数据", [PY, str(QUANT / "data_pipeline" / "update_daily.py")], day):
            return 1
        # update_daily 可能把日历从旧日推进到最新 release；未显式指定 --day 时必须刷新，
        # 否则会在旧日上重复出信号/调仓单（06-15 晚间曾因此用 06-11 跑了一整轮）。
        if args.day is None:
            day = C.latest_trading_day()
            print(f"[OK] 数据更新后刷新交易日 → {day}")
    if not step("生成信号", [PY, str(QUANT / "research" / "predict_daily.py"),
                            "--date", day], day):
        return 1
    plan = [PY, str(QUANT / "execution" / "make_trade_plan.py"), "--date", day]
    if args.account:
        plan += ["--account", args.account]
    if args.config:
        plan += ["--config", args.config]
    if args.capital:
        plan += ["--capital", str(args.capital)]
    elif args.account:
        acc = C.load_account(args.account)
        cash = (acc or {}).get("cash")
        cap = cash if cash is not None else cfg.get("account", {}).get("initial_capital")
        if cap is not None:
            plan += ["--capital", str(cap)]
        # 已建账：把账上闲置现金传入，调仓日按「持仓市值 + 现金」计可用资金，
        # 否则 make_trade_plan 在有持仓时会把闲置现金当 0，导致仓位偏小、现金永不部署。
        if cash is not None:
            plan += ["--cash", str(cash)]
    if args.cash:
        plan += ["--cash", str(args.cash)]
    if args.ump:
        plan += ["--ump"]
    if not step("生成调仓清单", plan, day):
        return 1
    # 注：不在 evening 预生成 fills 模板——成交日期应为"次日执行日"而非订单日，
    # 在订单日写 fills/<订单日>.csv 会与上一日 postclose 的真实成交文件撞名。
    # 研究线由 simulate_fills 在 postclose 自动产出成交；
    # 实盘线在执行日用 record_fills.py --order-day <订单日> --template 生成模板。
    mon = [PY, str(QUANT / "ops" / "monitor.py"), "--day", day, "--stage", "evening"]
    if args.account:
        mon += ["--account", args.account]
    step("健康检查", mon, day, fatal=False)
    C.alert("INFO", "evening 流水线完成，调仓清单待次日人工执行", day)
    return 0


def postclose(args, day: str) -> int:
    cfg = C.account_config(args.account) if args.account else C.CFG
    mode = cfg.get("account", {}).get("mode")
    order_day = args.order_day or C.prev_trading_day(day)
    if mode == "simulated":
        if C.load_account(args.account) is None:
            cap = cfg.get("account", {}).get("initial_capital")
            if cap:
                C.init_account(cap, day, args.account)
        sim = [PY, str(QUANT / "execution" / "simulate_fills.py"),
               "--account", args.account, "--day", day]
        if order_day:
            sim += ["--order-day", order_day]
        if args.force:
            sim += ["--force"]
        if not step("模拟成交", sim, day):
            return 1

    mon = [PY, str(QUANT / "ops" / "monitor.py"), "--day", day, "--stage", "postclose"]
    rec = [PY, str(QUANT / "execution" / "reconcile.py"), "--day", day]
    nav = [PY, str(QUANT / "ops" / "compute_nav.py"), "--day", day]
    rpt = [PY, str(QUANT / "ops" / "daily_report.py"), "--day", day]
    if args.account:
        for cmd in (mon, rec, nav, rpt):
            cmd += ["--account", args.account]
    if order_day:
        rec += ["--order-day", order_day]

    if not step("健康检查", mon, day):
        return 1
    step("对账", rec, day, fatal=False)  # 对账有差异返回非零，但不应阻断净值/日报
    if not step("计算净值", nav, day):
        return 1
    if not step("出日报", rpt, day):
        return 1
    C.alert("INFO", "postclose 流水线完成", day)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--stage", choices=["evening", "postclose"], required=True)
    p.add_argument("--day", default=None)
    p.add_argument("--capital", type=float, default=None)
    p.add_argument("--cash", type=float, default=0.0)
    p.add_argument("--ump", action="store_true")
    p.add_argument("--config", default=None,
                   help="传给 make_trade_plan.py 的实盘配置覆盖文件")
    p.add_argument("--account", default=None,
                   help="账户名，如 research_sim_100k / live_manual_10k")
    p.add_argument("--order-day", default=None,
                   help="postclose 对账所用订单日，默认前一交易日")
    p.add_argument("--force", action="store_true",
                   help="研究线强制重算已模拟过的成交（人工修账时用）")
    p.add_argument("--skip-data", action="store_true")
    args = p.parse_args()
    day = args.day or C.latest_trading_day()
    print(f"### 每日流水线 stage={args.stage} day={day} ###")
    return evening(args, day) if args.stage == "evening" else postclose(args, day)


if __name__ == "__main__":
    raise SystemExit(main())
