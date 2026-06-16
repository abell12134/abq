"""阶段2 UMP 裁判模型（借鉴 abu 的"拦截器/裁判"思想，自研实现）。

思路：用历史买入候选的"入场时特征"（市场状态 / 个股波动 / 信号分位 等）训练
一个分类器，预测该笔交易在持有期内是否会跑输基准（loser）。每日选股时对买入
候选做最后否决——砍掉模型判定胜率最差的尾部交易，目标是提升组合的风险收益比。

与 abu 的关系：只移植"用历史交易特征训练裁判、对信号二次否决"的思想，不引入
abu 运行时依赖；模型本身用 LightGBM 自研。

防前视：
  - 入场特征只用信号日（D_sig，已收盘）及之前的数据；
  - 训练/评估按时间切分（cutoff 之前训练、之后评估），裁判从不见未来；
  - 否决阈值只在训练集上标定。

用法：
    python ump_judge.py train                      # 构建样本→训练→评估→存模型+报告
    python ump_judge.py train --cutoff 2025-07-01 --veto-frac 0.2 --horizon 10
产物：
    validation/ump_model.pkl                       # 模型 + 特征列 + 阈值 + 元信息
    data/reports/ump_YYYYMMDD.md                   # 否决有效性评估报告
"""

from __future__ import annotations

import argparse
import datetime as dt
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_backtrader import CFG, QUANT, load_panel, load_signals  # noqa: E402

MODEL_PATH = Path(__file__).resolve().parent / "ump_model.pkl"
FEATURES = [
    "score", "score_z", "rank_pct",          # 信号分位
    "mom5", "mom20", "mom60", "rel20",        # 个股动量 / 相对强弱
    "vol20", "rng20", "liq",                  # 个股波动 / 位置 / 流动性
    "mkt5", "mkt20", "mkt_vol20",             # 市场状态
]
CAND_PER_DAY = 60   # 每日取分数最高的若干只作为"买入候选"样本（topk=50 + 缓冲）


# --------------------------------------------------------------------------- #
# 特征工程：返回 (datetime, instrument) 索引的特征面板，全部"截至当日收盘"可知
# --------------------------------------------------------------------------- #
class FeatureBuilder:
    def __init__(self, panel: pd.DataFrame, bench_close: pd.Series):
        self.panel = panel
        self.bench = bench_close.sort_index()
        self.feat = self._build()

    def _build(self) -> pd.DataFrame:
        close = self.panel["close"].unstack("instrument").sort_index()
        vol = self.panel["volume"].unstack("instrument").sort_index()
        ret = close.pct_change(fill_method=None)

        mom5 = close / close.shift(5) - 1
        mom20 = close / close.shift(20) - 1
        mom60 = close / close.shift(60) - 1
        vol20 = ret.rolling(20).std()
        rng20 = close / close.rolling(20).max() - 1          # ≤0，离 20 日高点的距离
        liq = vol.rolling(5).mean() / (vol.rolling(60).mean() + 1e-9)  # 量能放大

        b = self.bench.reindex(close.index).ffill()
        bret = b.pct_change(fill_method=None)
        mkt5 = (b / b.shift(5) - 1)
        mkt20 = (b / b.shift(20) - 1)
        mkt_vol20 = bret.rolling(20).std()
        rel20 = mom20.sub(mkt20, axis=0)                     # 个股相对市场强弱

        def melt(df, name):
            s = df.stack()
            s.name = name
            return s

        feat = pd.concat([
            melt(mom5, "mom5"), melt(mom20, "mom20"), melt(mom60, "mom60"),
            melt(vol20, "vol20"), melt(rng20, "rng20"), melt(liq, "liq"),
            melt(rel20, "rel20"),
        ], axis=1)
        # 市场特征按日期广播
        feat = feat.reset_index()
        feat.columns = ["datetime", "instrument"] + list(feat.columns[2:])
        mkt = pd.DataFrame({"mkt5": mkt5, "mkt20": mkt20, "mkt_vol20": mkt_vol20})
        feat = feat.merge(mkt, left_on="datetime", right_index=True, how="left")
        return feat.set_index(["datetime", "instrument"]).sort_index()


