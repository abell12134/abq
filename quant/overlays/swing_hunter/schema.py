"""swing_hunter 输出契约：预测 JSON + 跟踪状态机。

文件：data/overlays/swing_hunter/predictions/YYYY-MM-DD.json
跟踪：data/overlays/swing_hunter/tracker/{instrument}.json

验证口径（用户决策：收盘价口径）：
  entry      = 预测日 T 的次一交易日开盘价（T+1 open，后复权）
  hit        = T+1 起 10 个交易日内，某日收盘 ≥ entry × (1 + HIT_PCT)
  stopped    = 某日收盘 ≤ entry × (1 - STOP_PCT)，且先于 hit
  expired    = 满 HORIZON_DAYS 未 hit 也未 stopped
  mfe/mae    = 期内最高/最低收盘相对 entry（辅助统计，不参与判定）
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

QUANT = Path(__file__).resolve().parents[2]
ROOT = QUANT / "data" / "overlays" / "swing_hunter"

# ---- 验证口径常数（改动会污染历史可比性，谨慎） ----
HIT_PCT = 0.10          # 达标：收盘涨幅
HIT_PCT_TIER2 = 0.15    # 第二目标档
HIT_PCT_TIER3 = 0.20    # 第三目标档
STOP_PCT = 0.05         # 止损：收盘跌幅
HORIZON_DAYS = 10       # 兑现窗口（交易日）

# ---- 候选池参数 ----
SIGNAL_TOP_N = 30       # 量化强势池：signals rank 前 N
EVENT_LOOKBACK_DAYS = 3  # 事件催化池：近 N 日 raw 舆情/公告
MAX_LLM_CALLS = 0       # 每日 LLM 深析上限；0=不截断，全部未过滤候选都跑 LLM

# 第 4 路：短线动量/突破（不依赖 LGBM Top30）
MOMENTUM_RET5_SOFT = 0.05       # ret_5d ≥ 5% 且放量
MOMENTUM_VOL_RATIO = 1.3        # 量比门槛（与 soft 联用）
MOMENTUM_RET5_HARD = 0.08       # ret_5d ≥ 8% 单独入池
MOMENTUM_SCORE_BOOST = 0.15     # 动量路规则分加成
PATTERN_SCORE_BOOST = 0.10      # live 模式相似度加成
PATTERN_SIM_THRESHOLD = 0.55    # 模式相似度门槛（0~1）
LIVE_PATTERN_STATUSES = frozenset({"live_case", "live"})

# ---- 状态机 ----
# watch（LLM 认为可观察但未达预测标准，不入跟踪）
# triggered（预测已发布，等待 T+1 开盘入场记录）
# holding（已记录入场价，跟踪中）
# hit / stopped / expired / invalid（终态）
ACTIVE_STATES = frozenset({"triggered", "holding"})
TERMINAL_STATES = frozenset({"hit", "stopped", "expired", "invalid"})

# 催化类型词典（与 prompts_cn 对齐；LLM 输出须从其中选取）
CATALYST_TYPES = frozenset({
    "业绩超预期", "政策行业利好", "重大合同订单", "股东增持回购",
    "题材板块启动", "技术突破放量", "其他催化",
})

RISK_TAGS = frozenset({
    "高位放量滞涨", "题材退潮风险", "业绩不及预期风险", "解禁减持临近",
    "立案处罚风险", "停牌风险", "流动性风险", "大盘系统性风险",
})


@dataclass
class Prediction:
    instrument: str
    name: str = ""
    action: str = "watch"           # predict | watch | reject
    confidence: float = 0.0
    swing_score: float = 0.0        # 融合分 0~1（规则分 + LLM 修正）
    entry_ref: str = "T+1开盘价"
    target_tiers: list[dict[str, Any]] = field(default_factory=list)
    stop_loss: float = -STOP_PCT
    horizon_days: int = HORIZON_DAYS
    catalysts: list[str] = field(default_factory=list)
    risk_tags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    factor_brief: dict[str, Any] = field(default_factory=dict)
    news_brief: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Prediction":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class PredictionFile:
    date: str
    status: str                     # ok | fail_open | dry_run
    universe_size: int
    candidates: list[str]
    predictions: list[Prediction]
    fail_reason: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "status": self.status,
            "fail_reason": self.fail_reason,
            "universe_size": self.universe_size,
            "candidates": list(self.candidates),
            "predictions": [p.to_dict() for p in self.predictions],
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PredictionFile":
        return cls(
            date=str(d["date"]),
            status=str(d.get("status", "fail_open")),
            universe_size=int(d.get("universe_size", 0)),
            candidates=[str(x) for x in (d.get("candidates") or [])],
            predictions=[Prediction.from_dict(x) for x in (d.get("predictions") or [])],
            fail_reason=d.get("fail_reason"),
            meta=dict(d.get("meta") or {}),
        )


# ---------------- 跟踪记录 ----------------


@dataclass
class TrackRecord:
    """一条预测的全生命周期（存于 tracker/{instrument}.json 的 records 列表）。"""
    pred_date: str
    instrument: str
    name: str = ""
    state: str = "triggered"
    confidence: float = 0.0
    swing_score: float = 0.0
    catalysts: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    entry_date: str | None = None
    entry_price: float | None = None
    days_held: int = 0
    mfe: float | None = None          # 期内最高收盘相对 entry
    mae: float | None = None          # 期内最低收盘相对 entry
    hit_tier: int = 0                 # 0 未达标 / 1:+10% / 2:+15% / 3:+20%
    result: str | None = None         # hit | stopped | expired | invalid
    result_date: str | None = None
    result_return: float | None = None  # 终态收益（收盘口径；hit 记达标日收盘）
    daily: list[dict[str, Any]] = field(default_factory=list)  # 逐日 {date,close,ret}
    notes: list[str] = field(default_factory=list)
    seen_news_ids: list[str] = field(default_factory=list)
    deltas: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TrackRecord":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------- 文件读写 ----------------


def pred_path(day: str) -> Path:
    return ROOT / "predictions" / f"{day}.json"


def write_predictions(payload: PredictionFile) -> Path:
    path = pred_path(payload.date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload.to_dict(), ensure_ascii=False, indent=2) + "\n")
    path.with_suffix(".done").touch()
    return path


def read_predictions(day: str) -> PredictionFile | None:
    path = pred_path(day)
    if not path.exists():
        return None
    try:
        return PredictionFile.from_dict(json.loads(path.read_text()))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def already_predicted_today(day: str) -> PredictionFile | None:
    """同日已有成功态预测（status=ok + .done）则返回文件，供跨账户幂等跳过。"""
    done = pred_path(day).with_suffix(".done")
    if not done.exists():
        return None
    pf = read_predictions(day)
    if pf and pf.status == "ok":
        return pf
    return None


def mark_skip_meta(pf: PredictionFile, account: str | None) -> PredictionFile:
    """记录后续账户因同日幂等而跳过 LLM。"""
    meta = dict(pf.meta or {})
    skipped = list(meta.get("skipped_by_accounts") or [])
    if account and account not in skipped:
        skipped.append(account)
    meta["skipped_by_accounts"] = skipped
    if "first_runner" not in meta:
        meta["first_runner"] = meta.get("account")
    pf.meta = meta
    return pf


def latest_prediction_day() -> str | None:
    """取最适合展示的预测日。

    优先「日历日最新」的有效结果，而不是「watch 数量最多」的旧日——
    否则新跑完的 08-07（137 watch + 8 predict）会被更旧的 08-06（145 watch）压住，
    看板上每日报告看起来像没更新。
    """
    d = ROOT / "predictions"
    if not d.exists():
        return None
    days = sorted(p.stem for p in d.glob("????-??-??.json"))
    if not days:
        return None

    def _rank(day: str) -> tuple:
        pf = read_predictions(day)
        if not pf:
            return (0, 0, 0, 0, day)
        ok = 1 if pf.status not in {"dry_run", "fail_open"} else 0
        n_pred = sum(1 for p in pf.predictions if p.action == "predict")
        n_watch = sum(1 for p in pf.predictions if p.action == "watch")
        useful = 1 if (n_pred + n_watch) > 0 else 0
        # 有效状态 > 有观察/预测内容 > 有 predict > 日期新
        return (ok, useful, 1 if n_pred > 0 else 0, day)

    return max(days, key=_rank)


def normalize_prediction(p: Prediction) -> Prediction:
    """入库前清洗：裁剪字段、压置信度区间、过滤非法催化/风险标签。"""
    p.action = p.action if p.action in {"predict", "watch", "reject"} else "watch"
    p.confidence = max(0.0, min(1.0, float(p.confidence or 0.0)))
    p.swing_score = max(0.0, min(1.0, float(p.swing_score or 0.0)))
    p.catalysts = [c for c in p.catalysts if c in CATALYST_TYPES][:4]
    p.risk_tags = [t for t in p.risk_tags if t in RISK_TAGS][:4]
    p.reasons = [str(r)[:120] for r in (p.reasons or [])][:3]
    raw_tiers = p.target_tiers
    if isinstance(raw_tiers, dict):
        raw_tiers = [raw_tiers]
    elif not isinstance(raw_tiers, list):
        raw_tiers = []
    tiers = []
    for t in raw_tiers[:3]:
        if not isinstance(t, dict):
            continue
        try:
            pct = float(t.get("pct", 0.0))
            prob = max(0.0, min(1.0, float(t.get("prob", 0.0))))
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 < pct <= 0.5:
            tiers.append({"pct": round(pct, 3), "prob": round(prob, 3)})
    p.target_tiers = tiers or [{"pct": HIT_PCT, "prob": round(p.confidence * 0.7, 3)}]
    return p
