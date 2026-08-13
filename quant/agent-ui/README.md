# Quant Analysis Agent

独立 **可结算预测** 控制台（React）+ API（FastAPI）+ 确定性账本（SQLite）。

架构：[`docs/QUANT_AGENT_ARCHITECTURE.md`](../../docs/QUANT_AGENT_ARCHITECTURE.md) · 方案：[`docs/QUANT_AGENT_PLAN.md`](../../docs/QUANT_AGENT_PLAN.md) · **使用说明：[`docs/QUANT_AGENT_USAGE.md`](../../docs/QUANT_AGENT_USAGE.md)**

## 公网入口

**http://43.159.136.65:8000/agent/**

## 启动

```bash
cd quant && bash agent_api/serve.sh          # :8010
cd quant/agent-ui && npm run build           # 有前端改动时
cd quant && bash webapp/serve.sh stop && bash webapp/serve.sh start
```

## 账本 / Track

```bash
python agent/jobs/run_emit.py --day YYYY-MM-DD
python agent/jobs/run_track.py --day YYYY-MM-DD
python agent/jobs/run_tests.py
```

已挂入 `ops/run_daily.py`：evening → `jobs/run_emit`（+ shadow）；postclose → `jobs/run_track`。

## 后端分层（`quant/agent/`）

| 包 | 职责 |
|----|------|
| `core` | 口径、账本、成绩单 |
| `settlement` | 结算 / Track / 口径重算 |
| `prediction` | emit / 影子 / Critic / 特征归档 |
| `trust` | L3 权重、晋升门、混权推荐 |
| `orchestration` | Supervisor / LangGraph / API enrich |
| `jobs` | CLI 入口 |

## 完成度

| 项 | 状态 |
|----|------|
| L1 方向结算 + SQLite 账本 + Track | 已通 |
| 成绩单 / Wilson / 分桶校准 / 冷启动毕业 | 已通 |
| L3 信任权重（Wilson 降权/暂停） | 已通 |
| L2 纸面组合发射 + 三项裁决结算 | 已通 |
| Supervisor（工具取数 + Peak LLM，代理 600s） | 已通 |
| factor_lab 同步 + 二项/Holm 晋升门 + 研究旁路 UI | 已通 |
| challenger 影子预测 | 已通 |
| 区间 claim 发射 | 已通 |
| LangGraph plan→execute 骨架 | 已通 |
| 分层目录（无根 shim） | 已通 |
