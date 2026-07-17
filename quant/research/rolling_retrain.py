"""月度滚动重训：walk-forward 切窗 → 训练 LGBM → IR 门禁 → 达标才写入实验记录。

借鉴 qlib RollingGen 思想，但不依赖 Mongo TrainerRM：用现有 MLflow/recorder
与 predict_daily.load_latest_model 兼容（同一 experiment_name）。

门禁：
  1) 样本外 information_ratio ≥ --min-ir（默认 0.8）
  2) 若不加 --force：新 IR 须 ≥ 当前线上模型 IR - --tol（默认 0.05）
     避免「能训但更差」的模型顶掉生产模型

用法：
    python rolling_retrain.py
    python rolling_retrain.py --as-of 2026-07-10 --force
    python rolling_retrain.py --train-years 8 --valid-years 1 --test-years 1
"""

from __future__ import annotations

import argparse
import datetime as dt
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
from qlib.workflow.record_temp import PortAnaRecord, SigAnaRecord, SignalRecord

HERE = Path(__file__).resolve().parent
QUANT = HERE.parent
EXPERIMENT = "baseline_alpha158_lgbm"
IR_KEY = "1day.excess_return_with_cost.information_ratio"


def _shift_years(day: pd.Timestamp, years: float) -> pd.Timestamp:
    # 用 365.25 近似，再对齐到交易日
    return day - pd.Timedelta(days=int(years * 365.25))


def build_segments(as_of: pd.Timestamp, train_years: float, valid_years: float,
                   test_years: float) -> dict:
    test_end = as_of
    test_start = _shift_years(test_end, test_years)
    valid_end = test_start - pd.Timedelta(days=1)
    valid_start = _shift_years(valid_end, valid_years)
    train_end = valid_start - pd.Timedelta(days=1)
    train_start = _shift_years(train_end, train_years)

    def fmt(t: pd.Timestamp) -> str:
        return t.strftime("%Y-%m-%d")

    return {
        "train": [fmt(train_start), fmt(train_end)],
        "valid": [fmt(valid_start), fmt(valid_end)],
        "test": [fmt(test_start), fmt(test_end)],
    }


def current_prod_ir(exp_uri: str) -> float | None:
    """读取线上最新 FINISHED recorder 的 IR；没有则 None。"""
    try:
        exp = R.get_exp(experiment_name=EXPERIMENT, create=False)
        recorders = exp.list_recorders(rtype=exp.RT_L, status="FINISHED")
        if not recorders:
            return None
        rec = max(recorders, key=lambda r: r.info["end_time"])
        m = rec.list_metrics()
        ir = m.get(IR_KEY)
        return float(ir) if ir is not None else None
    except Exception:
        return None


def apply_segments(cfg: dict, segments: dict, as_of: str) -> dict:
    """深拷一份配置，改 segments / handler 时间窗 / backtest 区间。"""
    import copy
    c = copy.deepcopy(cfg)
    ds = c["task"]["dataset"]["kwargs"]
    ds["segments"] = {
        "train": segments["train"],
        "valid": segments["valid"],
        "test": segments["test"],
    }
    hkw = ds["handler"]["kwargs"]
    hkw["start_time"] = segments["train"][0]
    hkw["end_time"] = segments["test"][1]
    hkw["fit_start_time"] = segments["train"][0]
    hkw["fit_end_time"] = segments["train"][1]
    # 组合回测对齐 test 段
    bt = c["port_analysis_config"]["backtest"]
    bt["start_time"] = segments["test"][0]
    bt["end_time"] = segments["test"][1]
    # handler 可能是相对模块 alpha158_plus_lab，保证可 import
    sys.path.insert(0, str(HERE))
    return c


