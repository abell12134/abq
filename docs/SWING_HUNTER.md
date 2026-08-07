# 短线猎手（swing_hunter）

> **研究 / 学习用途，不构成投资建议。**  
> 与指数增强主线（LGBM + UMP + 订单）**严格隔离**：纯看板建议层，不改 `orders/`、不开新账户。

## 定位

| 项 | 说明 |
|----|------|
| 目标 | 5~15 个交易日内，收盘涨幅 **+10%**（分档目标 +15% / +20%）的赔率判断；猎「方正科技式」短线高赔率票 |
| 候选池 | **四路**汇流：量化强势 Top30 + 事件催化 + 跟踪延伸（多账户并集）+ **短线动量/突破**；live 模式库加权 |
| LLM 职责 | 催化识别 → 多空辩论 → 裁判预测；**不做**精确点位 |
| 规则职责 | 硬伤过滤（ST/停牌等）、跟踪状态机、模式挖掘触发 |
| 验证口径 | **收盘价**：T+1 开盘价入场；10 日内收盘 ≥+10% → hit；收盘 ≤−5% 且先于 hit → stopped |

## 流程（每日）

```
收盘后 evening（各账户 use_swing_hunter: true）
  ├─ 跟踪更新（规则，零 LLM）
  ├─ 若同日 predictions/{day}.done 且 status=ok → 跳过新预测 LLM（记 skipped_by_accounts）
  ├─ Delta 跟踪（活跃票：仅「今日新增」公告/舆情，轻量 LLM）
  ├─ 候选池 + 硬伤过滤（四路 + 模式加权）
  ├─ 舆情预采集（库内条目不足则 collect_for_instrument）
  ├─ LLM 深析全部未过滤候选（默认不截断；`--max-llm N` 可限流）
  │    strict 门槛 → 无 predict 则 Judge 降一档 standard
  ├─ predict 动作 → 写入 tracker（triggered，待 T+1 入场）
  └─ predictions/ + Markdown 日报 → 看板「短线猎手」页

hit 终态 → pattern_mine 写入 swing_patterns.yaml（candidate）
实盘 fills 达标 → mine_live_cases → status=live_case（候选加权 + 评测夹具）
```

## 预测门槛（gate_tier）

| 档位 | `gate_tier` | 说明 |
|------|-------------|------|
| 严格档 | `strict` | 默认 `watch`；催化明确 + 量价配合 + 无硬伤才 `predict` |
| 标准档 | `standard` | 降一档：弱催化 + 量价尚可、无硬伤可 `predict`；降档时**仅重跑 Judge**（省 token） |

落盘字段：`prediction.meta.gate_tier`、`gate_label`、`gate_fallback`、`gate_prev_tier`。

## 目录与数据

**代码**：`quant/overlays/swing_hunter/`

| 文件 | 作用 |
|------|------|
| `schema.py` | 契约常数、Prediction / TrackRecord、同日幂等 `already_predicted_today` |
| `candidates.py` | 四路候选 + live 模式加权 + 硬伤过滤 |
| `analyze.py` | Analyst/Bull/Bear/Judge、`apply_gate_fallback()` |
| `prompts_cn.py` | 中文提示词 + 两档 Judge |
| `sentiment_prep.py` | 分析前缺舆情则采集 |
| `delta_track.py` | 活跃票每日 delta（仅新增材料） |
| `tracker.py` | 收盘口径状态机 |
| `pattern_mine.py` | hit / 实盘 fills → `swing_patterns.yaml` |
| `mine_live_cases.py` | 从 fills 挖 live_case CLI |
| `assert_live_cases.py` | 实盘案例断言（候选覆盖 + 收益档 + 历史裁判复盘） |
| `report.py` | 每日 Markdown 日报 |
| `run_swing.py` | 日常 CLI + evening 挂钩（`--force` 可覆盖同日跳过） |
| `run_swing_eval.py` | 本地 TopN + DeepSeek Top5 双路评测 |
| `phase0_stats.py` | 历史候选池自然 hit 率（零 LLM） |
| `swing_patterns.yaml` | 达标案例模式库（candidate / live_case） |

**运行时数据**：`quant/data/overlays/swing_hunter/`

