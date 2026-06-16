"""小资金实盘复盘汇总。

用途：把路线一实盘记录沉淀成可复盘的数据面板，方便后续验证与调整。

输入：
    data/nav/account.json
    data/nav/daily.csv
    data/fills/YYYY-MM-DD.csv
    data/logs/alerts.log

输出：
    data/reports/live_review_YYYY-MM-DD.md

用法：
    python ops/review_live.py
（双线对比复盘请用 ops/review_accounts.py --research ... --live ...）
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

QUANT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QUANT / "contracts"))
sys.path.insert(0, str(QUANT / "ops"))
import common as C  # noqa: E402
import schemas as S  # noqa: E402

DAILY = QUANT / "data" / "nav" / "daily.csv"
FILLS = QUANT / "data" / "fills"
REPORTS = QUANT / "data" / "reports"


def load_profile(path: str | None) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.is_absolute():
        p = QUANT / path
    return yaml.safe_load(p.read_text()) or {}


def load_fills() -> pd.DataFrame:
    rows = []
    for f in sorted(FILLS.glob("????-??-??.csv")):
        if not f.with_suffix(".done").exists():
            continue
        try:
            df = S.read_csv("fills", f)
        except Exception:
            continue
        if df.empty:
            continue
        df["date"] = f.stem
        rows.append(df)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
        columns=["date", "instrument", "side", "shares", "price", "amount", "fee"])


def max_drawdown(nav: pd.Series) -> float:
    if nav.empty:
        return 0.0
    peak = nav.cummax()
    return float((nav / peak - 1).min())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=None)
    args = p.parse_args()

    profile = load_profile(args.config)
    account = C.load_account()
    if account is None or not DAILY.exists():
        C.alert("WARN", "暂无实盘账户或净值记录，无法生成复盘")
        return 1

    d = S.read_csv("daily", DAILY).sort_values("date").reset_index(drop=True)
    fills = load_fills()
    start_cap = float(account["start_capital"])
    last = d.iloc[-1]
    n = len(d)
    cum_ret = float(last["nav"]) / start_cap - 1
    cum_excess = float(np.prod(1 + d["excess_ret"].values) - 1)
    ann = 244
    ann_excess = (1 + cum_excess) ** (ann / n) - 1 if n else float("nan")
    avg_cash_ratio = float((d["cash"] / d["nav"]).mean()) if n else 0.0
    avg_pos = float(d["n_pos"].mean()) if n else 0.0
    avg_turnover = float(d["turnover"].mean()) if n else 0.0
    fee = float(fills["fee"].sum()) if len(fills) else 0.0
    traded = float(fills["amount"].sum()) if len(fills) else 0.0
    fee_ratio = fee / traded if traded else 0.0
    n_buy = int((fills["side"].astype(str).str.upper() == "BUY").sum()) if len(fills) else 0
    n_sell = int((fills["side"].astype(str).str.upper() == "SELL").sum()) if len(fills) else 0

    review_min_days = profile.get("account", {}).get("review_min_days", 20)
    target_min = profile.get("account", {}).get("target_min_positions")
    target_max = profile.get("account", {}).get("target_max_positions")
    backtest_excess = profile.get("review", {}).get("benchmark_annual_excess", 0.093)
    tracking_alert = profile.get("review", {}).get("tracking_deviation_alert", 0.02)
    tracking_gap = abs(ann_excess - backtest_excess) if ann_excess == ann_excess else float("nan")

    alerts = []
    if C.ALERT_LOG.exists():
        days = set(d["date"].astype(str))
        alerts = [ln for ln in C.ALERT_LOG.read_text().splitlines()
                  if any(day in ln for day in days) and " INFO " not in ln][-20:]

    pos_warning = ""
    if target_min and avg_pos < target_min:
        pos_warning = (
            f"- 持仓均值 {avg_pos:.1f} 低于目标下限 {target_min}，多半是 1000 元账户受"
            " A 股 100 股整手与股价约束影响；应记录为资金规模约束，不建议为凑数量牺牲信号质量。"
        )

    enough = n >= review_min_days
    conclusion = "样本不足，先验证流程与数据质量"
    if enough and tracking_gap == tracking_gap:
        conclusion = "跟踪偏差达标" if tracking_gap <= tracking_alert else "跟踪偏差超阈值，需复核策略/执行"

    lines = [
        f"# 小资金实盘复盘 {dt.date.today():%Y-%m-%d}",
        "",
        f"- 账户: {profile.get('account', {}).get('name', 'default')}",
        f"- 运行区间: {d['date'].iloc[0]} ~ {d['date'].iloc[-1]}，共 {n} 个交易日",
        f"- 初始资金: {start_cap:,.2f}；最新净值: {float(last['nav']):,.2f}",
        f"- 结论: **{conclusion}**",
        "",
        "## 收益与跟踪",
        f"- 累计收益: {cum_ret:+.2%}；累计超额: {cum_excess:+.2%}",
        f"- 年化超额(折算): {ann_excess:+.2%}；回测对照: {backtest_excess:+.2%}；"
        f"偏差: {tracking_gap:.2%}" if ann_excess == ann_excess else "- 年化超额: 样本不足",
        f"- 最大回撤: {max_drawdown(d['nav']):.2%}",
        "",
        "## 执行质量",
        f"- 平均持仓只数: {avg_pos:.1f}"
        + (f"（目标 {target_min}-{target_max} 只）" if target_min else ""),
        f"- 平均现金占比: {avg_cash_ratio:.1%}；平均换手: {avg_turnover:.1%}",
        f"- 成交笔数: 买入 {n_buy} / 卖出 {n_sell}；成交额 {traded:,.2f}；费用 {fee:,.2f}"
        f"（费用/成交额 {fee_ratio:.2%}）",
    ]
    if pos_warning:
        lines += ["", "## 小资金约束提示", pos_warning]
    lines += ["", "## 近日日净值"]
    lines += ["| 日期 | 净值 | 当日收益 | 超额 | 持仓数 | 现金占比 | 换手 |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for r in d.tail(10).itertuples():
        lines.append(
            f"| {r.date} | {r.nav:,.2f} | {r.daily_ret:+.2%} | {r.excess_ret:+.2%} | "
            f"{int(r.n_pos)} | {r.cash / r.nav:.1%} | {r.turnover:.1%} |"
        )
    lines += ["", "## 告警摘要"]
    lines += (["```"] + alerts + ["```"]) if alerts else ["无"]

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"live_review_{dt.date.today():%Y-%m-%d}.md"
    out.write_text("\n".join(lines))
    print("\n".join(lines))
    print(f"\n[OK] 复盘报告 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
