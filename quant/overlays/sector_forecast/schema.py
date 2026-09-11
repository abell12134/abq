"""板块预测口径（第一天冻结）与纯函数：选股、证据、shadow 门、持仓对齐。

结算口径（改动会污染历史可比性，谨慎）：
  对象      申万一级行业（信号池内等权，家数 < MIN_STOCKS 不发）
  基准      中证500 SH000905
  窗口      T 日收盘后发出；从下一交易日 T+1 起算 HORIZON 个交易日
  hit       窗口累计行业等权收益 − 基准累计收益 > 0
  动作      每期限最多 TOP_K 条 up + TOP_K 条 avoid
  两条 claim  industry_excess_10d / industry_excess_20d

成绩单：OOS 命中率须优于「当日涨幅榜 TopK」才进主区；否则 shadow。
题材/概念、舆情只做证据，不进主成绩单，也不改排名。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

HORIZON_10 = 10
HORIZON_20 = 20
HORIZONS = (HORIZON_10, HORIZON_20)
BENCHMARK = "SH000905"
BENCHMARK_NAME = "中证500"
MIN_STOCKS = 3
TOP_K = 3
CLAIM_10 = "industry_excess_10d"
CLAIM_20 = "industry_excess_20d"
UNIVERSE = "pool_sw_l1"
MODEL_VERSION = "v1"
CALIBER = "sector_excess_v1"

# OOS 门：样本不足或未优于追热度 → shadow
MIN_OOS_DAYS = 40
CHASE_MARGIN = 0.01          # 模型命中须至少高过追涨幅榜这么多
LOOKBACK_DAYS = 420          # 特征窗口（含滚动 20 + 标签 20）
OOS_DAYS = 80
FEATURE_WARMUP = 20

FEATURE_COLS = [
    "ret_1d", "ret_5d", "ret_10d", "ret_20d",
    "rs_5d", "rs_10d", "rs_20d",
    "accel",
    "pct_up", "limit_up_share",
    "n_stocks",
    "ret_std",
    "vol_20",
    "crowded",
    "mkt_eq_ret", "mkt_pct_up", "mkt_limit_up_share", "mkt_temp", "mkt_ret_5d",
]

DISCLAIMER = (
    "研究/学习用途，不构成投资建议。对象为信号池内申万一级等权，"
    "不是全市场行业指数；禁止解读为「必涨」。"
)

TILT_NOTE = "舆情命中与东财概念热度为证据倾斜，未纳入历史成绩单"
LLM_NOTE = "LLM 只解释已选出的候选，未纳入历史成绩单，不能改排名或概率"


def claim_type_for(horizon: int) -> str:
    if horizon == HORIZON_10:
        return CLAIM_10
    if horizon == HORIZON_20:
        return CLAIM_20
    raise ValueError(f"不支持的期限 {horizon}")


def window_end(cal: list[str], pred_day: str, horizon: int) -> str | None:
    """T 日预测 → 窗口最后一天（T 之后第 horizon 个交易日）。"""
    if pred_day not in cal:
        return None
    i = cal.index(pred_day)
    j = i + horizon
    if j >= len(cal):
        return None
    return cal[j]


def window_days(cal: list[str], pred_day: str, horizon: int) -> list[str]:
    """T+1 … T+horizon（不含预测日 T）。"""
    if pred_day not in cal:
        return []
    i = cal.index(pred_day)
    return cal[i + 1: i + 1 + horizon]


def pick_claims(rows: list[dict[str, Any]], *, top_k: int = TOP_K) -> list[dict[str, Any]]:
    """按 p_beat 降序取 TopK up 与末尾 TopK avoid；行业不重叠。"""
    eligible = [
        r for r in rows
        if (r.get("n_stocks") or 0) >= MIN_STOCKS
        and r.get("p_beat") is not None
        and r.get("industry") and r["industry"] != "未知"
    ]
    eligible.sort(key=lambda r: (-float(r["p_beat"]), -float(r.get("expected_excess") or 0)))
    ups = eligible[:top_k]
    up_inds = {r["industry"] for r in ups}
    avoids: list[dict[str, Any]] = []
    for r in reversed(eligible):
        if r["industry"] in up_inds:
            continue
        avoids.append(r)
        if len(avoids) >= top_k:
            break
    out: list[dict[str, Any]] = []
    for i, r in enumerate(ups, 1):
        item = dict(r)
        item["action"] = "up"
        item["rank"] = i
        out.append(item)
    for i, r in enumerate(avoids, 1):
        item = dict(r)
        item["action"] = "avoid"
        item["rank"] = i
        out.append(item)
    return out


def evidence_lines(row: dict[str, Any], limit: int = 3) -> list[str]:
    """由特征生成可核对的中文证据，不编造数字。"""
    lines: list[str] = []

    def _pct(key: str) -> str | None:
        v = row.get(key)
        if v is None:
            return None
        return f"{float(v) * 100:+.1f}%"

    r5, r20, accel = row.get("ret_5d"), row.get("ret_20d"), row.get("accel")
    if r5 is not None and r20 is not None and accel is not None:
        if float(accel) > 0.005 and float(r20) < 0:
            lines.append(f"短窗转强、长窗仍弱（5日 {_pct('ret_5d')} / 20日 {_pct('ret_20d')}），偏早期切换")
        elif float(r5) > 0.04 and float(r20) > 0.06:
            lines.append(f"短长窗均已上涨（5日 {_pct('ret_5d')} / 20日 {_pct('ret_20d')}），存在拥挤")
        else:
            lines.append(f"池内等权 5日 {_pct('ret_5d')}、20日 {_pct('ret_20d')}")
    rs10 = row.get("rs_10d")
    if rs10 is not None:
        side = "占优" if float(rs10) > 0 else "落后"
        lines.append(f"近10日相对{BENCHMARK_NAME}{side} {_pct('rs_10d')}")
    lu, pu = row.get("limit_up_share"), row.get("pct_up")
    if lu is not None and pu is not None:
        lines.append(f"广度：上涨占比 {float(pu):.0%}、涨停占比 {float(lu):.0%}（{int(row.get('n_stocks') or 0)} 只）")
    crowded = row.get("crowded")
    if crowded is not None and float(crowded) > 0.05 and not any("拥挤" in x for x in lines):
        lines.append(f"拥挤度（近5日已涨幅）{float(crowded) * 100:.1f}%")
    return lines[:limit]


def shadow_gate(*, model_hit: float | None, chase_hit: float | None,
                oos_days: int, min_days: int = MIN_OOS_DAYS,
                margin: float = CHASE_MARGIN) -> tuple[bool, str]:
    """是否只出 shadow。返回 (shadow, reason)。"""
    if oos_days < min_days:
        return True, f"OOS 仅 {oos_days} 日，不足 {min_days}，样本不够"
    if model_hit is None or chase_hit is None:
        return True, "缺少 OOS 命中率，无法对照追热度基线"
    if model_hit <= chase_hit + margin:
        return True, (
            f"OOS 命中 {model_hit:.1%} 未显著优于追今日涨幅榜 {chase_hit:.1%}"
            f"（门槛 +{margin:.0%}）"
        )
    return False, f"OOS 命中 {model_hit:.1%} 优于追涨幅榜 {chase_hit:.1%}"


def stock_bias(swing: dict | None, research: dict | None) -> str:
    """个股方向：bull / bear / mid。"""
    votes = []
    if research:
        d = research.get("merged_direction")
        if d == "up":
            votes.append("bull")
        elif d == "down":
            votes.append("bear")
        elif d == "hold":
            votes.append("mid")
    if swing:
        st = str(swing.get("result") or swing.get("state") or "")
        if st in {"hit", "holding", "triggered", "predict"}:
            votes.append("bull")
        elif st in {"stopped", "reject"}:
            votes.append("bear")
        elif st:
            votes.append("mid")
    if not votes:
        return "mid"
    if votes.count("bull") > votes.count("bear"):
        return "bull"
    if votes.count("bear") > votes.count("bull"):
        return "bear"
    return "mid"


def industry_bias(h10: dict | None, h20: dict | None) -> str:
    """行业方向：看两周优先，一月辅助。"""
    actions = []
    for c in (h10, h20):
        if c and c.get("action") in {"up", "avoid"}:
            actions.append(c["action"])
    if "up" in actions and "avoid" not in actions:
        return "up"
    if "avoid" in actions and "up" not in actions:
        return "avoid"
    if "up" in actions and "avoid" in actions:
        # 期限冲突：以 10 日为准
        a = (h10 or {}).get("action")
        if a in {"up", "avoid"}:
            return a
        return "mid"
    return "mid"


def align_stock_sector(stock: str, industry: str) -> str:
    """个股 × 行业对齐文案。"""
    if industry == "up" and stock == "bull":
        return "共振：个股与行业预测同向偏多"
    if industry == "avoid" and stock == "bull":
        return "个股逆势：票偏多但行业预测偏弱"
    if industry == "up" and stock in {"bear", "mid"}:
        return "板块有戏票一般：行业预测占优，个股方向未跟上"
    if industry == "avoid" and stock == "bear":
        return "共振：个股与行业预测同向偏空"
    if industry == "avoid" and stock == "mid":
        return "行业逆风：板块预测偏弱，个股中性"
    return "中性：行业无明确候选或个股覆盖不足"


@dataclass
class HorizonEval:
    horizon: int
    oos_days: int = 0
    model_hit: float | None = None
    model_excess: float | None = None
    chase_hit: float | None = None
    chase_excess: float | None = None
    shadow: bool = True
    shadow_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizon": self.horizon,
            "oos_days": self.oos_days,
            "model_hit": None if self.model_hit is None else round(self.model_hit, 4),
            "model_excess": None if self.model_excess is None else round(self.model_excess, 4),
            "chase_hit": None if self.chase_hit is None else round(self.chase_hit, 4),
            "chase_excess": None if self.chase_excess is None else round(self.chase_excess, 4),
            "shadow": self.shadow,
            "shadow_reason": self.shadow_reason,
        }


@dataclass
class ForecastFile:
    day: str
    shadow: bool
    shadow_reason: str
    claims: list[dict[str, Any]] = field(default_factory=list)
    by_industry: dict[str, Any] = field(default_factory=dict)
    scorecard: dict[str, Any] = field(default_factory=dict)
    model_version: str = MODEL_VERSION
    caliber: str = CALIBER
    benchmark: str = BENCHMARK
    universe: str = UNIVERSE
    tilt_note: str = TILT_NOTE
    disclaimer: str = DISCLAIMER
    status: str = "ok"
    errors: list[str] = field(default_factory=list)
    generated: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "shadow": self.shadow,
            "shadow_reason": self.shadow_reason,
            "claims": self.claims,
            "by_industry": self.by_industry,
            "scorecard": self.scorecard,
            "model_version": self.model_version,
            "caliber": self.caliber,
            "benchmark": self.benchmark,
            "universe": self.universe,
            "horizons": list(HORIZONS),
            "top_k": TOP_K,
            "tilt_note": self.tilt_note,
            "disclaimer": self.disclaimer,
            "status": self.status,
            "errors": self.errors,
            "generated": self.generated,
        }