| 路径 | 内容 |
|------|------|
| `predictions/YYYY-MM-DD.json` | 当日全量预测（含 watch/reject） |
| `predictions/YYYY-MM-DD.md` | 日报（看板展示） |
| `tracker/{instrument}.json` | 单票全生命周期 + delta 时间线 |
| `catalog.json` | 活跃跟踪 + 累计统计 |
| `eval/YYYY-MM-DD/` | 评测：`pass1_*.json`、`pass2_*.json`、`comparison.md`、`traces/` |
| `eval/cases/live_wins.yaml` | 实盘达标夹具（方正等） |
| `deltas/YYYY-MM-DD/` | 按日 delta JSON 镜像 |
| `patterns_mined.jsonl` | 模式挖掘 JSON 镜像 |

## 命令

```bash
cd quant

# 完整跑（跟踪 + delta + 候选 + LLM + 报告）
../quant-venv/bin/python overlays/swing_hunter/run_swing.py --date 2026-08-05 --account live_manual_10k

# 仅跟踪 + delta（不调候选 LLM）
../quant-venv/bin/python overlays/swing_hunter/run_swing.py --track-only

# 管线联调（不调 LLM）
../quant-venv/bin/python overlays/swing_hunter/run_swing.py --dry-run --max-llm 3

# 强制重跑（忽略同日已有 ok 预测）
../quant-venv/bin/python overlays/swing_hunter/run_swing.py --force --date 2026-08-05

# 实盘 fills → live_case 模式库
../quant-venv/bin/python overlays/swing_hunter/mine_live_cases.py --account live_manual_10k

# 实盘案例断言（零 LLM）
../quant-venv/bin/python overlays/swing_hunter/assert_live_cases.py

# LLM 双路评测（评测结束自动 sync 到 predictions/ 供看板）
../quant-venv/bin/python overlays/swing_hunter/run_swing_eval.py --date 2026-08-05 --top-n 15 --refine-n 5

# Phase 0：历史候选池 hit 率（零 LLM）
../quant-venv/bin/python overlays/swing_hunter/phase0_stats.py --start 2024-01-02 --end 2026-06-30
```

## 账户与 evening 挂钩

四条账户均开启（共享 `data/overlays/swing_hunter/`；同日只跑一次贵价 LLM）：

```yaml
# live_manual_10k / research_sim_100k / shadow_ctrl_sim / shadow_ta_sim
execution:
  use_swing_hunter: true   # evening 在舆情记忆之后跑 run_swing.py（fail-open）
```

`ops/run_daily.py` evening 顺序（live）：信号 → 清单 → UMP → 舆情硬伤筛 → 舆情记忆 → **短线猎手**。

同日幂等：`predictions/{day}.done` 且 `status=ok` 时，后续账户只更新跟踪/delta，跳过新预测 LLM；`--force` / `--force-swing` 可覆盖。延伸宇宙对所有 `use_swing_hunter` 账户持仓/订单取并集，避免 research 先跑漏掉实盘持仓。

## 看板

页签：**短线猎手**（`webapp/templates/index.html` + `static/app.js`）

- 统计卡：活跃 / 结算 / hit 率 / 舆情补齐
- **每日报告**：结构化卡片（预测/观察/否决汇总 + 个股卡片）
- **LLM 评测**：门槛说明 + Pass1/Pass2 表格（日期下拉）
- 活跃跟踪、最新预测表（含门槛列）、已结算、模式库
- 点击标的 → 详情弹窗（理由 / delta / 逐日收盘）

**API**（`webapp/server.py`）：

| 接口 | 说明 |
|------|------|
| `GET /api/swing/catalog` | 目录 + 最新预测日全量决策 |
| `GET /api/swing/report` | 日报 Markdown + meta |
| `GET /api/swing/eval?day=` | 评测 comparison.md |
| `GET /api/swing/patterns` | 模式库 |
| `GET /api/swing/tracker/{inst}` | 单票跟踪 |
| `GET /api/swing/job` | 当前/最近一次运行进度（进度条） |
| `POST /api/swing/run` | 后台触发 run_swing |

## 备注

- 研究用途，不构成投资建议。
- 标杆案例：方正科技 SH600601（2026-08-04@9.24 → 08-06@11.42，约 +23.6%，tier3）。
