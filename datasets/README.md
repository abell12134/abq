# 训练用 Qlib 行情数据包（分卷）

> ## ⚠ 风险声明（必读）
>
> **本仓库为量化研究 / 学习项目，不构成任何投资建议。**  
> **股市有风险，谨慎操作；据此交易的一切后果由使用者自行承担。**  
> 历史回测、模拟与纸面表现均 **不代表** 未来收益。

因 GitHub 单文件上限 100MB，全 A `cn_data`（约 839MB）打成 gzip 分卷：

```
qlib_cn_data.tar.gz.part_aa
qlib_cn_data.tar.gz.part_ab
...
qlib_cn_data.tar.gz.sha256
```

## 用法

克隆后首次训练/推理会自动解压：

```bash
git clone git@github.com:abell12134/abq.git
cd abq
python3 -m venv quant-venv && quant-venv/bin/pip install -r quant/requirements.txt
# 自动解压到 datasets/qlib_data/cn_data（约 1–3 分钟）
quant-venv/bin/python quant/ops/ensure_qlib_data.py
quant-venv/bin/python quant/research/run_baseline.py
```

`run_baseline.py` / `predict_daily.py` / `ops.common.init_qlib` 也会在启动时调用解压。

解压后的 `datasets/qlib_data/` **不入库**（见根 `.gitignore`）。
