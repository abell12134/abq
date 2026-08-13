"""Pydantic contracts for the prediction ledger and Supervisor.

Numbers on the wire are produced by deterministic settlement/calibration code.
LLM channels may only attach narrative fields (explanation, critic_notes).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ClaimType(str, Enum):
    direction = "direction"
    interval = "interval"
    target = "target"


class PredStatus(str, Enum):
    pending = "pending"
    resolved = "resolved"
    expired = "expired"
    shadow = "shadow"


class StrategyState(str, Enum):
    champion = "champion"
    challenger = "challenger"
    paused = "paused"
    shadow = "shadow"


class ReleaseGate(str, Enum):
    """Whether a prediction may enter the user-facing release board."""

    released = "released"  # graduated, n>=30, not paused
    hold = "hold"  # shadow / cold-start
    quarantine = "quarantine"  # sample insufficient or strategy paused
    observe = "observe"  # visible but not main recommend


class Scorecard(BaseModel):
    claim_type: ClaimType
    n: int
    hit_rate: float | None = None
    pic: float | None = None
    wilson_low: float | None = None
    wilson_high: float | None = None
    sample_ok: bool
    label: str  # e.g. "样本不足，仅供参考"


class FeatureSnapshotRef(BaseModel):
    feature_version: str
    pit_timestamp: str
    content_hash: str
    snapshot_ref: str


class Prediction(BaseModel):
    pred_id: str
    level: Literal["L1", "L2"]
    object: str
    object_name: str = ""
    claim_type: ClaimType
    claim: dict[str, Any]
    horizon: int
    benchmark: str
    settlement_caliber: str
    confidence: float
    raw_confidence: float
    strategy_version: str
    feature_snapshot: FeatureSnapshotRef
    created_at: str
    resolve_at: str
    status: PredStatus
    outcome: dict[str, Any] | None = None
    error_metrics: dict[str, Any] | None = None
    scorecard: Scorecard
    release_gate: ReleaseGate
    failure_conditions: list[str] = Field(default_factory=list)
    critic_notes: list[str] = Field(default_factory=list)
    explanation: str | None = None  # LLM narrative only


class StrategyTrust(BaseModel):
    strategy_id: str
    name: str
    version: str
    state: StrategyState
    trust_weight: float
    rolling_n: int
    rolling_hit_rate: float | None = None
    wilson_low: float | None = None
    pause_reason: str | None = None
    claim_type: ClaimType = ClaimType.direction


class SystemStatus(BaseModel):
    data_day: str
    settlement_caliber: str
    mode: Literal["shadow", "graduated"]
    shadow_days_remaining: int | None = None
    released_count: int
    hold_count: int
    quarantine_count: int
    pending_settle_count: int
    synthetic_demo: bool = True
    disclaimer: str


class CalibrationBucket(BaseModel):
    claim_type: ClaimType
    bin_lo: float
    bin_hi: float
    mean_confidence: float
    empirical_rate: float
    n: int


class SupervisorMessage(BaseModel):
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    pred_id: str | None = None
    tool_name: str | None = None
    ts: str


class SupervisorSession(BaseModel):
    session_id: str
    intent: Literal["single", "portfolio", "strategy", "general"] | None = None
    messages: list[SupervisorMessage] = Field(default_factory=list)
    attached_pred_ids: list[str] = Field(default_factory=list)


class SupervisorAskRequest(BaseModel):
    session_id: str | None = None
    message: str
    pred_id: str | None = None
    intent: Literal["single", "portfolio", "strategy", "general"] | None = None
