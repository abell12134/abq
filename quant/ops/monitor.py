"""阶段4 监控告警：流水线健康检查。任一环节异常即告警（写 alerts.log + 打印）。

检查项随阶段而变：
  evening  ：数据新鲜度、当日信号已生成、账户已建立
  postclose：成交已回填、持仓/净值已更新、当日是否触发熔断

用法：
    python monitor.py --day 2026-06-11 --stage evening
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

QUANT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QUANT / "ops"))
import common as C  # noqa: E402

DATA = QUANT / "data"


def _has(path: Path, need_done: bool = True) -> bool:
    return path.exists() and (path.with_suffix(".done").exists() if need_done else True)


def run_checks(day: str, stage: str, account: str | None = None) -> list[dict]:
    alerts: list[dict] = []
    dirs = C.ensure_account_dirs(account)

    def add(level, msg):
        prefix = f"[{account}] " if account else ""
        alerts.append(C.alert(level, prefix + msg, day))

    latest = C.latest_trading_day()
    if latest < day:
        add("CRIT", f"Qlib 数据最新 {latest} 落后于目标 {day}，盘前不可交易")

    acc = C.load_account(account)

    if stage == "evening":
        if not _has(DATA / "signals" / f"{day}.csv"):
            add("CRIT", "当日信号缺失或未完成（.done 缺）")
        if not _has(dirs["orders"] / f"{day}.csv", need_done=False):
            add("WARN", "当日调仓清单未生成")
        if acc is None:
            add("WARN", "账户尚未建立（首次交易前需 record_fills --init-capital）")
        # 熔断期间若订单仍含 BUY，说明剥仓/禁买未生效
        of = dirs["orders"] / f"{day}.csv"
        if acc and C.is_halted(acc=acc) and of.exists():
            try:
                import pandas as pd
                odf = pd.read_csv(of)
                n_buy = int((odf["side"].astype(str).str.upper() == "BUY").sum()) if not odf.empty else 0
                if n_buy > 0:
                    add("CRIT", f"熔断中但仍有 {n_buy} 笔 BUY 订单，请人工核查")
                else:
                    add("INFO", f"熔断生效中（halt_since={acc.get('halt_since')}），无新开仓")
            except Exception as e:
                add("WARN", f"熔断订单检查失败：{e}")

    if stage == "postclose":
        if not _has(dirs["fills"] / f"{day}.csv"):
            add("CRIT", "当日成交未回填（fills 缺失或未完成）")
        if not _has(dirs["nav"] / "holdings.csv"):
            add("WARN", "持仓文件未更新")
        if acc and C.is_halted(acc=acc):
            add("WARN", f"账户仍处于熔断（halt_since={acc.get('halt_since')}），"
                "次日调仓应禁止买入")

    if not alerts:
        C.alert("INFO", f"[{stage}] 健康检查全部通过", day)
    return alerts


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--day", default=None)
    p.add_argument("--stage", choices=["evening", "postclose"], default="evening")
    p.add_argument("--account", default=None)
    args = p.parse_args()
    day = args.day or C.latest_trading_day()
    alerts = run_checks(day, args.stage, args.account)
    crit = [a for a in alerts if a["level"] == "CRIT"]
    return 1 if crit else 0


if __name__ == "__main__":
    raise SystemExit(main())
