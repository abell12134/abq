# 短线猎手（swing_hunter）

> **研究 / 学习用途，不构成投资建议。**  
> 与指数增强主线（LGBM + UMP + 订单）**严格隔离**：纯看板建议层，不改 `orders/`、不开新账户。

## 定位

| 项 | 说明 |
|----|------|
| 目标 | 5~15 个交易日内，收盘涨幅 **+10%**（分档目标 +15% / +20%）的赔率判断 |
| 候选池 | 三路汇流：量化强势（signals Top30）+ 事件催化（近 3 日公告/舆情）+ 跟踪延伸 |
| LLM 职责 | 催化识别 → 多空辩论 → 裁判预测；**不做**精确点位 |
| 规则职责 | 硬伤过滤（ST/停牌等）、跟踪状态机、模式挖掘触发 |
| 验证口径 | **收盘价**：T+1 开盘价入场；10 日内收盘 ≥+10% → hit；收盘 ≤−5% 且先于 hit → stopped |

## 流程（每日）

```
收盘后 evening（账户 use_swing_hunter: true）
  ├─ 跟踪更新（规则，零 LLM）
  ├─ Delta 跟踪（活跃票：仅「今日新增」公告/舆情，轻量 LLM）
  ├─ 候选池 + 硬伤过滤
  ├─ 舆情预采集（库内条目不足则 collect_for_instrument）
  ├─ LLM 深析 TopN（strict 门槛 → 无 predict 则 Judge 降一档 standard）
  ├─ predict 动作 → 写入 tracker（triggered，待 T+1 入场）
  └─ predictions/ + Markdown 日报 → 看板「短线猎手」页

hit 终态 → pattern_mine 写入 swing_patterns.yaml（候选模式，需样本外验证）
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
| `schema.py` | 契约常数、Prediction / TrackRecord、`latest_prediction_day()` |
| `candidates.py` | 三路候选 + 硬伤过滤 |
| `analyze.py` | Analyst/Bull/Bear/Judge、`apply_gate_fallback()` |
| `prompts_cn.py` | 中文提示词 + 两档 Judge |
| `sentiment_prep.py` | 分析前缺舆情则采集 |
| `delta_track.py` | 活跃票每日 delta（仅新增材料） |
| `tracker.py` | 收盘口径状态机 |
| `pattern_mine.py` | hit → `swing_patterns.yaml` |
| `report.py` | 每日 Markdown 日报 |
| `run_swing.py` | 日常 CLI + evening 挂钩 |
| `run_swing_eval.py` | 本地 TopN + DeepSeek Top5 双路评测 |
| `phase0_stats.py` | 历史候选池自然 hit 率（零 LLM） |
| `swing_patterns.yaml` | 达标案例模式库（类比 factor_lab 轻量版） |

**运行时数据**：`quant/data/overlays/swing_hunter/`

| 路径 | 内容 |
|------|------|
| `predictions/YYYY-MM-DD.json` | 当日全量预测（含 watch/reject） |
| `predictions/YYYY-MM-DD.md` | 日报（看板展示） |
| `tracker/{instrument}.json` | 单票全生命周期 + delta 时间线 |
| `catalog.json` | 活跃跟踪 + 累计统计 |
| `eval/YYYY-MM-DD/` | 评测：`pass1_*.json`、`pass2_*.json`、`comparison.md`、`traces/` |
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

# LLM 双路评测（评测结束自动 sync 到 predictions/ 供看板）
../quant-venv/bin/python overlays/swing_hunter/run_swing_eval.py --date 2026-08-05 --top-n 15 --refine-n 5

# Phase 0：历史候选池 hit 率（零 LLM）
../quant-venv/bin/python overlays/swing_hunter/phase0_stats.py --start 2024-01-02 --end 2026-06-30
```

## 账户与 evening 挂钩

`configs/accounts/live_manual_10k.yaml`：

```yaml
execution:
  use_swing_hunter: true   # evening 在舆情记忆之后跑 run_swing.py（fail-open）
```

`ops/run_daily.py` evening 顺序（live）：信号 → 清单 → UMP → 舆情硬伤筛 → 舆情记忆 → **短线猎手**。

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
| `GET /api/swing/tracking` | 跟踪列表 + 统计 |
| `GET /api/swing/detail/{instrument}` | 单票预测 + delta + 逐日 |
| `GET /api/swing/patterns` | 模式库 |
| `POST /api/swing/run` | 手动触发（白名单 IP） |

更新 swing 相关代码或 `server.py` 后需重启看板：

```bash
bash webapp/serve.sh stop && bash webapp/serve.sh start
```

静态资源（`app.js` / `style.css`）强刷：**Ctrl+F5**。

## LLM 路由

复用 `overlays/sentiment_memory/llm_router.py`：

- 高峰（9–12 / 14–18）→ 自部署 `LLM_PEAK_*`
- 闲时 / 失败 → DeepSeek `LLM_OFFPEAK_MODEL`
- 评测 Pass1 强制 `peak`，Pass2 强制 `offpeak`

## 有效性门禁（文化）

- 样本 **≥60** 笔结算后才谈 hit 率有效性
- 影子跑 **≥40** 交易日再考虑与主线联动
- Phase 0 参考：LGBM Top30 自然 hit 率约 **17.6%** vs 随机池 **14.5%**（收盘口径，2024–2026）

## 延伸阅读

- [quant/README.md](../quant/README.md) — 模块总览与命令索引  
- [FLOWCHARTS.md](FLOWCHARTS.md) — 流程图 §8  
- [设计实现方案.md](../设计实现方案.md) — §9.9
