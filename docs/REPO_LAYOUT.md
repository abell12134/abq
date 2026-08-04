# abq 仓库目录结构

> ## ⚠ 风险声明（必读）
>
> **本仓库为量化研究 / 学习项目，不构成任何投资建议。**  
> **股市有风险，谨慎操作；据此交易的一切后果由使用者自行承担。**  
> 历史回测、模拟与纸面表现均 **不代表** 未来收益。

GitHub 仓库 `abell12134/abq` 只包含**自研代码**，目录约定如下：

```
abq/                              ← 仓库根（clone 后 cd abq）
├── .gitignore                    ← 根 ignore：排除 venv、上游 clone
├── README.md                     ← 项目总览、亮点、流程图、快速开始
├── LICENSE
├── docs/
│   ├── FLOWCHARTS.md             ← 流程图（ASCII + Mermaid）
│   └── REPO_LAYOUT.md            ← 本文件
├── 设计实现方案.md                ← 完整架构与阶段验收（可选）
└── quant/                        ← 自研系统（所有 Python 代码在此）
    ├── configs/
    │   ├── global.yaml
    │   ├── secret.env.example    ← 复制为 secret.env，勿提交
    │   └── accounts/
    ├── data_pipeline/
    ├── research/
    ├── factor_lab/
    ├── validation/
    ├── execution/
    ├── ops/
    ├── overlays/                 ← TA 影子否决 / 舆情硬伤筛 / 舆情长期记忆
    ├── webapp/
    ├── contracts/
    ├── requirements.txt
    ├── README.md
    └── .gitignore
```

## 不在仓库内的内容

| 路径 | 说明 |
|------|------|
| `quant-venv/` 或 `venv/` | 本地虚拟环境，`pip install -r quant/requirements.txt` |
| `qlib/` `vnpy/` 等 | 上游开源项目，单独 clone 到**仓库外**对照学习 |
| `quant/data/` | 运行时数据（含 `overlays/sentiment_memory/` 向量与报告），首次运行后本地生成 |
| `~/.qlib/qlib_data/cn_data` | 市场数据，按 README 下载 |
| `quant/configs/secret.env` | LLM / Cursor / 自部署高峰端点密钥 |

## 本地开发工作区建议

若需同时保留上游 clone，推荐布局：

```
~/workspace/
├── abq/                 ← git clone 的本仓库
├── quant-venv/          ← 虚拟环境（与 abq 同级）
├── qlib/                ← 可选，上游对照
└── vnpy/                ← 可选
```

在 `abq/quant` 下执行脚本时：

```bash
cd ~/workspace/abq/quant
../../quant-venv/bin/python research/run_baseline.py
```
