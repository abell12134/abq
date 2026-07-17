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
    args = parser.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text())

    sys.path.insert(0, str(HERE))  # alpha158_plus_lab
    qlib.init(provider_uri=cfg["qlib_init"]["provider_uri"], region="cn")

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

    sig_dir = QUANT / "data" / "signals"
    sig_dir.mkdir(parents=True, exist_ok=True)
    pred.to_csv(sig_dir / "latest_pred.csv")

    report = render_report(metrics, recorder.id, cfg)
    rep_dir = QUANT / "data" / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    rep_path = rep_dir / f"baseline_{dt.date.today():%Y%m%d}.md"
    rep_path.write_text(report)
    print(report)
    print(f"[OK] 报告已写入 {rep_path}")

    # 阶段1验收门槛：样本外信息比率 >= 0.8
    ir = metrics.get("1day.excess_return_with_cost.information_ratio")
    if ir is not None and ir < 0.8:
        print(f"[WARN] 样本外 IR={ir:.2f} 未达验收门槛 0.8，需要迭代")
    return 0


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
