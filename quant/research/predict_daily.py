"""每日信号生成：加载已训练的基线模型，对最新交易日打分。

产出（数据契约 L2 → L3）：
    data/signals/YYYY-MM-DD.csv   列：instrument, score, rank
    data/signals/YYYY-MM-DD.done  完成标记，下游以此为触发条件

前置条件：当日 update_daily.py 已成功；run_baseline.py 至少跑过一次。
若 Qlib 数据最新日期不等于目标日期则拒绝出信号（防止用陈旧数据交易）。
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
# Alpha158 滚动窗口最长 60 日，回看一年足够
LOOKBACK_DAYS = 365


def latest_trading_day() -> pd.Timestamp:
    cal = D.calendar(freq="day")
    return pd.Timestamp(cal[-1])


def load_latest_model():
    exp = R.get_exp(experiment_name=EXPERIMENT, create=False)
    recorders = exp.list_recorders(rtype=exp.RT_L, status="FINISHED")
    if not recorders:
        raise RuntimeError("没有已完成的训练记录，请先运行 run_baseline.py")
    rec = max(recorders, key=lambda r: r.info["end_time"])
    return rec.load_object("params.pkl"), rec.id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="目标日期，默认取数据最新交易日")
    args = parser.parse_args()

    cfg = yaml.safe_load((HERE / "workflow_baseline.yaml").read_text())
    # 本地 handler（alpha158_plus_lab）需在 research/ 下可 import
    sys.path.insert(0, str(HERE))
    # 训练实验记录固定存放在 research/mlruns；显式指定 exp_manager uri，
    # 使本脚本无论从哪个工作目录启动（如 ops/run_daily 子进程）都能找到模型。
    exp_uri = "file:" + str(HERE / "mlruns")
    qlib.init(provider_uri=cfg["qlib_init"]["provider_uri"], region="cn",
              exp_manager={
                  "class": "MLflowExpManager",
                  "module_path": "qlib.workflow.expm",
                  "kwargs": {"uri": exp_uri, "default_exp_name": "Experiment"},
              })

    day = pd.Timestamp(args.date) if args.date else latest_trading_day()
    if args.date and latest_trading_day() < day:
        print(f"[FATAL] Qlib 数据最新日期 {latest_trading_day():%F} 早于目标 {day:%F}，"
              "请先运行 update_daily.py")
        return 1

    out = Path(QUANT / "data" / "signals") / f"{day:%Y-%m-%d}.csv"
    if (out.with_suffix(".done")).exists():
        print(f"[OK] {out.name} 已存在，跳过")
        return 0

    model, rec_id = load_latest_model()
    print(f"[1/2] 使用模型 recorder={rec_id}，目标日 {day:%F}")

    ds_cfg = cfg["task"]["dataset"]
    start = (day - pd.Timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    end = day.strftime("%Y-%m-%d")
    h = ds_cfg["kwargs"]["handler"]
    h["kwargs"].update(start_time=start, end_time=end,
                       fit_start_time=start, fit_end_time=end)
    ds_cfg["kwargs"]["segments"] = {"test": [end, end]}
    dataset = init_instance_by_config(ds_cfg)

    print("[2/2] 推理并写出信号")
    pred: pd.Series = model.predict(dataset, segment="test")
    if isinstance(pred, pd.DataFrame):
        pred = pred.iloc[:, 0]
    df = pred.reset_index()
    df.columns = ["datetime", "instrument", "score"]
    df = df[df["datetime"] == day][["instrument", "score"]]
    if df.empty:
        print(f"[FATAL] {day:%F} 无信号产出（数据缺失？）")
        return 1
    df = df.sort_values("score", ascending=False).reset_index(drop=True)
    df["rank"] = df.index + 1

    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    out.with_suffix(".done").touch()
    print(f"[OK] {len(df)} 条信号已写入 {out}")
    print(df.head(10).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