# --------------------------------------------------------------------------- #
# 交易样本：每个信号日取分数最高的候选，加信号分位特征 + 前向 horizon 期标签
# --------------------------------------------------------------------------- #
def build_samples(sig_by_day, sig_days, fb: FeatureBuilder, horizon: int) -> pd.DataFrame:
    close = fb.panel["close"].unstack("instrument").sort_index()
    bench = fb.bench.reindex(close.index).ffill()
    cal = list(close.index)
    pos = {d: i for i, d in enumerate(cal)}

    rows = []
    for d_sig in sig_days:
        if d_sig not in pos:
            continue
        i = pos[d_sig]
        j = i + horizon
        if j >= len(cal):
            break  # 没有前向窗口，无法打标签
        d_fwd = cal[j]
        s = sig_by_day[d_sig].dropna().sort_values(ascending=False)
        if len(s) < 10:
            continue
        top = s.head(CAND_PER_DAY)
        mean, std = s.mean(), s.std() or 1e-9
        bench_fwd = bench.loc[d_fwd] / bench.loc[d_sig] - 1
        for rk, (inst, sc) in enumerate(top.items(), start=1):
            if inst not in close.columns:
                continue
            c0, c1 = close.at[d_sig, inst], close.at[d_fwd, inst]
            if not (c0 > 0) or pd.isna(c1):
                continue
            fwd = c1 / c0 - 1
            excess = fwd - bench_fwd
            rows.append({
                "datetime": d_sig, "instrument": inst,
                "score": sc, "score_z": (sc - mean) / std,
                "rank_pct": rk / len(s),
                "fwd_excess": excess, "loser": int(excess < 0),
            })
    samp = pd.DataFrame(rows)
    # 合并行情类特征
    samp = samp.merge(fb.feat.reset_index(), on=["datetime", "instrument"], how="left")
    samp = samp.dropna(subset=FEATURES)
    return samp


# --------------------------------------------------------------------------- #
# 训练 + 评估
# --------------------------------------------------------------------------- #
def train_judge(samp: pd.DataFrame, cutoff: str, veto_frac: float):
    import lightgbm as lgb

    tr = samp[samp["datetime"] < cutoff]
    ev = samp[samp["datetime"] >= cutoff]
    if len(tr) < 500 or len(ev) < 200:
        raise RuntimeError(f"样本不足：train={len(tr)} eval={len(ev)}，请调 cutoff")

    clf = lgb.LGBMClassifier(
        objective="binary", n_estimators=300, learning_rate=0.03,
        num_leaves=31, max_depth=5, subsample=0.8, colsample_bytree=0.8,
        reg_lambda=10.0, min_child_samples=80, n_jobs=2, verbose=-1,
    )
    clf.fit(tr[FEATURES], tr["loser"])

    # 阈值：在训练集上取 (1-veto_frac) 分位，即否决预测亏损概率最高的 veto_frac
    p_tr = clf.predict_proba(tr[FEATURES])[:, 1]
    thresh = float(np.quantile(p_tr, 1 - veto_frac))

    # 评估（样本外）：被否决 vs 被保留 的胜率与平均前向超额
    p_ev = clf.predict_proba(ev[FEATURES])[:, 1]
    ev = ev.assign(p=p_ev, vetoed=p_ev >= thresh)
    metrics = _eval(ev)
    metrics.update(n_train=len(tr), n_eval=len(ev), thresh=thresh,
                   cutoff=cutoff, veto_frac=veto_frac, horizon=None)
    importance = dict(sorted(zip(FEATURES, clf.feature_importances_),
                             key=lambda x: -x[1]))
    return clf, thresh, metrics, importance


