# 中间宇宙实验（CSI800）

## 动机

- 生产：`csi500`（过 IR≈1.05）
- 全 A：`workflow_baseline_all.yaml`（IR 未过门禁）
- 中间步：`csi800` ≈ 沪深300 ∪ 中证500（约 800 只），基准 `SH000906`

## 先验证（不碰生产）

```bash
cd quant/research
../quant-venv/bin/python run_baseline.py \
  --config workflow_baseline_csi800.yaml \
  --experiment baseline_alpha158_lgbm_csi800 \
  --no-promote \
  --report-tag csi800
```

- 独立 MLflow 实验名，**不会**被 `predict_daily` 当成线上模型
- `--no-promote`：即便 IR≥0.8 也不写 `data/signals/latest_pred.csv`
- 报告：`data/reports/baseline_csi800_YYYYMMDD.md`

## 门禁

与生产相同：样本外含成本超额 **IR ≥ 0.8**；通过后再考虑阶段2 backtrader 复演与影子线，**再谈**是否改 `global.yaml` / 账户宇宙。
