"""用已有训练记录（pred.pkl）重跑组合回测，不重新训练。

用途：调组合参数（topk/n_drop/费率）时快速迭代。
    python rerun_backtest.py --recorder <id> [--topk 50] [--n_drop 1]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")

import yaml

import qlib
from qlib.workflow import R
from qlib.workflow.record_temp import PortAnaRecord

HERE = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recorder", required=True)
    parser.add_argument("--topk", type=int, default=None)
    parser.add_argument("--n_drop", type=int, default=None)
    parser.add_argument("--hold_thresh", type=int, default=None,
                        help="最小持有天数，提高可降换手（默认1=可次日卖出）")
    args = parser.parse_args()

    cfg = yaml.safe_load((HERE / "workflow_baseline.yaml").read_text())
    qlib.init(provider_uri=cfg["qlib_init"]["provider_uri"], region="cn")

    pa_cfg = cfg["port_analysis_config"]
    if args.topk:
        pa_cfg["strategy"]["kwargs"]["topk"] = args.topk
    if args.n_drop is not None:
        pa_cfg["strategy"]["kwargs"]["n_drop"] = args.n_drop
    if args.hold_thresh is not None:
        pa_cfg["strategy"]["kwargs"]["hold_thresh"] = args.hold_thresh
    sk = pa_cfg["strategy"]["kwargs"]
    print(f"组合参数: topk={sk['topk']} n_drop={sk['n_drop']} "
          f"hold_thresh={sk.get('hold_thresh', 1)}")

    rec = R.get_exp(experiment_name="baseline_alpha158_lgbm").get_recorder(
        recorder_id=args.recorder)
    PortAnaRecord(rec, pa_cfg, risk_analysis_freq="day").generate()

    m = rec.list_metrics()
    for k in sorted(m):
        if "excess_return" in k and ("annualized" in k or "information" in k
                                     or "max_drawdown" in k):
            print(f"{k:65s} {m[k]: .4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