def _eval(ev: pd.DataFrame) -> dict:
    kept, veto = ev[~ev.vetoed], ev[ev.vetoed]
    def wr(df):  # win rate = 前向超额 > 0 的比例
        return float((df["fwd_excess"] > 0).mean()) if len(df) else float("nan")
    def me(df):
        return float(df["fwd_excess"].mean()) if len(df) else float("nan")
    return {
        "all_winrate": wr(ev), "all_excess": me(ev),
        "kept_winrate": wr(kept), "kept_excess": me(kept), "n_kept": len(kept),
        "veto_winrate": wr(veto), "veto_excess": me(veto), "n_veto": len(veto),
    }


# --------------------------------------------------------------------------- #
# 推理接口：供 replay_backtrader 调用，产出 (信号日, 标的) 的否决集合
# --------------------------------------------------------------------------- #
class Judge:
    def __init__(self, clf, feature_cols, thresh, horizon):
        self.clf, self.cols, self.thresh, self.horizon = clf, feature_cols, thresh, horizon

    @classmethod
    def load(cls, path: Path = MODEL_PATH):
        with open(path, "rb") as f:
            d = pickle.load(f)
        return cls(d["clf"], d["features"], d["thresh"], d["horizon"])

    def veto_set(self, sig_by_day, sig_days, fb: FeatureBuilder) -> set:
        """返回需否决的 {(信号日 Timestamp, instrument)} 集合。"""
        rows = []
        for d_sig in sig_days:
            s = sig_by_day[d_sig].dropna().sort_values(ascending=False)
            if len(s) < 10:
                continue
            mean, std = s.mean(), s.std() or 1e-9
            top = s.head(CAND_PER_DAY)
            for rk, (inst, sc) in enumerate(top.items(), start=1):
                rows.append({"datetime": d_sig, "instrument": inst, "score": sc,
                             "score_z": (sc - mean) / std, "rank_pct": rk / len(s)})
        cand = pd.DataFrame(rows).merge(fb.feat.reset_index(),
                                        on=["datetime", "instrument"],
                                        how="left").dropna(subset=self.cols)
        if cand.empty:
            return set()
        p = self.clf.predict_proba(cand[self.cols])[:, 1]
        vetoed = cand[p >= self.thresh]
        return set(zip(vetoed["datetime"], vetoed["instrument"]))


