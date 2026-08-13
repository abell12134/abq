"""Hard Critic assertions (code, not LLM) — block tainted emits.

Plan §9 minimum:
- feature snapshot must be point-in-time for pred_date
- no future calendar day beyond Qlib latest
- signal artifact must match pred_date (no silent latest_pred substitution without flag)
- train/track non-overlap when train_end meta is set
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AssertResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def raise_if_failed(self) -> None:
        if not self.ok:
            raise AssertionError("; ".join(self.errors))


def _latest_trading_day() -> str | None:
    try:
        import sys

        quant = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(quant / "ops"))
        import common as C  # noqa: WPS433

        return str(C.latest_trading_day())[:10]
    except Exception:
        return None


def assert_emit_day(
    day: str,
    *,
    signal_path: Path | None,
    allow_latest_fallback: bool = False,
    train_end: str | None = None,
) -> AssertResult:
    errors: list[str] = []
    warnings: list[str] = []

    if not day or len(day) < 10:
        errors.append(f"非法 pred_date={day}")

    latest = _latest_trading_day()
    if latest and day > latest:
        errors.append(f"lookahead: pred_date {day} 晚于 Qlib 最新交易日 {latest}")

    if signal_path is None or not signal_path.exists():
        errors.append(f"缺少信号文件: {signal_path}")
    else:
        name = signal_path.name
        if name == "latest_pred.csv":
            if allow_latest_fallback:
                warnings.append("使用 latest_pred.csv 回退；请确认无未来信息泄漏")
            else:
                errors.append(
                    "禁止静默使用 latest_pred.csv；须提供当日 signals/YYYY-MM-DD.csv"
                )
        elif name != f"{day}.csv":
            errors.append(f"信号文件名 {name} 与 pred_date {day} 不一致")

        done = signal_path.with_suffix(".done")
        if signal_path.suffix == ".csv" and name != "latest_pred.csv" and not done.exists():
            warnings.append(f"缺少 {done.name} 完成标记")

    if train_end and day <= train_end:
        errors.append(
            f"训练/追踪重叠: pred_date {day} <= train_end {train_end}"
        )

    return AssertResult(ok=not errors, errors=errors, warnings=warnings)


def assert_prediction_record(pred: dict[str, Any]) -> AssertResult:
    errors: list[str] = []
    warnings: list[str] = []
    day = pred.get("pred_date") or ""
    fs = pred.get("feature_snapshot") or {}
    pit = str(fs.get("pit_timestamp") or "")
    if not fs.get("feature_version"):
        errors.append("feature_snapshot.feature_version 缺失")
    if not fs.get("content_hash"):
        errors.append("feature_snapshot.content_hash 缺失")
    if not fs.get("snapshot_ref"):
        errors.append("feature_snapshot.snapshot_ref 缺失")
    if pit and day and not pit.startswith(day):
        errors.append(f"PIT 违规: pit_timestamp={pit} 不属于 pred_date={day}")
    if pred.get("resolve_at") and pred.get("status") in ("pending", "shadow"):
        # resolve_at may be estimated; if set before settlement must not be used as price input
        warnings.append("pending 预测带 resolve_at 仅为预期到期，不得作特征")
    claim = pred.get("claim") or {}
    for bad in ("future_return", "label", "y_true"):
        if bad in claim:
            errors.append(f"claim 含禁止字段 {bad}")
    return AssertResult(ok=not errors, errors=errors, warnings=warnings)


def gate_emit_or_raise(
    day: str,
    signal_path: Path | None,
    *,
    allow_latest_fallback: bool = False,
    train_end: str | None = None,
) -> AssertResult:
    from agent.core import store

    te = train_end or store.get_meta("train_end")
    res = assert_emit_day(
        day,
        signal_path=signal_path,
        allow_latest_fallback=allow_latest_fallback,
        train_end=te,
    )
    res.raise_if_failed()
    return res
