"""运行基线工作流并产出报告。

用法：
    python run_baseline.py [--config workflow_baseline.yaml]

产出：
  - MLflow 实验记录（qlib recorder，可复现）
  - data/reports/baseline_YYYYMMDD.md  关键指标报告
  - data/signals/latest_pred.csv       全部测试期信号（供验证层/导出用）
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

# 新版 mlflow 默认禁用文件存储后端，qlib recorder 依赖它
os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")

import pandas as pd
import yaml

import qlib
from qlib.utils import init_instance_by_config
from qlib.workflow import R
from qlib.workflow.record_temp import PortAnaRecord, SigAnaRecord, SignalRecord

HERE = Path(__file__).resolve().parent
QUANT = HERE.parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(HERE / "workflow_baseline.yaml"))
    parser.add_argument("--min-ir", type=float, default=0.8,
                        help="阶段1验收门槛：样本外 IR 低于此值不晋升为线上模型")
    args = parser.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text())

    # 确保仓库内分卷 Qlib 数据已解压（git clone 后首次自动解压）
    sys.path.insert(0, str(QUANT / "ops"))
    from ensure_qlib_data import extract, resolve_provider_uri
    extract(force=False)
    cfg["qlib_init"]["provider_uri"] = resolve_provider_uri(
        cfg["qlib_init"].get("provider_uri")
    )

    sys.path.insert(0, str(HERE))  # alpha158_plus_lab
    # 实验记录必须写到 research/mlruns —— predict_daily.load_latest_model 只从这里读，
    # 否则 qlib.init 默认按 CWD 落到 quant/mlruns，重训模型永远不会被每日信号采用。
    exp_uri = "file:" + str(HERE / "mlruns")
    qlib.init(provider_uri=cfg["qlib_init"]["provider_uri"], region="cn",
              exp_manager={
                  "class": "MLflowExpManager",
                  "module_path": "qlib.workflow.expm",
                  "kwargs": {"uri": exp_uri, "default_exp_name": "Experiment"},
              })

    print("[1/4] 构建数据集（Alpha158 因子计算，耗时较长）")
    dataset = init_instance_by_config(cfg["task"]["dataset"])
    model = init_instance_by_config(cfg["task"]["model"])

    with R.start(experiment_name="baseline_alpha158_lgbm"):
        print("[2/4] 训练 LightGBM")
        model.fit(dataset)
        R.save_objects(**{"params.pkl": model})
        recorder = R.get_recorder()

        print("[3/4] 生成信号与信号分析")
        SignalRecord(model, dataset, recorder).generate()
        SigAnaRecord(recorder, ann_scaler=252, ana_long_short=False).generate()

        print("[4/4] 组合回测")
        pa_cfg = cfg["port_analysis_config"]
        PortAnaRecord(recorder, pa_cfg, risk_analysis_freq="day").generate()

        # ---- 汇总指标 ----
        metrics = recorder.list_metrics()
        pred: pd.DataFrame = recorder.load_object("pred.pkl")
        rec_id = recorder.id

    report = render_report(metrics, rec_id, cfg)
    rep_dir = QUANT / "data" / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    rep_path = rep_dir / f"baseline_{dt.date.today():%Y%m%d}.md"
    rep_path.write_text(report)
    print(report)
    print(f"[OK] 报告已写入 {rep_path}")

    # 阶段1验收门槛：样本外信息比率 >= min_ir。
    # 关键纪律：未过门禁的模型【绝不】晋升为线上信号——predict_daily.load_latest_model
    # 取本 experiment 里 end_time 最新的 recorder，因此若把不达标模型留在实验里，它会
    # 自动顶掉已验证模型（2026-07 事故根因）。故未过门禁时：不写 latest_pred.csv，
    # 并把本次 recorder 移出实验目录到 mlruns_rejected/ 隔离，保持线上仍用上一版已验证模型。
    ir = metrics.get("1day.excess_return_with_cost.information_ratio")
    sig_dir = QUANT / "data" / "signals"
    sig_dir.mkdir(parents=True, exist_ok=True)
    if ir is not None and ir < args.min_ir:
        print(f"[WARN] 样本外 IR={ir:.3f} 未达验收门槛 {args.min_ir}，不晋升为线上模型，需迭代")
        moved = _quarantine_recorder(rec_id, ir)
        if moved:
            print(f"[SKIP] 已将不达标 recorder 隔离到 {moved}；latest_pred.csv 保持上一版不变")
        else:
            print("[WARN] 未能定位 recorder 目录做隔离，请人工核查 research/mlruns")
        return 0
    pred.to_csv(sig_dir / "latest_pred.csv")
    print(f"[OK] IR={ir} 达标，已晋升：写入 {sig_dir / 'latest_pred.csv'}")
    return 0


def _quarantine_recorder(rec_id: str, ir: float | None) -> Path | None:
    """把未过门禁的 recorder 从 experiment 目录移到 mlruns_rejected/，
    使 predict_daily 不再把它当作最新线上模型。返回目标路径或 None。"""
    import shutil
    mlruns = HERE / "mlruns"
    for exp_dir in mlruns.glob("*"):
        cand = exp_dir / rec_id
        if cand.is_dir():
            rej = HERE / "mlruns_rejected"
            rej.mkdir(parents=True, exist_ok=True)
            tag = f"{rec_id[:8]}_{dt.date.today():%Y%m%d}_ir{ir:.2f}" if ir is not None \
                else f"{rec_id[:8]}_{dt.date.today():%Y%m%d}"
            dst = rej / tag
            shutil.move(str(cand), str(dst))
            return dst
    return None


def render_report(m: dict, rec_id: str, cfg: dict) -> str:
    def g(key, fmt="{:.4f}"):
        v = m.get(key)
        return fmt.format(v) if v is not None else "N/A"

    market = cfg.get("market", "?")
    sk = cfg["port_analysis_config"]["strategy"]["kwargs"]
    bench = cfg["port_analysis_config"]["backtest"]["benchmark"]
    title = (f"Alpha158 + LGBM, {market}, TopK{sk['topk']}"
             f"/n_drop{sk['n_drop']}/hold{sk.get('hold_thresh', 1)}")

    return f"""# 基线回测报告（{title}）

> ## ⚠ 风险声明（必读）
>
> **本报告为量化研究 / 学习用途，不构成任何投资建议。**  
> **股市有风险，谨慎操作；据此交易的一切后果由使用者自行承担。**  
> 历史回测、模拟与纸面表现均 **不代表** 未来收益。

- 生成时间: {dt.datetime.now():%Y-%m-%d %H:%M}
- Recorder ID: {rec_id}（MLflow 可复现）
- 基准: {bench}

## 信号质量（测试期样本外）
| 指标 | 值 |
|---|---|
| IC | {g('IC')} |
| ICIR | {g('ICIR')} |
| Rank IC | {g('Rank IC')} |
| Rank ICIR | {g('Rank ICIR')} |

## 组合表现（含交易成本，相对基准 {bench}）
| 指标 | 值 |
|---|---|
| 年化超额收益 | {g('1day.excess_return_with_cost.annualized_return', '{:.2%}')} |
| 信息比率 IR | {g('1day.excess_return_with_cost.information_ratio')} |
| 超额最大回撤 | {g('1day.excess_return_with_cost.max_drawdown', '{:.2%}')} |

验收门槛：IR >= 0.8（阶段1）；后续还需通过 backtrader 复演（阶段2）。
"""


if __name__ == "__main__":
    sys.exit(main())
