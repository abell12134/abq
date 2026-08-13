"""Feature vector archive — ledger keeps snapshot_ref only.

Writes JSON always; parquet best-effort (pyarrow can abort on some hosts).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

QUANT = Path(__file__).resolve().parents[2]
ARCHIVE_ROOT = QUANT / "data" / "agent" / "features"


def content_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def write_feature_snapshot(
    *,
    pred_id: str,
    day: str,
    instrument: str,
    features: dict[str, Any],
    root: Path | None = None,
) -> dict[str, str]:
    """Persist feature row; returns feature_snapshot fields."""
    base = root or ARCHIVE_ROOT
    day_dir = base / day
    day_dir.mkdir(parents=True, exist_ok=True)
    row = {"pred_id": pred_id, "pred_date": day, "instrument": instrument, **features}
    json_path = day_dir / f"{pred_id}.json"
    json_path.write_text(json.dumps(row, ensure_ascii=False, default=str), encoding="utf-8")
    rel = f"archive://features/{day}/{pred_id}.json"

    # optional parquet twin
    try:
        import pandas as pd

        pq = day_dir / f"{pred_id}.parquet"
        pd.DataFrame([row]).to_parquet(pq, index=False)
        rel = f"parquet://features/{day}/{pred_id}.parquet"
    except Exception:
        pass

    return {
        "snapshot_ref": rel,
        "content_hash": content_hash(row),
        "archive_path": str(json_path),
    }


def read_feature_snapshot(
    pred_id: str,
    day: str,
    *,
    root: Path | None = None,
) -> dict[str, Any] | None:
    base = root or ARCHIVE_ROOT
    json_path = base / day / f"{pred_id}.json"
    if json_path.exists():
        return json.loads(json_path.read_text(encoding="utf-8"))
    pq = base / day / f"{pred_id}.parquet"
    if pq.exists():
        try:
            import pandas as pd

            df = pd.read_parquet(pq)
            if df.empty:
                return None
            return df.iloc[0].to_dict()
        except Exception:
            return None
    return None
