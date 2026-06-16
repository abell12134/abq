"""阶段5 历史回填：让两条线在过去 N 个交易日上跑出净值序列，
使看板启动即有数据，之后由常驻调度器每日向前累积。

口径（与正式每日闭环完全一致，只是离线批量重放）：
  - 交易日序列 days = 最新数据日往前数 N+1 个交易日；
  - 对每个 days[i]：evening 生成 days[i] 的调仓清单（信号、UMP、风控预检）；
  - 对 days[i+1]：postclose 按 days[i+1] 开盘价模拟成交、对账、算净值、出日报；
  - 两条线（research_sim_100k / live_manual_10k）均以 simulated 模式重放。

幂等：已应用过的成交日会被 apply_fills 跳过；重跑安全。

断档补跑（服务停跑/节假日后账户净值落后于日历）：
  1) 先补缺失信号（缺哪几天补哪几天，可一次区间）：
       python research/predict_range.py --start <起> --end <止>
  2) 再按交易日顺序离线重放两条线（--skip-data，不重复下载）：
       python ops/backfill.py --start <账户最后净值日> --end <数据最新日>
     postclose 会用 prev_trading_day(成交日) 作为 order_day 找调仓单；
     若中间缺某天的 evening（如周五 06-12 未出单），则周一 postclose 会 CRIT——须用 backfill 补齐。

用法：
    python ops/backfill.py --days 6
    python ops/backfill.py --start 2026-06-04 --end 2026-06-11
    python ops/backfill.py --accounts research_sim_100k,live_manual_10k --days 6
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
RUN_DAILY = QUANT / "ops" / "run_daily.py"


def trading_days(start: str | None, end: str | None, n: int) -> list[str]:
    cal = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in C.calendar()]
    if end:
        cal = [d for d in cal if d <= end]
    if start:
        cal = [d for d in cal if d >= start]
    elif n:
        cal = cal[-(n + 1):]  # 多取 1 天：首日只建仓，从次日起才有净值
    return cal


def run(stage: str, account: str, day: str, order_day: str | None = None,
        ump: bool = True) -> int:
    cmd = [PY, str(RUN_DAILY), "--stage", stage, "--account", account,
           "--day", day, "--skip-data"]
    if stage == "evening" and ump:
        cmd += ["--ump"]
    if order_day:
        cmd += ["--order-day", order_day]
    print(f"\n>>> {stage} {account} day={day}"
          f"{' order_day=' + order_day if order_day else ''}")
    return subprocess.run(cmd).returncode


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--accounts", default="research_sim_100k,live_manual_10k",
                   help="逗号分隔的账户名")
    p.add_argument("--days", type=int, default=6, help="回填最近 N 个交易日")
    p.add_argument("--start", default=None, help="起始交易日（含），覆盖 --days")
    p.add_argument("--end", default=None, help="结束交易日（含），默认数据最新日")
    p.add_argument("--no-ump", action="store_true")
    args = p.parse_args()

    days = trading_days(args.start, args.end, args.days)
    if len(days) < 2:
        print(f"[FATAL] 可用交易日不足（{days}），无法回填")
        return 1
    accounts = [a.strip() for a in args.accounts.split(",") if a.strip()]
    print(f"### 回填交易日 {days[0]} → {days[-1]}（{len(days)} 天）"
          f" 账户 {accounts} ###")

    for account in accounts:
        print(f"\n========== 账户 {account} ==========")
        for i in range(len(days) - 1):
            d0, d1 = days[i], days[i + 1]
            if run("evening", account, d0, ump=not args.no_ump) != 0:
                C.alert("WARN", f"[{account}] 回填 evening {d0} 失败，跳过该日", d0)
                continue
            if run("postclose", account, d1, order_day=d0) != 0:
                C.alert("WARN", f"[{account}] 回填 postclose {d1} 失败", d1)
    print("\n[OK] 回填完成。启动看板服务查看：bash webapp/serve.sh start")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
