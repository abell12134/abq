"""阶段2 验证层：backtrader 事件驱动复演引擎（独立于 qlib 向量化回测）。

目的：用第二套独立实现 + 全量 A 股微观约束复核 qlib 回测，杜绝"纸面成交"。
强制建模：
  - T+1：当日买入不可卖（由 hold_thresh>=1 天然覆盖，并显式校验）；
  - 涨停不可买、跌停不可卖（阈值 9.5%，与 qlib limit_threshold 一致）；
  - 停牌剔除（qlib 数据中停牌表现为缺失交易日 → 重建日历后该日不可交易）；
  - 整手 100 股取整；
  - 佣金万2.5（最低5元）+ 卖出印花税 0.05% + 可配滑点；
  - 次日开盘价成交（cheat-on-open：T 日信号 → T+1 开盘执行）。

调仓逻辑严格对齐研究层 qlib TopkDropoutStrategy(method_sell=bottom, method_buy=top)
与 execution/make_trade_plan.py，确保三层口径一致。

用法：
    python replay_backtrader.py                       # 全期，滑点敏感性 0.1/0.2/0.3%
    python replay_backtrader.py --start 2024-01-02 --end 2024-03-01 --slippage 0.001
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path

os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")

import backtrader as bt
import numpy as np
import pandas as pd
import yaml

QUANT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((QUANT / "configs" / "global.yaml").read_text())
import sys
sys.path.insert(0, str(QUANT / "ops"))
from ensure_qlib_data import resolve_provider_uri  # noqa: E402
PROVIDER = resolve_provider_uri(CFG["paths"]["qlib_data"])
LOT = 100
LIMIT = 0.095  # 涨跌停阈值，与 workflow_baseline.yaml 的 limit_threshold 对齐
TRADING_DAYS = 252


# --------------------------------------------------------------------------- #
# 数据加载（一次性从 qlib 取面板，复用于多次复演）
# --------------------------------------------------------------------------- #
def load_signals(sig_path: Path) -> tuple[dict[pd.Timestamp, pd.Series], list]:
    df = pd.read_csv(sig_path, parse_dates=["datetime"])
    by_day = {d: g.set_index("instrument")["score"]
              for d, g in df.groupby("datetime")}
    return by_day, sorted(by_day)


def load_panel(instruments: list[str], start: str, end: str):
    import qlib
    from qlib.data import D
    qlib.init(provider_uri=PROVIDER, region="cn")
    fields = ["$open", "$high", "$low", "$close", "$volume"]
    panel = D.features(instruments, fields, start_time=start, end_time=end)
    panel.columns = ["open", "high", "low", "close", "volume"]
    cal = sorted(panel.index.get_level_values("datetime").unique())
    return panel, cal


def to_feed_frames(panel: pd.DataFrame, calendar: list) -> dict[str, pd.DataFrame]:
    """把面板拆成每只股票的日历对齐 DataFrame；停牌日 volume=0、价格前向填充。"""
    cal_idx = pd.DatetimeIndex(calendar)
    frames = {}
    for inst, g in panel.groupby(level="instrument"):
        d = g.droplevel("instrument").reindex(cal_idx)
        # 停牌（缺失）日：价格用前收平推、volume=0（策略据此判定不可交易）
        d["volume"] = d["volume"].fillna(0.0)
        d[["open", "high", "low", "close"]] = (
            d[["open", "high", "low", "close"]].ffill())
        d = d.dropna(subset=["close"])  # 上市前的前导缺失丢弃
        if len(d):
            frames[inst] = d
    return frames


# --------------------------------------------------------------------------- #
# A 股费用模型：佣金万2.5(最低5) + 卖出印花税 0.05%
# --------------------------------------------------------------------------- #
class AStockComm(bt.CommInfoBase):
    params = (
        ("commission", 0.00025),
        ("stamp_duty", 0.0005),
        ("min_comm", 5.0),
        ("stocklike", True),
        ("commtype", bt.CommInfoBase.COMM_PERC),
        ("percabs", True),
    )

    def _getcommission(self, size, price, pseudoexec):
        value = abs(size) * price
        comm = max(value * self.p.commission, self.p.min_comm)
        if size < 0:  # 卖出加印花税
            comm += value * self.p.stamp_duty
        return comm


# --------------------------------------------------------------------------- #
# 复演策略：对齐 TopkDropoutStrategy + hold_thresh + A 股约束
# --------------------------------------------------------------------------- #
class TopkReplay(bt.Strategy):
    params = dict(sig_by_day=None, sig_days=None, topk=50, n_drop=3,
                  hold_thresh=10, invest_ratio=0.98, veto_set=None)

    def __init__(self):
        self.entry_bar: dict = {}     # data -> 建仓时的 bar 序号（计持有天数）
        self.nav = []                 # (date, 总资产)
        self.sig_days = list(self.p.sig_days)
        self.veto_set = self.p.veto_set or set()  # {(信号日, 标的)} UMP 否决集
        self.n_limit_block = 0        # 因涨跌停被拦截的下单次数
        self.n_susp_block = 0         # 因停牌被拦截的次数
        self.n_ump_block = 0          # 因 UMP 否决被拦截的买入次数

    def _prev_signal(self, today: pd.Timestamp):
        """T 日开盘执行，使用上一交易日（已收盘）生成的信号。返回 (信号日, 分数)。"""
        import bisect
        i = bisect.bisect_left(self.sig_days, today)
        if i == 0:
            return None, None
        d = self.sig_days[i - 1]
        return d, self.p.sig_by_day[d]

    def _tradable(self, d, side: str) -> bool:
        """停牌（volume=0）与涨跌停过滤。side: 'buy'/'sell'。"""
        if d.volume[0] <= 0:
            self.n_susp_block += 1
            return False
        prev_close = d.close[-1]
        ret = d.open[0] / prev_close - 1 if prev_close > 0 else 0.0
        if side == "buy" and ret >= LIMIT:      # 涨停不可买
            self.n_limit_block += 1
            return False
        if side == "sell" and ret <= -LIMIT:    # 跌停不可卖
            self.n_limit_block += 1
            return False
        return True

    def next(self):
        # 在收盘记录净值，与基准 close-to-close 口径对齐（否则 open/close
        # 错位会虚增跟踪误差、压低超额 IR）
        today = pd.Timestamp(bt.num2date(self.datas[0].datetime[0]).date())
        self.nav.append((today, self.broker.getvalue()))

    def next_open(self):
        today = bt.num2date(self.datas[0].datetime[0])
        today = pd.Timestamp(today.date())
        sig_day, score = self._prev_signal(today)

        value = self.broker.getvalue()
        if score is None:
            return

        # 当前持仓
        held = [d for d in self.datas if self.getposition(d).size > 0]
        held_names = [d._name for d in held]
        s_held = score.reindex(held_names).dropna().sort_values(ascending=False)
        last = list(s_held.index)

        # 买入候选：未持有、按分排序（宽候选以应对停牌/涨停/凑整手顺延）
        ranked_new = score[~score.index.isin(last)].sort_values(ascending=False)
        n_buy_candi = max(self.p.n_drop + self.p.topk - len(last), 0)
        today_top = list(ranked_new.index[:n_buy_candi])

        # 卖出集合：last 中落在 (last ∪ today_top) 末 n_drop 名者（method_sell=bottom）
        comb = score.reindex(pd.Index(last).union(pd.Index(today_top))).sort_values(
            ascending=False)
        bottom = set(comb.index[-self.p.n_drop:]) if self.p.n_drop > 0 else set()
        sell_candi = [n for n in last if n in bottom]

        cur_bar = len(self)
        # 1) 卖出（hold_thresh + T+1 + 跌停 + 停牌 过滤）
        n_sold = 0
        for name in sell_candi:
            d = self.getdatabyname(name)
            held_bars = cur_bar - self.entry_bar.get(d, cur_bar)
            if held_bars < max(self.p.hold_thresh, 1):  # 含 T+1
                continue
            if not self._tradable(d, "sell"):
                continue
            self.close(data=d)
            self.entry_bar.pop(d, None)
            n_sold += 1

        # 2) 买入补足到 topk（顺位、整手、单票上限、涨停/停牌过滤）
        n_buy = max(n_sold + self.p.topk - len(last), 0)
        weight = min(1.0 / self.p.topk, CFG["strategy"]["max_weight"])
        per_name = value * self.p.invest_ratio * weight
        n_bought = 0
        for name in ranked_new.index:
            if n_bought >= n_buy:
                break
            if (sig_day, name) in self.veto_set:   # UMP 裁判否决
                self.n_ump_block += 1
                continue
            d = self.getdatabyname(name)
            if self.getposition(d).size > 0:
                continue
            if not self._tradable(d, "buy"):
                continue
            px = d.open[0]
            shares = int(per_name / px / LOT) * LOT
            if shares <= 0:
                continue
            self.buy(data=d, size=shares)
            self.entry_bar[d] = cur_bar
            n_bought += 1


def run_replay(frames, calendar, sig_by_day, sig_days, slippage,
               start_cash=1_000_000, veto_set=None):
    cerebro = bt.Cerebro(cheat_on_open=True, stdstats=False)
    cerebro.broker.setcash(start_cash)
    cerebro.broker.addcommissioninfo(AStockComm())
    if slippage > 0:
        cerebro.broker.set_slippage_perc(
            slippage, slip_open=True, slip_match=True, slip_out=False)
    for inst, df in frames.items():
        feed = bt.feeds.PandasData(dataname=df, openinterest=None)
        cerebro.adddata(feed, name=inst)
    cerebro.addstrategy(TopkReplay, sig_by_day=sig_by_day, sig_days=sig_days,
                        topk=CFG["strategy"]["topk"], n_drop=CFG["strategy"]["n_drop"],
                        hold_thresh=CFG["strategy"].get("hold_thresh", 10),
                        veto_set=veto_set)
    strat = cerebro.run()[0]
    nav = pd.Series({d: v for d, v in strat.nav}).sort_index()
    return nav, strat.n_limit_block, strat.n_susp_block, strat.n_ump_block


# --------------------------------------------------------------------------- #
# 指标：与 qlib excess_return_with_cost 口径对齐（日超额 mean*252、IR=mean/std*sqrt252）
# --------------------------------------------------------------------------- #
def excess_metrics(nav: pd.Series, bench_close: pd.Series) -> dict:
    r_p = nav.pct_change().dropna()
    r_b = bench_close.reindex(nav.index).pct_change().reindex(r_p.index)
    ex = (r_p - r_b).dropna()
    ann = ex.mean() * TRADING_DAYS
    ir = ex.mean() / ex.std() * np.sqrt(TRADING_DAYS) if ex.std() > 0 else float("nan")
    cum = (1 + r_p).prod() - 1
    cum_b = (1 + r_b.reindex(r_p.index)).prod() - 1
    # 超额最大回撤（累计超额净值口径）
    ex_nav = (1 + ex).cumprod()
    mdd = (ex_nav / ex_nav.cummax() - 1).min()
    return dict(ann_excess=ann, ir=ir, total_ret=cum, bench_ret=cum_b, ex_mdd=mdd)


def _read_metric(run_dir: Path, name: str):
    f = run_dir / "metrics" / name
    if not f.exists():
        return None
    return float(f.read_text().strip().splitlines()[-1].split()[1])


def qlib_reference() -> dict:
    """直接从 research/mlruns 读取最近一次基线 recorder 的 excess_return 指标。

    不走 qlib R（其 tracking uri 依赖 cwd），直接扫描 mlruns 文件更稳。
    """
    key = "1day.excess_return_with_cost.annualized_return"
    mlruns = QUANT / "research" / "mlruns"
    best = None
    for meta in mlruns.glob("*/*/meta.yaml"):
        run_dir = meta.parent
        if _read_metric(run_dir, key) is None:
            continue
        try:
            end = float(yaml.safe_load(meta.read_text()).get("end_time") or 0)
        except Exception:
            end = 0
        if best is None or end > best[0]:
            best = (end, run_dir)
    if best is None:
        return {"ann_excess": None, "ir": None, "rec_id": "N/A (无 mlruns 记录)"}
    run_dir = best[1]
    return {
        "ann_excess": _read_metric(run_dir, key),
        "ir": _read_metric(run_dir, "1day.excess_return_with_cost.information_ratio"),
        "rec_id": run_dir.name,
    }


def render_report(rows: list[dict], qref: dict, period: tuple, blocks: dict) -> str:
    s, e = period
    def f(x, p="{:.2%}"):
        return p.format(x) if x is not None else "N/A"
    lines = [
        "# 阶段2 验证报告：backtrader 独立复演 + 滑点敏感性",
        "",
        f"- 生成时间: {dt.datetime.now():%Y-%m-%d %H:%M}",
        f"- 复演区间: {s} ~ {e}（次日开盘成交，T+1/涨跌停/停牌/整手/费税全建模）",
        f"- 策略: TopK{CFG['strategy']['topk']} / n_drop{CFG['strategy']['n_drop']} / "
        f"hold_thresh{CFG['strategy'].get('hold_thresh', 10)}（与研究层一致）",
        f"- qlib 基线参考(recorder {qref['rec_id']}): "
        f"年化超额 {f(qref['ann_excess'])}, IR {f(qref['ir'], '{:.4f}')}",
        "",
        "## 滑点敏感性（相对基准 SH000905 的超额）",
        "| 滑点 | 年化超额 | 超额IR | 超额最大回撤 | 与qlib年化差异 |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        diff = (r["ann_excess"] - qref["ann_excess"]
                if qref["ann_excess"] is not None else None)
        lines.append(
            f"| {r['slippage']:.1%} | {f(r['ann_excess'])} | {r['ir']:.4f} | "
            f"{f(r['ex_mdd'])} | {f(diff, '{:+.2%}') if diff is not None else 'N/A'} |")
    base = next((r for r in rows if abs(r["slippage"] - 0.001) < 1e-9), rows[0])
    s02 = next((r for r in rows if abs(r["slippage"] - 0.002) < 1e-9), None)
    diff_base = (base["ann_excess"] - qref["ann_excess"]
                 if qref["ann_excess"] is not None else None)
    lines += [
        "",
        f"- 涨跌停/停牌拦截下单次数: 涨跌停 {blocks['limit']}，停牌 {blocks['susp']}",
        "",
        "## 验收判定（阶段2门槛）",
        f"- 复演与 qlib 年化超额差异 ≤3pct: "
        + (f"{f(diff_base, '{:+.2%}')} → "
           + ("**通过**" if diff_base is not None and abs(diff_base) <= 0.03
              else "**待复核**") if diff_base is not None else "qlib 参考缺失"),
        f"- 0.2% 滑点下年化超额 ≥5%: "
        + (f"{f(s02['ann_excess'])} → "
           + ("**通过**" if s02["ann_excess"] >= 0.05 else "**未通过**")
           if s02 else "N/A"),
    ]
    return "\n".join(lines)


def build_veto_set(uni, sig_by_day, sig_days, start, end):
    """用带回看的特征面板 + 已训练的 UMP 模型，产出 (信号日, 标的) 否决集。"""
    from ump_judge import FeatureBuilder, Judge
    from qlib.data import D
    lb = (pd.Timestamp(start) - pd.Timedelta(days=130)).strftime("%Y-%m-%d")
    fpanel, _ = load_panel(uni, lb, end)
    fbench = D.features(["SH000905"], ["$close"], start_time=lb,
                        end_time=end)["$close"].droplevel("instrument")
    fb = FeatureBuilder(fpanel, fbench)
    judge = Judge.load()
    return judge.veto_set(sig_by_day, sig_days, fb)


def render_ab(no_ump: dict, ump: dict, qref: dict, period: tuple, slippage: float,
              n_ump_block: int) -> str:
    s, e = period
    def f(x, p="{:.2%}"):
        return p.format(x) if x is not None else "N/A"
    def row(tag, m):
        return (f"| {tag} | {f(m['ann_excess'])} | {m['ir']:.4f} | {f(m['ex_mdd'])} "
                f"| {f(m['total_ret'])} |")
    d_ir = ump["ir"] - no_ump["ir"]
    d_mdd = ump["ex_mdd"] - no_ump["ex_mdd"]
    return "\n".join([
        "# 阶段2 UMP 接入复演 A/B 报告（同区间、同滑点）",
        "",
        f"- 生成时间: {dt.datetime.now():%Y-%m-%d %H:%M}",
        f"- 区间: {s} ~ {e}（UMP 样本外应用区间）；滑点: {slippage:.1%}",
        f"- UMP 否决买入次数: {n_ump_block}",
        f"- qlib 基线参考: 年化超额 {f(qref['ann_excess'])}, IR {f(qref['ir'], '{:.4f}')}",
        "",
        "| 方案 | 年化超额 | 超额IR | 超额最大回撤 | 总收益 |",
        "|---|---|---|---|---|",
        row("无 UMP", no_ump),
        row("有 UMP", ump),
        "",
        f"- UMP 带来的变化：超额IR {d_ir:+.4f}，超额最大回撤 {d_mdd:+.2%}",
        f"- 结论: {'**UMP 改善风险收益比**' if (d_ir > 0 or d_mdd > 0) else '**UMP 未见改善，需调阈值/特征**'}"
        "（UMP 定位是砍尾部、降回撤，年化可能略降属正常）",
    ])


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2024-01-02")
    p.add_argument("--end", default="2026-06-11")
    p.add_argument("--slippage", type=float, default=None,
                   help="只跑单一滑点；缺省则跑 0.001/0.002/0.003 敏感性")
    p.add_argument("--ump", action="store_true", help="在复演中启用 UMP 裁判否决")
    p.add_argument("--compare-ump", action="store_true",
                   help="同区间同滑点跑 有/无 UMP 的 A/B 对比（默认滑点 0.1%）")
    p.add_argument("--signals", default=str(QUANT / "data" / "signals" / "latest_pred.csv"))
    args = p.parse_args()

    sig_by_day, sig_days = load_signals(Path(args.signals))
    sig_days = [d for d in sig_days if args.start <= d.strftime("%Y-%m-%d") <= args.end]
    uni = sorted({i for d in sig_days for i in sig_by_day[d].index})
    print(f"[1/3] 加载面板：{len(uni)} 只标的 {args.start}~{args.end}")
    panel, calendar = load_panel(uni, args.start, args.end)
    frames = to_feed_frames(panel, calendar)
    print(f"      可用 feed：{len(frames)} 只；交易日 {len(calendar)} 天")

    from qlib.data import D
    bench = D.features(["SH000905"], ["$close"], start_time=args.start,
                       end_time=args.end)["$close"].droplevel("instrument")
    qref = qlib_reference()

    veto = None
    if args.ump or args.compare_ump:
        print("[*] 构建 UMP 否决集（带回看特征 + 已训练模型）")
        veto = build_veto_set(uni, sig_by_day, sig_days, args.start, args.end)
        print(f"    否决集大小：{len(veto)} 个 (信号日,标的)")

    if args.compare_ump:
        sl = args.slippage if args.slippage is not None else 0.001
        print(f"[2/3] A/B 复演 slippage={sl:.1%}：无 UMP")
        nav0, *_ = run_replay(frames, calendar, sig_by_day, sig_days, sl)
        m0 = excess_metrics(nav0, bench)
        print(f"      无UMP 年化超额 {m0['ann_excess']:.2%} IR {m0['ir']:.3f} "
              f"超额MDD {m0['ex_mdd']:.2%}")
        print(f"      有 UMP")
        nav1, _, _, nump = run_replay(frames, calendar, sig_by_day, sig_days, sl,
                                      veto_set=veto)
        m1 = excess_metrics(nav1, bench)
        print(f"      有UMP 年化超额 {m1['ann_excess']:.2%} IR {m1['ir']:.3f} "
              f"超额MDD {m1['ex_mdd']:.2%}  否决 {nump} 次")
        report = render_ab(m0, m1, qref, (args.start, args.end), sl, nump)
        out = QUANT / "data" / "reports" / f"ump_replay_ab_{dt.date.today():%Y%m%d}.md"
        out.write_text(report)
        print("\n" + report + f"\n\n[OK] 报告已写入 {out}")
        return 0

    slippages = [args.slippage] if args.slippage is not None else [0.001, 0.002, 0.003]
    rows, blocks = [], {"limit": 0, "susp": 0}
    for sl in slippages:
        print(f"[2/3] 复演 slippage={sl:.1%}{' +UMP' if veto is not None else ''}")
        nav, nlim, nsusp, nump = run_replay(frames, calendar, sig_by_day, sig_days, sl,
                                            veto_set=veto)
        m = excess_metrics(nav, bench)
        m["slippage"] = sl
        rows.append(m)
        blocks = {"limit": nlim, "susp": nsusp}
        print(f"      年化超额 {m['ann_excess']:.2%}  IR {m['ir']:.3f}  "
              f"超额MDD {m['ex_mdd']:.2%}  总收益 {m['total_ret']:.2%}")

    print("[3/3] 出报告")
    report = render_report(rows, qref, (args.start, args.end), blocks)
    tag = "_ump" if veto is not None else ""
    out = QUANT / "data" / "reports" / f"validation{tag}_{dt.date.today():%Y%m%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print("\n" + report)
    print(f"\n[OK] 报告已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