# --------------------------------------------------------------------------- #
def live_veto_instruments(day: str, score: pd.Series, lookback_days: int = 130) -> set:
    """实盘用：对给定交易日的信号，返回 UMP 建议否决的标的集合。

    无模型文件时返回空集（视为不否决），保证主流程不被阻断。
    """
    if not MODEL_PATH.exists():
        return set()
    judge = Judge.load()
    ts = pd.Timestamp(day)
    score = score.dropna().sort_values(ascending=False)
    sig_by_day, sig_days = {ts: score}, [ts]
    uni = sorted(score.index)
    load_start = (ts - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    panel, _ = load_panel(uni, load_start, day)
    from qlib.data import D
    bench = D.features(["SH000905"], ["$close"], start_time=load_start,
                       end_time=day)["$close"].droplevel("instrument")
    fb = FeatureBuilder(panel, bench)
    return {inst for (_, inst) in judge.veto_set(sig_by_day, sig_days, fb)}


def _load_inputs(start: str, end: str, lookback_days: int = 130):
    sig_path = QUANT / "data" / "signals" / "latest_pred.csv"
    sig_by_day, sig_days = load_signals(sig_path)
    sig_days = [d for d in sig_days if start <= d.strftime("%Y-%m-%d") <= end]
    uni = sorted({i for d in sig_days for i in sig_by_day[d].index})
    load_start = (pd.Timestamp(start) - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    panel, _ = load_panel(uni, load_start, end)
    from qlib.data import D
    bench = D.features(["SH000905"], ["$close"], start_time=load_start,
                       end_time=end)["$close"].droplevel("instrument")
    return sig_by_day, sig_days, panel, bench


def render_report(metrics: dict, importance: dict) -> str:
    def pct(x):
        return f"{x:.2%}" if x == x else "N/A"
    lift = metrics["kept_winrate"] - metrics["veto_winrate"]
    ok = (metrics["veto_winrate"] < metrics["all_winrate"]
          and metrics["veto_excess"] < metrics["kept_excess"])
    top_imp = " · ".join(f"{k}({v})" for k, v in list(importance.items())[:6])
    return "\n".join([
        "# 阶段2 UMP 裁判模型评估报告",
        "",
        f"- 生成时间: {dt.datetime.now():%Y-%m-%d %H:%M}",
        f"- 训练/评估时间切分: < {metrics['cutoff']} 训练，>= 评估（防前视，样本外评估）",
        f"- 样本数: 训练 {metrics['n_train']}，评估 {metrics['n_eval']}；"
        f"持有期 {metrics['horizon']} 日；否决比例(训练标定) {metrics['veto_frac']:.0%}，"
        f"概率阈值 {metrics['thresh']:.3f}",
        "",
        "## 样本外否决有效性（核心：被否决的交易应明显更差）",
        "| 分组 | 笔数 | 胜率(前向超额>0) | 平均前向超额 |",
        "|---|---|---|---|",
        f"| 全部候选 | {metrics['n_eval']} | {pct(metrics['all_winrate'])} | {pct(metrics['all_excess'])} |",
        f"| UMP 保留 | {metrics['n_kept']} | {pct(metrics['kept_winrate'])} | {pct(metrics['kept_excess'])} |",
        f"| UMP 否决 | {metrics['n_veto']} | {pct(metrics['veto_winrate'])} | {pct(metrics['veto_excess'])} |",
        "",
        f"- 保留 vs 否决 胜率差: **{lift:+.2%}**（越大越说明裁判识别出了坏交易）",
        f"- 结论: {'**有效**（被否决交易胜率与超额均更低，砍尾部成立）' if ok else '**待复核**（区分度不足，需调特征/阈值）'}",
        "",
        f"## 特征重要度 Top6\n{top_imp}",
        "",
        "> 否决集成入 `validation/replay_backtrader.py --ump` 做有/无 UMP 的 A/B 复演验证。",
    ])


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["train"])
    p.add_argument("--start", default="2024-01-02")
    p.add_argument("--end", default="2026-06-11")
    p.add_argument("--cutoff", default="2025-07-01")
    p.add_argument("--veto-frac", type=float, default=0.20)
    p.add_argument("--horizon", type=int, default=CFG["strategy"].get("hold_thresh", 10))
    args = p.parse_args()

    print(f"[1/4] 加载信号/行情/基准（含 {args.horizon} 日前向窗口与特征回看）")
    sig_by_day, sig_days, panel, bench = _load_inputs(args.start, args.end)
    print(f"[2/4] 构建特征与交易样本（每日 top{CAND_PER_DAY} 候选）")
    fb = FeatureBuilder(panel, bench)
    samp = build_samples(sig_by_day, sig_days, fb, args.horizon)
    print(f"      样本 {len(samp)} 条；整体胜率 {(samp['fwd_excess']>0).mean():.2%}")

    print(f"[3/4] 训练 UMP 并样本外评估（cutoff={args.cutoff}）")
    clf, thresh, metrics, importance = train_judge(samp, args.cutoff, args.veto_frac)
    metrics["horizon"] = args.horizon

    with open(MODEL_PATH, "wb") as f:
        pickle.dump({"clf": clf, "features": FEATURES, "thresh": thresh,
                     "horizon": args.horizon}, f)
    report = render_report(metrics, importance)
    out = QUANT / "data" / "reports" / f"ump_{dt.date.today():%Y%m%d}.md"
    out.write_text(report)
    print("[4/4] 完成\n\n" + report)
    print(f"\n[OK] 模型已存 {MODEL_PATH}；报告 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
