# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

React (Agent 控制台独立前端)；后端延续 `quant/` Python（FastAPI / LangGraph 编排 / 确定性结算与校准）。现有 `quant/webapp` 多账户运维看板保留，与 Agent 控制台分入口、可互链。

## Users

主用户：系统所有者本人——在 A 股真金白银环境下，用可结算预测与追踪成绩做研究与人工决策参考（系统本身不下单）。

场景：盘前看今日观点与信任状态；盘中/随时用 Supervisor 做单票/组合/策略诊断；盘后核对应到期结算与权重变化；研究时段触发搜因子（只进 challenger）。

## Product Purpose

独立的 **A股量化分析 Agent 控制台**：把 LGBM 等模型产出的主张写成可结算预测，到期结算，用成绩单校准信任，并由 Supervisor（LLM）编排分析会话与解释——**评估预测质量，不执行交易**。

成功：任意可见推荐可追溯到预测账本与样本量/置信区间；调权可解释；新因子不能「回测好看就上主推荐」；用户同时看到今日观点、历史准不准、系统当前信任谁。

## Positioning

不是聊天荐股机器人，也不是账户 NAV 看板。差异化机制是：**可证伪的预测账本 + 双时间线（回测准入 ≠ 在线追踪）+ champion–challenger 晋升**；LLM 只编排与解释，不产生 claim 数值。

## Operating Context

- 后端与数据：`quant/`（Qlib/LGBM、`factor_lab`、overlays、`ops/run_daily`、baostock 等）
- 运维看板：`quant/webapp`（账户/NAV/舆情/短线猎手）——并列产品，非本控制台壳
- 方案权威：`docs/QUANT_AGENT_PLAN.md`（L1/L2 预测账本、L3 状态机、结算口径、Critic、冷启动）
- 实现架构：`docs/QUANT_AGENT_ARCHITECTURE.md`（分层边界、模块地图、数据流；图见 `docs/diagrams/quant-agent-architecture.drawio`）
- 使用说明：`docs/QUANT_AGENT_USAGE.md`
- 节奏：盘前出预测、盘后 Track 结算、会话中可搜因子、周/月晋升评审
- 真金白银约束：界面与文案不得暗示「目标收益可实现」；纸面指标与实盘成交反馈严格区分；shadow / 样本不足不得进主推荐

## Capabilities and Constraints

**能力（完整产品范围，非裁剪版 MVP 心智）：**

- L1 单票 / L2 组合可结算预测与成绩单
- L3 策略注册与信任权重（champion / challenger / paused）
- Track 结算、分桶校准、结算口径版本 `settlement_caliber`
- Supervisor：意图路由（单票 / 组合 / 策略诊断）、会话编排、报告汇总；内层取数 + Critic
- 旁路搜因子（复用 `factor_lab`）+ 多重检验门 + 晋升可视化
- 冷启动 shadow 水印与毕业条件
- 与运维看板互链（账户执行反馈只读引用，不混入口）

**约束：**

- 不下单、不接券商；分析 Agent 与执行账本隔离
- claim 来自模型，LLM 不写预测数值
- 结算 / 校准 / 命中判定为确定性代码
- React 为 Agent 控制台唯一前端栈（本表面）

**部署默认：** Agent API `:8010`，React UI `:5173`（dev）/ 可静态挂到 API；运维看板仍 `:8000`。鉴权与 webapp 共用 IP 白名单（`configs/webapp.local.yaml`）；生产建议再加反向代理认证。

## Brand Commitments

- 产品称呼：量化分析 Agent / Supervisor（与「多账户看板」区分）
- 语气：可证伪、可追溯、反夸大；禁「必涨 / 稳赚」等不可证伪措辞
- 用户明确：独立页面、单独 Agent、生产级、真金白银、Supervisor 一并交付、React

## Evidence on Hand

- 方案：`docs/QUANT_AGENT_PLAN.md`
- 现有信号与 overlays：`quant/research/predict_daily.py`、`quant/overlays/*`（含 swing_hunter tracker 可抽象生命周期）
- 运维 UI（并列参考，非视觉权威）：`quant/webapp/`
- 尚无生产预测账本表、尚无 Agent React 应用骨架

## Product Principles

1. **无成绩单不荐股**：主推荐区只展示已毕业、样本量达标的预测；其余明确标 shadow / 样本不足。
2. **可结算才算预测**：界面每条主张暴露标的、claim、期限、基准、口径版本与 resolve 状态。
3. **LLM 编排、代码算账**：聊天与报告不得覆盖或改写结算数字。
4. **双时间线可见**：回测/伪成绩单与在线追踪成绩视觉分离，禁止混读成「已验证收益」。
5. **真金白银诚实**：纸面回撤/换手与实盘执行偏差分开展示；任何目标收益输出必须带历史可达性与失效条件。

## Accessibility & Inclusion

未单独约定标准；默认桌面优先的专业工作台，支持键盘可达的主导航与表单，对比度满足可读的数据密集 UI。
