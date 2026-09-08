"""持仓追踪 overlay：聚合各账户 fills，自首次买入日起追踪价格走势。

输出：data/overlays/tracking/snapshot.json
任务状态：data/overlays/tracking/job.json
"""

from __future__ import annotations

from pathlib import Path

QUANT = Path(__file__).resolve().parents[2]
ROOT = QUANT / "data" / "overlays" / "tracking"
SNAPSHOT_FILE = ROOT / "snapshot.json"
JOB_FILE = ROOT / "job.json"
ANALYZE_JOB_FILE = ROOT / "analyze_job.json"
