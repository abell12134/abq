"""批量信号生成：一次特征计算覆盖整个日期区间，按日拆分写出信号。

predict_daily.py 每调用一次都要重算 365 天 Alpha158 特征（单日约 4 分钟），
回填一周需重复多次。本脚本把 [start, end] 作为一个 test 段一次性推理，
再按交易日拆分写出 data/signals/YYYY-MM-DD.csv(+.done)，已存在的日期跳过。

用法：
    python research/predict_range.py --start 2026-06-03 --end 2026-06-11
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")

import pandas as pd
import yaml

import qlib
from qlib.data import D
from qlib.utils import init_instance_by_config
from qlib.workflow import R

HERE = Path(__file__).resolve().parent
QUANT = HERE.parent
EXPERIMENT = "baseline_alpha158_lgbm"
LOOKBACK_DAYS = 365


def load_latest_model():
    exp = R.get_exp(experiment_name=EXPERIMENT, create=False)
    recorders = exp.list_recorders(rtype=exp.RT_L, status="FINISHED")
    if not recorders:
        raise RuntimeError("没有已完成的训练记录，请先运行 run_baseline.py")
    rec = max(recorders, key=lambda r: r.info["end_time"])
    return rec.load_object("params.pkl"), rec.id


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    args = p.parse_args()

    cfg = yaml.safe_load((HERE / "workflow_baseline.yaml").read_text())
    exp_uri = "file:" + str(HERE / "mlruns")
    qlib.init(provider_uri=cfg["qlib_init"]["provider_uri"], region="cn",
              exp_manager={"class": "MLflowExpManager", "module_path": "qlib.workflow.expm",
                           "kwargs": {"uri": exp_uri, "default_exp_name": "Experiment"}})

    cal = [pd.Timestamp(d) for d in D.calendar(freq="day")]
    last = cal[-1]
    end = pd.Timestamp(args.end)
    if end > last:
        print(f"[FATAL] 目标 {end:%F} 晚于数据最新 {last:%F}")
        return 1

    model, rec_id = load_latest_model()
    print(f"[1/2] 模型 recorder={rec_id}，区间 {args.start}~{args.end}")

    ds_cfg = cfg["task"]["dataset"]
    start = (pd.Timestamp(args.start) - pd.Timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    h = ds_cfg["kwargs"]["handler"]
    h["kwargs"].update(start_time=start, end_time=args.end,
                       fit_start_time=start, fit_end_time=args.end)
    ds_cfg["kwargs"]["segments"] = {"test": [args.start, args.end]}
    dataset = init_instance_by_config(ds_cfg)

    print("[2/2] 推理并按日拆分写出")
    pred = model.predict(dataset, segment="test")
    if isinstance(pred, pd.DataFrame):
        pred = pred.iloc[:, 0]
    df = pred.reset_index()
    df.columns = ["datetime", "instrument", "score"]

    out_dir = QUANT / "data" / "signals"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for day, g in df.groupby("datetime"):
        ds = pd.Timestamp(day).strftime("%Y-%m-%d")
        out = out_dir / f"{ds}.csv"
        if out.with_suffix(".done").exists():
            continue
        g = g[["instrument", "score"]].sort_values("score", ascending=False).reset_index(drop=True)
        g["rank"] = g.index + 1
        g.to_csv(out, index=False)
        out.with_suffix(".done").touch()
        written += 1
        print(f"  [OK] {ds}: {len(g)} 条")
    print(f"[完成] 新写 {written} 个交易日信号")
    return 0


if __name__ == "__main__":
    sys.exit(main())
