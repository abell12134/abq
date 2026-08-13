"""Synthetic demo ledger — labeled synthetic; replace with real Track outputs."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent_api.models import (
    CalibrationBucket,
    ClaimType,
    FeatureSnapshotRef,
    Prediction,
    PredStatus,
    ReleaseGate,
    Scorecard,
    StrategyState,
    StrategyTrust,
    SystemStatus,
)

TZ = ZoneInfo("Asia/Shanghai")
CALIBER = "caliber.v1.direction_excess"


def _day() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _scorecard(n: int, hit: float, claim_type: ClaimType = ClaimType.direction) -> Scorecard:
    # Approximate Wilson for demo display only
    import math

    z = 1.96
    if n <= 0:
        return Scorecard(
            claim_type=claim_type,
            n=0,
            hit_rate=None,
            wilson_low=None,
            wilson_high=None,
            sample_ok=False,
            label="样本不足，仅供参考",
        )
    p = hit
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    lo = (centre - margin) / denom
    hi = (centre + margin) / denom
    ok = n >= 30
    return Scorecard(
        claim_type=claim_type,
        n=n,
        hit_rate=round(hit, 4) if claim_type == ClaimType.direction else None,
        pic=round(hit, 4) if claim_type == ClaimType.interval else None,
        wilson_low=round(lo, 4),
        wilson_high=round(hi, 4),
        sample_ok=ok,
        label="追踪成绩" if ok else "样本不足，仅供参考",
    )


def system_status() -> SystemStatus:
    preds = list_predictions()
    return SystemStatus(
        data_day=_day(),
        settlement_caliber=CALIBER,
        mode="shadow",
        shadow_days_remaining=28,
        released_count=sum(1 for p in preds if p.release_gate == ReleaseGate.released),
        hold_count=sum(1 for p in preds if p.release_gate == ReleaseGate.hold),
        quarantine_count=sum(1 for p in preds if p.release_gate == ReleaseGate.quarantine),
        pending_settle_count=sum(1 for p in preds if p.status == PredStatus.pending),
        synthetic_demo=True,
        disclaimer=(
            "演示数据为合成样例，非实盘成绩。"
            "系统只分析不交易；纸面指标≠可实现收益；无成绩单不荐股。"
        ),
    )


def list_predictions() -> list[Prediction]:
    day = _day()
    base = datetime.now(TZ).replace(hour=9, minute=0, second=0, microsecond=0)
    snap = FeatureSnapshotRef(
        feature_version="alpha158_plus_lab.v3",
        pit_timestamp=f"{day}T09:00:00+08:00",
        content_hash="a1b2c3d4e5f67890",
        snapshot_ref=f"parquet://features/{day}/batch.parquet",
    )

    items: list[Prediction] = [
        Prediction(
            pred_id="pred_20260812_L1_600519_001",
            level="L1",
            object="SH600519",
            object_name="贵州茅台",
            claim_type=ClaimType.direction,
            claim={"direction": "up", "vs": "CSI500"},
            horizon=10,
            benchmark="CSI500",
            settlement_caliber=CALIBER,
            confidence=0.62,
            raw_confidence=0.71,
            strategy_version="lgbm_planC.champion.v12",
            feature_snapshot=snap,
            created_at=base.isoformat(),
            resolve_at=(base + timedelta(days=14)).strftime("%Y-%m-%d"),
            status=PredStatus.shadow,
            scorecard=_scorecard(42, 0.571),
            release_gate=ReleaseGate.hold,
            failure_conditions=[
                "滚动 Wilson 下界跌破 0.50 且持续 2 窗 → 降权",
                "预测期内 ST/退市 → 事件日提前结算",
            ],
            critic_notes=["代码断言：PIT 特征通过", "冷启动：shadow，不进主推荐"],
            explanation="模型相对中证500偏多；校准后置信度下调。合成演示叙述。",
        ),
        Prediction(
            pred_id="pred_20260812_L1_000001_002",
            level="L1",
            object="SZ000001",
            object_name="平安银行",
            claim_type=ClaimType.direction,
            claim={"direction": "up", "vs": "CSI300"},
            horizon=10,
            benchmark="CSI300",
            settlement_caliber=CALIBER,
            confidence=0.55,
            raw_confidence=0.58,
            strategy_version="lgbm_planC.champion.v12",
            feature_snapshot=snap,
            created_at=base.isoformat(),
            resolve_at=(base + timedelta(days=14)).strftime("%Y-%m-%d"),
            status=PredStatus.shadow,
            scorecard=_scorecard(42, 0.571),
            release_gate=ReleaseGate.hold,
            failure_conditions=["行业集中度超限时 L2 约束可能否决组合纳入"],
            critic_notes=["样本毕业策略级，系统仍处 shadow 日"],
        ),
        Prediction(
            pred_id="pred_20260812_L1_300750_003",
            level="L1",
            object="SZ300750",
            object_name="宁德时代",
            claim_type=ClaimType.interval,
            claim={"low": -0.02, "high": 0.05, "vs": "CSI500"},
            horizon=10,
            benchmark="CSI500",
            settlement_caliber=CALIBER,
            confidence=0.48,
            raw_confidence=0.52,
            strategy_version="interval_band.challenger.v3",
            feature_snapshot=snap,
            created_at=base.isoformat(),
            resolve_at=(base + timedelta(days=14)).strftime("%Y-%m-%d"),
            status=PredStatus.shadow,
            scorecard=_scorecard(18, 0.72, ClaimType.interval),
            release_gate=ReleaseGate.quarantine,
            failure_conditions=["区间 PIC 偏离名义水平 → 带宽重估，不做命中率校准"],
            critic_notes=["challenger · n=18 样本不足，隔离观察"],
        ),
        Prediction(
            pred_id="pred_20260720_L1_601318_004",
            level="L1",
            object="SH601318",
            object_name="中国平安",
            claim_type=ClaimType.direction,
            claim={"direction": "down", "vs": "CSI300"},
            horizon=10,
            benchmark="CSI300",
            settlement_caliber=CALIBER,
            confidence=0.57,
            raw_confidence=0.60,
            strategy_version="lgbm_planC.champion.v12",
            feature_snapshot=snap.model_copy(
                update={"pit_timestamp": "2026-07-20T09:00:00+08:00"}
            ),
            created_at="2026-07-20T09:00:00+08:00",
            resolve_at="2026-08-03",
            status=PredStatus.resolved,
            outcome={"hit": True, "excess_return": -0.031},
            scorecard=_scorecard(42, 0.571),
            release_gate=ReleaseGate.observe,
            failure_conditions=[],
            critic_notes=["已结算 · hit"],
        ),
        Prediction(
            pred_id="pred_20260812_L2_PORT_A_005",
            level="L2",
            object="PORT_STEADY_A",
            object_name="稳健组合 A",
            claim_type=ClaimType.target,
            claim={
                "target_ann_return": 0.08,
                "max_drawdown": 0.12,
                "max_vol": 0.18,
                "benchmark": "CSI500",
            },
            horizon=60,
            benchmark="CSI500",
            settlement_caliber=CALIBER,
            confidence=0.44,
            raw_confidence=0.50,
            strategy_version="portfolio_opt.v1",
            feature_snapshot=snap,
            created_at=base.isoformat(),
            resolve_at=(base + timedelta(days=90)).strftime("%Y-%m-%d"),
            status=PredStatus.shadow,
            scorecard=_scorecard(12, 0.42, ClaimType.target),
            release_gate=ReleaseGate.quarantine,
            failure_conditions=[
                "三项独立裁决：超额 / 目标 / 约束，不可由 L1 命中率推断",
                "纸面回撤与换手，不含冲击成本",
            ],
            critic_notes=["L2 样本不足 · 纸面指标"],
            explanation="目标年化 8% 相对历史可达性偏低；仅观察。",
        ),
    ]
    return items


def list_strategies() -> list[StrategyTrust]:
    return [
        StrategyTrust(
            strategy_id="lgbm_planC",
            name="LGBM Plan C 方向",
            version="champion.v12",
            state=StrategyState.shadow,
            trust_weight=1.0,
            rolling_n=42,
            rolling_hit_rate=0.571,
            wilson_low=0.42,
            pause_reason=None,
        ),
        StrategyTrust(
            strategy_id="interval_band",
            name="区间带宽 Challenger",
            version="challenger.v3",
            state=StrategyState.challenger,
            trust_weight=0.0,
            rolling_n=18,
            rolling_hit_rate=None,
            wilson_low=None,
            pause_reason="样本不足，不评估不降权",
            claim_type=ClaimType.interval,
        ),
        StrategyTrust(
            strategy_id="momentum_overflow",
            name="动量溢出（已暂停）",
            version="paused.v8",
            state=StrategyState.paused,
            trust_weight=0.0,
            rolling_n=60,
            rolling_hit_rate=0.41,
            wilson_low=0.29,
            pause_reason="滚动 Wilson 下界持续低于随机基准",
        ),
    ]


def calibration_buckets() -> list[CalibrationBucket]:
    return [
        CalibrationBucket(
            claim_type=ClaimType.direction,
            bin_lo=0.5,
            bin_hi=0.55,
            mean_confidence=0.53,
            empirical_rate=0.51,
            n=14,
        ),
        CalibrationBucket(
            claim_type=ClaimType.direction,
            bin_lo=0.55,
            bin_hi=0.60,
            mean_confidence=0.57,
            empirical_rate=0.54,
            n=12,
        ),
        CalibrationBucket(
            claim_type=ClaimType.direction,
            bin_lo=0.60,
            bin_hi=0.70,
            mean_confidence=0.64,
            empirical_rate=0.58,
            n=16,
        ),
        CalibrationBucket(
            claim_type=ClaimType.interval,
            bin_lo=0.70,
            bin_hi=0.85,
            mean_confidence=0.78,
            empirical_rate=0.74,
            n=11,
        ),
        CalibrationBucket(
            claim_type=ClaimType.interval,
            bin_lo=0.85,
            bin_hi=1.01,
            mean_confidence=0.91,
            empirical_rate=0.88,
            n=7,
        ),
    ]