def render_report(segments: dict, metrics: dict, rec_id: str, promoted: bool,
                  prod_ir: float | None) -> str:
    ir = metrics.get(IR_KEY)
    ann = metrics.get("1day.excess_return_with_cost.annualized_return")
    lines = [
        f"# 滚动重训报告 {dt.date.today():%Y-%m-%d}",
        "",
        f"- Recorder: `{rec_id}`",
        f"- segments.train: {segments['train']}",
        f"- segments.valid: {segments['valid']}",
        f"- segments.test: {segments['test']}",
        f"- 新模型 IR: {ir}",
        f"- 新模型年化超额: {ann}",
        f"- 线上原 IR: {prod_ir}",
        f"- 是否晋升为最新 recorder: **{'是' if promoted else '否'}**",
        "",
        "门禁：IR≥min_ir，且（--force 或 新IR ≥ 线上IR - tol）。",
        "predict_daily 始终加载同 experiment 下 end_time 最新的 FINISHED recorder；",
        "未过门禁时本脚本不 `R.start` 落盘，故不会顶掉线上模型。",
    ]
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=str(HERE / "workflow_baseline.yaml"))
    p.add_argument("--as-of", default=None, help="滚动终点，默认 Qlib 最新交易日")
    p.add_argument("--train-years", type=float, default=8.0)
    p.add_argument("--valid-years", type=float, default=1.0)
    p.add_argument("--test-years", type=float, default=1.0)
    p.add_argument("--min-ir", type=float, default=0.8)
    p.add_argument("--tol", type=float, default=0.05,
                   help="允许新 IR 低于线上的最大幅度")
    p.add_argument("--force", action="store_true",
                   help="忽略与线上 IR 比较，仍须过 min-ir")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印切窗，不训练")
    args = p.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    exp_uri = "file:" + str(HERE / "mlruns")
    qlib.init(provider_uri=cfg["qlib_init"]["provider_uri"], region="cn",
              exp_manager={
                  "class": "MLflowExpManager",
                  "module_path": "qlib.workflow.expm",
                  "kwargs": {"uri": exp_uri, "default_exp_name": "Experiment"},
              })

    cal = list(D.calendar(freq="day"))
    as_of = pd.Timestamp(args.as_of) if args.as_of else pd.Timestamp(cal[-1])
    if as_of > pd.Timestamp(cal[-1]):
        print(f"[FATAL] as_of {as_of:%F} 晚于数据最新日 {pd.Timestamp(cal[-1]):%F}")
        return 1

    segments = build_segments(as_of, args.train_years, args.valid_years, args.test_years)
    print(f"[segments] train={segments['train']} valid={segments['valid']} "
          f"test={segments['test']}")
    if args.dry_run:
        return 0

    cfg = apply_segments(cfg, segments, as_of.strftime("%Y-%m-%d"))
    prod_ir = current_prod_ir(exp_uri)
    print(f"[prod] 线上 IR={prod_ir}")

    print("[1/4] 构建数据集（含 Alpha158PlusLab live 因子）")
    dataset = init_instance_by_config(cfg["task"]["dataset"])
    model = init_instance_by_config(cfg["task"]["model"])

    # 先在临时实验训练评估；过门禁再写入正式 experiment
    # 简化：直接写入正式 experiment，若未过门禁则删除本次 recorder 较难，
    # 改为：训练在正式 experiment，未过门禁打印 WARN 并写报告 promoted=False。
    # predict_daily 取 end_time 最新 → 未过门禁的模型仍会顶掉！
    # 因此：未过门禁时用 experiment_name=rolling_reject_* 隔离；过门禁才写正式名。

    with R.start(experiment_name="rolling_retrain_candidate"):
        print("[2/4] 训练 LightGBM")
        model.fit(dataset)
        R.save_objects(**{"params.pkl": model})
        recorder = R.get_recorder()
        print("[3/4] 信号与组合分析")
        SignalRecord(model, dataset, recorder).generate()
        SigAnaRecord(recorder, ann_scaler=252, ana_long_short=False).generate()
        PortAnaRecord(recorder, cfg["port_analysis_config"],
                      risk_analysis_freq="day").generate()
        metrics = recorder.list_metrics()
        cand_id = recorder.id
        pred = recorder.load_object("pred.pkl")

    ir = metrics.get(IR_KEY)
    ir_f = float(ir) if ir is not None else float("nan")
    print(f"[4/4] 候选 IR={ir_f}")

    promote = ir_f == ir_f and ir_f >= args.min_ir
    reason = ""
    if not promote:
        reason = f"IR {ir_f} < min_ir {args.min_ir}"
    elif not args.force and prod_ir is not None and ir_f < prod_ir - args.tol:
        promote = False
        reason = f"IR {ir_f:.3f} < 线上 {prod_ir:.3f} - tol {args.tol}"

    if promote:
        # 只把已拟合模型写入生产 experiment，避免二次全量 fit
        with R.start(experiment_name=EXPERIMENT):
            print("[promote] 写入生产 experiment baseline_alpha158_lgbm")
            R.save_objects(**{"params.pkl": model})
            recorder = R.get_recorder()
            cand_id = recorder.id
        sig_dir = QUANT / "data" / "signals"
        sig_dir.mkdir(parents=True, exist_ok=True)
        pred.to_csv(sig_dir / "latest_pred.csv")
        print(f"[OK] 已晋升 recorder={cand_id} IR={ir_f}")
    else:
        print(f"[SKIP] 未晋升：{reason or '未知'}（候选保留在 rolling_retrain_candidate）")

    rep_dir = QUANT / "data" / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    rep = render_report(segments, metrics, cand_id, promote, prod_ir)
    path = rep_dir / f"retrain_{dt.date.today():%Y%m%d}.md"
    path.write_text(rep)
    print(rep)
    print(f"[OK] 报告 {path}")
    return 0 if (promote or ir_f == ir_f) else 1


if __name__ == "__main__":
    raise SystemExit(main())
