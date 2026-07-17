"""阶段4 每日流水线编排（路线一：手动下单）。

把"除下单/回填录入外"的环节串成一条可重跑的流水线，任一步失败即告警并中止下游。

  evening   收盘后：更新数据 → 生成信号 →（可选 TA 定性否决）→ 调仓清单(含UMP/TA) → 健康检查
            产出推送给人，次日开盘人工在同花顺下单
            （未显式 --day 时，update_daily 成功后自动刷新为最新交易日）
  （人工）   按清单下单 → 收盘后 record_fills.py --apply 录入实际成交
  postclose 当晚：健康检查(成交已回填) → 对账 → 计算净值 → 出日报

用法：
    python run_daily.py --stage evening --account research_sim_100k --ump
    python run_daily.py --stage postclose --account research_sim_100k
    python run_daily.py --stage evening --account live_manual_10k --ump
    python run_daily.py --stage postclose --account live_manual_10k
    python run_daily.py --stage evening --account shadow_ta_sim --skip-data
    python run_daily.py --stage evening --skip-data   # 离线/演示：跳过数据更新
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pandas as pd

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


def _autofill_no_trade_day(account: str, day: str, order_day: str | None) -> None:
    """无交易日自动回填空成交，根治人工实盘 postclose 的误报。

    仅在「订单日订单为空（确无交易）且当日成交尚未回填」时触发：写空 fills 并 --apply，
    使持仓按当日收盘重新盯市、净值/日报照常产出，monitor 不再因缺 fills 报 CRIT。
    订单非空时绝不自动回填——必须人工录入真实成交，以免用"零成交"污染净值序列。
    """
    dirs = C.ensure_account_dirs(account)
    fills = dirs["fills"] / f"{day}.csv"
    if fills.exists() and fills.with_suffix(".done").exists():
        return  # 已回填（人工或先前自动），不重复
    if not order_day:
        return
    of = dirs["orders"] / f"{order_day}.csv"
    if not of.exists():
        return  # 无订单文件：交给 monitor 按缺失处理
    try:
        n_orders = len(pd.read_csv(of))
    except Exception:
        return
    if n_orders > 0:
        return  # 有订单：须人工录入真实成交，绝不自动填
    rf = str(QUANT / "execution" / "record_fills.py")
    base = [PY, rf, "--account", account, "--day", day]
    step("空成交回填(无交易日)", base + ["--order-day", order_day, "--template"],
         day, fatal=False)
    if step("应用空成交", base + ["--apply"], day, fatal=False):
        C.alert("INFO", f"[{account}] {day} 订单日 {order_day} 无调仓，已自动回填空成交", day)


def evening(args, day: str) -> int:
    cfg = C.account_config(args.account) if args.account else C.CFG
    if not args.skip_data:
        if not step("更新数据", [PY, str(QUANT / "data_pipeline" / "update_daily.py")], day):
            return 1
        # 主路径（investment_data release）偶发停更，会让日历卡死、在旧日上空转。
        # 用 baostock 增量把标的池补到最新交易日；非致命：baostock 也挂了就沿用现有数据。
        # 上游正常时增量脚本判定无缺口即秒退，几乎无开销。
        step("增量补数(上游滞后回退)",
             [PY, str(QUANT / "data_pipeline" / "update_incremental.py")], day, fatal=False)
        # update_daily/增量补数 可能把日历从旧日推进；未显式指定 --day 时必须刷新，
        # 否则会在旧日上重复出信号/调仓单（06-15 晚间曾因此用 06-11 跑了一整轮）。
        if args.day is None:
            day = C.latest_trading_day()
            print(f"[OK] 数据更新后刷新交易日 → {day}")
    if not step("生成信号", [PY, str(QUANT / "research" / "predict_daily.py"),
                            "--date", day], day):
        return 1

    # TA 定性否决：仅账户开启 use_ta_veto 或 CLI --ta-veto 时运行；失败由 run_veto fail-open
    use_ta = args.ta_veto or bool(cfg.get("execution", {}).get("use_ta_veto"))
    if use_ta:
        ta_cmd = [PY, str(QUANT / "overlays" / "ta_veto" / "run_veto.py"),
                  "--date", day]
        if args.account:
            ta_cmd += ["--account", args.account]
        if args.dry_run_ta:
            ta_cmd += ["--dry-run"]
        # 与后续 make_trade_plan 的 UMP 开关对齐
        if args.ump or bool(cfg.get("execution", {}).get("use_ump")):
            ta_cmd += ["--ump"]
        if not step("TA定性否决", ta_cmd, day, fatal=False):
            C.alert("WARN", "TA 否决步骤异常；make_trade_plan 将 fail-open 跳过否决", day)

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
    if args.ump or bool(cfg.get("execution", {}).get("use_ump")):
        plan += ["--ump"]
    if use_ta:
        plan += ["--ta-veto"]
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
    mode = cfg.get("account", {}).get("mode", "manual")
    if mode == "simulated":
        C.alert("INFO", "evening 流水线完成，次日 postclose 将自动成交", day)
    else:
        C.alert("INFO", "evening 流水线完成，调仓清单待次日人工执行", day)
    return 0


def postclose(args, day: str) -> int:
    cfg = C.account_config(args.account) if args.account else C.CFG
    mode = cfg.get("account", {}).get("mode")
    order_day = C.resolve_order_day(args.account, day, args.order_day)
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
    elif args.account:
        # 人工实盘：无交易日（订单为空）自动回填空成交，避免次日成交未录时误报 CRIT
        _autofill_no_trade_day(args.account, day, order_day)

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
    p.add_argument("--ta-veto", action="store_true",
                   help="强制跑 TA 定性否决（账户 use_ta_veto 时也会自动跑）")
    p.add_argument("--dry-run-ta", action="store_true",
                   help="TA 否决 dry-run（不调 LLM，写全 pass）")
    p.add_argument("--config", default=None,
                   help="传给 make_trade_plan.py 的实盘配置覆盖文件")
    p.add_argument("--account", default=None,
                   help="账户名，如 research_sim_100k / live_manual_10k / shadow_ta_sim")
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
