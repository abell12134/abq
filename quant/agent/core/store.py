"""SQLite prediction ledger."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

QUANT = Path(__file__).resolve().parents[2]
DB_PATH = QUANT / "data" / "agent" / "ledger.sqlite3"


def _connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(path: Path | None = None) -> Path:
    p = path or DB_PATH
    with _connect(p) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS predictions (
              pred_id TEXT PRIMARY KEY,
              level TEXT NOT NULL,
              object TEXT NOT NULL,
              object_name TEXT,
              claim_type TEXT NOT NULL,
              claim_json TEXT NOT NULL,
              horizon INTEGER NOT NULL,
              benchmark TEXT NOT NULL,
              settlement_caliber TEXT NOT NULL,
              confidence REAL,
              raw_confidence REAL,
              strategy_version TEXT NOT NULL,
              feature_version TEXT,
              pit_timestamp TEXT,
              content_hash TEXT,
              snapshot_ref TEXT,
              created_at TEXT NOT NULL,
              pred_date TEXT NOT NULL,
              resolve_at TEXT,
              status TEXT NOT NULL,
              outcome_json TEXT,
              error_metrics_json TEXT,
              failure_conditions_json TEXT,
              critic_notes_json TEXT,
              explanation TEXT,
              entry_date TEXT,
              entry_price REAL,
              synthetic INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_pred_status ON predictions(status);
            CREATE INDEX IF NOT EXISTS idx_pred_date ON predictions(pred_date);
            CREATE INDEX IF NOT EXISTS idx_pred_object ON predictions(object);

            CREATE TABLE IF NOT EXISTS system_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS strategies (
              strategy_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              version TEXT NOT NULL,
              state TEXT NOT NULL,
              trust_weight REAL NOT NULL DEFAULT 0,
              claim_type TEXT NOT NULL DEFAULT 'direction',
              rolling_n INTEGER NOT NULL DEFAULT 0,
              rolling_hit_rate REAL,
              wilson_low REAL,
              wilson_high REAL,
              pause_reason TEXT,
              bad_windows INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
    return p


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    init_db(path)
    conn = _connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["claim"] = json.loads(d.pop("claim_json"))
    d["outcome"] = json.loads(d.pop("outcome_json")) if d.get("outcome_json") else None
    d["error_metrics"] = (
        json.loads(d.pop("error_metrics_json")) if d.get("error_metrics_json") else None
    )
    d["failure_conditions"] = json.loads(d.pop("failure_conditions_json") or "[]")
    d["critic_notes"] = json.loads(d.pop("critic_notes_json") or "[]")
    d["feature_snapshot"] = {
        "feature_version": d.pop("feature_version") or "",
        "pit_timestamp": d.pop("pit_timestamp") or "",
        "content_hash": d.pop("content_hash") or "",
        "snapshot_ref": d.pop("snapshot_ref") or "",
    }
    d["synthetic"] = bool(d.get("synthetic"))
    return d


def upsert_prediction(pred: dict[str, Any], path: Path | None = None) -> None:
    fs = pred.get("feature_snapshot") or {}
    with session(path) as conn:
        conn.execute(
            """
            INSERT INTO predictions (
              pred_id, level, object, object_name, claim_type, claim_json,
              horizon, benchmark, settlement_caliber, confidence, raw_confidence,
              strategy_version, feature_version, pit_timestamp, content_hash,
              snapshot_ref, created_at, pred_date, resolve_at, status,
              outcome_json, error_metrics_json, failure_conditions_json,
              critic_notes_json, explanation, entry_date, entry_price, synthetic
            ) VALUES (
              :pred_id, :level, :object, :object_name, :claim_type, :claim_json,
              :horizon, :benchmark, :settlement_caliber, :confidence, :raw_confidence,
              :strategy_version, :feature_version, :pit_timestamp, :content_hash,
              :snapshot_ref, :created_at, :pred_date, :resolve_at, :status,
              :outcome_json, :error_metrics_json, :failure_conditions_json,
              :critic_notes_json, :explanation, :entry_date, :entry_price, :synthetic
            )
            ON CONFLICT(pred_id) DO UPDATE SET
              status=excluded.status,
              resolve_at=excluded.resolve_at,
              outcome_json=excluded.outcome_json,
              error_metrics_json=excluded.error_metrics_json,
              confidence=excluded.confidence,
              critic_notes_json=excluded.critic_notes_json,
              explanation=excluded.explanation,
              entry_date=excluded.entry_date,
              entry_price=excluded.entry_price
            """,
            {
                "pred_id": pred["pred_id"],
                "level": pred["level"],
                "object": pred["object"],
                "object_name": pred.get("object_name") or "",
                "claim_type": pred["claim_type"],
                "claim_json": json.dumps(pred["claim"], ensure_ascii=False),
                "horizon": pred["horizon"],
                "benchmark": pred["benchmark"],
                "settlement_caliber": pred["settlement_caliber"],
                "confidence": pred.get("confidence"),
                "raw_confidence": pred.get("raw_confidence"),
                "strategy_version": pred["strategy_version"],
                "feature_version": fs.get("feature_version"),
                "pit_timestamp": fs.get("pit_timestamp"),
                "content_hash": fs.get("content_hash"),
                "snapshot_ref": fs.get("snapshot_ref"),
                "created_at": pred["created_at"],
                "pred_date": pred["pred_date"],
                "resolve_at": pred.get("resolve_at"),
                "status": pred["status"],
                "outcome_json": json.dumps(pred["outcome"], ensure_ascii=False)
                if pred.get("outcome")
                else None,
                "error_metrics_json": json.dumps(pred["error_metrics"], ensure_ascii=False)
                if pred.get("error_metrics")
                else None,
                "failure_conditions_json": json.dumps(
                    pred.get("failure_conditions") or [], ensure_ascii=False
                ),
                "critic_notes_json": json.dumps(
                    pred.get("critic_notes") or [], ensure_ascii=False
                ),
                "explanation": pred.get("explanation"),
                "entry_date": pred.get("entry_date"),
                "entry_price": pred.get("entry_price"),
                "synthetic": 1 if pred.get("synthetic") else 0,
            },
        )


def get_prediction(pred_id: str, path: Path | None = None) -> dict[str, Any] | None:
    with session(path) as conn:
        row = conn.execute(
            "SELECT * FROM predictions WHERE pred_id=?", (pred_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_predictions(
    *,
    status: str | None = None,
    path: Path | None = None,
    include_synthetic: bool = True,
    limit: int = 500,
) -> list[dict[str, Any]]:
    q = "SELECT * FROM predictions WHERE 1=1"
    args: list[Any] = []
    if status:
        q += " AND status=?"
        args.append(status)
    if not include_synthetic:
        q += " AND synthetic=0"
    q += " ORDER BY pred_date DESC, pred_id DESC LIMIT ?"
    args.append(limit)
    with session(path) as conn:
        rows = conn.execute(q, args).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_pending(path: Path | None = None) -> list[dict[str, Any]]:
    with session(path) as conn:
        rows = conn.execute(
            "SELECT * FROM predictions WHERE status IN ('pending','shadow') "
            "AND outcome_json IS NULL ORDER BY pred_date"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_resolved(
    strategy_version: str | None = None,
    claim_type: str = "direction",
    path: Path | None = None,
) -> int:
    q = (
        "SELECT COUNT(*) FROM predictions WHERE status='resolved' "
        "AND claim_type=? AND synthetic=0"
    )
    args: list[Any] = [claim_type]
    if strategy_version:
        q += " AND strategy_version LIKE ?"
        args.append(f"{strategy_version}%")
    with session(path) as conn:
        return int(conn.execute(q, args).fetchone()[0])


def set_meta(key: str, value: str, path: Path | None = None) -> None:
    with session(path) as conn:
        conn.execute(
            "INSERT INTO system_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_meta(key: str, default: str | None = None, path: Path | None = None) -> str | None:
    with session(path) as conn:
        row = conn.execute(
            "SELECT value FROM system_meta WHERE key=?", (key,)
        ).fetchone()
    return row["value"] if row else default


def has_real_rows(path: Path | None = None) -> bool:
    with session(path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE synthetic=0"
        ).fetchone()[0]
    return int(n) > 0


def upsert_strategy(row: dict[str, Any], path: Path | None = None) -> None:
    with session(path) as conn:
        conn.execute(
            """
            INSERT INTO strategies (
              strategy_id, name, version, state, trust_weight, claim_type,
              rolling_n, rolling_hit_rate, wilson_low, wilson_high,
              pause_reason, bad_windows, updated_at
            ) VALUES (
              :strategy_id, :name, :version, :state, :trust_weight, :claim_type,
              :rolling_n, :rolling_hit_rate, :wilson_low, :wilson_high,
              :pause_reason, :bad_windows, :updated_at
            )
            ON CONFLICT(strategy_id) DO UPDATE SET
              name=excluded.name,
              version=excluded.version,
              state=excluded.state,
              trust_weight=excluded.trust_weight,
              claim_type=excluded.claim_type,
              rolling_n=excluded.rolling_n,
              rolling_hit_rate=excluded.rolling_hit_rate,
              wilson_low=excluded.wilson_low,
              wilson_high=excluded.wilson_high,
              pause_reason=excluded.pause_reason,
              bad_windows=excluded.bad_windows,
              updated_at=excluded.updated_at
            """,
            row,
        )


def list_strategies(path: Path | None = None) -> list[dict[str, Any]]:
    with session(path) as conn:
        rows = conn.execute(
            "SELECT * FROM strategies ORDER BY trust_weight DESC, strategy_id"
        ).fetchall()
    return [dict(r) for r in rows]


def get_strategy(strategy_id: str, path: Path | None = None) -> dict[str, Any] | None:
    with session(path) as conn:
        row = conn.execute(
            "SELECT * FROM strategies WHERE strategy_id=?", (strategy_id,)
        ).fetchone()
    return dict(row) if row else None
