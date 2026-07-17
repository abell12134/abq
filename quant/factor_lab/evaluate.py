"""阶段3 因子评估：用 Qlib 在全 A（默认）上计算因子的各项准入指标。

指标（防前视：因子值只用当日及之前数据，标签为前向收益）：
  - rank_ic / icir         样本内 Rank IC 均值与 ICIR
  - oos_rank_ic / oos_icir 样本外（2024 至今）Rank IC（关卡3 样本外验证）
  - turnover               因子 TopK 集合的日均换手（换手过高的高 IC 因子要警惕）
  - max_corr               与现有因子库的最大 |相关|（关卡2 去重）
  - incr_ic_gain           叠加到基线信号后的样本外 Rank IC 增量（"提升组合"证据）
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
QUANT = HERE.parent
# 5 日前向开盘收益，与研究层基线标签一致
LABEL = "Ref($open,-6)/Ref($open,-1)-1"
PROVIDER = "~/.qlib/qlib_data/cn_data"


def _rank_within_date(s: pd.Series) -> pd.Series:
    return s.groupby(level="datetime").rank(pct=True)


class Evaluator:
    def __init__(self, market="all",
                 is_start="2019-01-01", is_end="2023-12-31",
                 oos_start="2024-01-01", oos_end="2026-07-16"):
        import qlib
        from qlib.data import D
        qlib.init(provider_uri=str(Path(PROVIDER).expanduser()), region="cn")
        self.D = D
        self.inst = D.instruments(market=market)
        self.is_p = (is_start, is_end)
        self.oos_p = (oos_start, oos_end)
        self.start, self.end = is_start, oos_end
        lab = self._feat([LABEL]).iloc[:, 0]
        self.label = lab.rename("y")
        self.base = self._load_baseline()

    # ----- qlib 取数 -----
    def _feat(self, exprs: list[str]) -> pd.DataFrame:
        df = self.D.features(self.inst, exprs, start_time=self.start, end_time=self.end)
        df.columns = [f"c{i}" for i in range(len(exprs))]
        return df.replace([np.inf, -np.inf], np.nan)

    def factor(self, expr: str) -> pd.Series:
        return self._feat([expr]).iloc[:, 0].rename("f")

    def _load_baseline(self) -> pd.Series | None:
        f = QUANT / "data" / "signals" / "latest_pred.csv"
        if not f.exists():
            return None
        df = pd.read_csv(f, parse_dates=["datetime"])
        return df.set_index(["instrument", "datetime"])["score"].rename("base")

    # ----- 指标 -----
    def _slice(self, s: pd.Series, period) -> pd.Series:
        d = s.index.get_level_values("datetime")
        return s[(d >= period[0]) & (d <= period[1])]

    def rank_ic(self, f: pd.Series, period) -> tuple[float, float]:
        d = pd.concat([f, self.label], axis=1).dropna()
        d = self._slice(d, period)
        if d.empty:
            return float("nan"), float("nan")
        ic = d.groupby(level="datetime").apply(
            lambda g: g["f"].corr(g["y"], method="spearman") if len(g) > 5 else np.nan
        ).dropna()
        if ic.empty:
            return float("nan"), float("nan")
        return float(ic.mean()), float(ic.mean() / ic.std()) if ic.std() else float("nan")

    def turnover(self, f: pd.Series, topk=50, period=None) -> float:
        period = period or self.is_p
        fs = self._slice(f.dropna(), period)
        wide = fs.unstack("instrument")
        rank = wide.rank(axis=1, ascending=False)        # 1 = 因子值最大
        mem = rank <= topk                               # TopK 成员
        entered = mem & (~mem.shift(1, fill_value=False))  # 当日新进入 TopK 的只数
        churn = entered.sum(axis=1) / topk
        return float(churn.iloc[1:].mean()) if len(churn) > 1 else float("nan")

    def corr_to_lib(self, f: pd.Series, lib_panel: dict[str, pd.Series]) -> tuple[float, str]:
        """与库内每个因子的截面秩相关（pooled），返回 (最大|corr|, 最相关因子名)。"""
        fr = _rank_within_date(self._slice(f, self.is_p)).rename("f")
        best, who = 0.0, ""
        for name, g in lib_panel.items():
            gr = _rank_within_date(self._slice(g, self.is_p)).rename("g")
            d = pd.concat([fr, gr], axis=1).dropna()
            if len(d) < 100:
                continue
            c = abs(d["f"].corr(d["g"]))
            if c > best:
                best, who = c, name
        return best, who

    def incremental_gain(self, f: pd.Series, sign: float = 1.0) -> float:
        """基线信号 vs 基线+标准化因子 的样本外 Rank IC 增量。

        sign：因子按其样本内 IC 方向对齐（负 IC 因子取反后再叠加），
        否则方向不对的因子会被误判为无价值。
        """
        if self.base is None:
            return float("nan")
        base = self._slice(self.base, self.oos_p)
        fac = self._slice(f, self.oos_p)
        df = pd.concat([base, fac, self.label], axis=1).dropna()
        if df.empty:
            return float("nan")

        def z(col):
            g = df[col].groupby(level="datetime")
            return (df[col] - g.transform("mean")) / (g.transform("std") + 1e-12)

        df["comb"] = z("base") + (1.0 if sign >= 0 else -1.0) * z("f")

        def ic(col):
            return df.groupby(level="datetime").apply(
                lambda x: x[col].corr(x["y"], method="spearman")).dropna().mean()

        return float(ic("comb") - ic("base"))

    # ----- 因子库组合的样本外增量（"提升组合 IR"的证据）-----
    def _z_within_date(self, s: pd.Series) -> pd.Series:
        g = s.groupby(level="datetime")
        return (s - g.transform("mean")) / (g.transform("std") + 1e-12)

    def make_composite(self, signed: dict[str, tuple], period) -> pd.Series:
        """把多个因子按 IC 方向标准化后等权合成一个组合打分信号。

        signed: {name: (series, sign)}，sign 为该因子样本内 IC 方向。
        """
        acc = None
        for s, sign in signed.values():
            z = self._z_within_date(self._slice(s, period)) * (1.0 if sign >= 0 else -1.0)
            acc = z if acc is None else acc.add(z, fill_value=0.0)
        return acc

    def oos_ic(self, signal: pd.Series) -> float:
        d = pd.concat([signal.rename("f"), self.label], axis=1).dropna()
        d = self._slice(d, self.oos_p)
        if d.empty:
            return float("nan")
        ic = d.groupby(level="datetime").apply(
            lambda g: g["f"].corr(g["y"], method="spearman") if len(g) > 5 else np.nan
        ).dropna()
        return float(ic.mean()) if len(ic) else float("nan")

    def incremental_vs_composite(self, f: pd.Series, sign: float,
                                 base_signal: pd.Series, base_ic: float) -> float:
        """把新因子加入"现有因子库组合"后，样本外组合 Rank IC 的增量。"""
        z = self._z_within_date(self._slice(f, self.oos_p)) * (1.0 if sign >= 0 else -1.0)
        comb = base_signal.add(z, fill_value=0.0)
        return float(self.oos_ic(comb) - base_ic)

    def evaluate(self, expr: str, lib_panel: dict[str, pd.Series]) -> dict:
        f = self.factor(expr)
        ic, icir = self.rank_ic(f, self.is_p)
        oic, oicir = self.rank_ic(f, self.oos_p)
        corr, who = self.corr_to_lib(f, lib_panel)
        return {
            "rank_ic": round(ic, 4), "icir": round(icir, 4),
            "oos_rank_ic": round(oic, 4), "oos_icir": round(oicir, 4),
            "turnover": round(self.turnover(f), 4),
            "max_corr": round(corr, 4), "corr_with": who,
            "incr_ic_gain": round(self.incremental_gain(f, np.sign(ic) if ic == ic else 1.0), 5),
        }

    def lib_panel(self, exprs: dict[str, str]) -> dict[str, pd.Series]:
        return {n: self.factor(e) for n, e in exprs.items()}
