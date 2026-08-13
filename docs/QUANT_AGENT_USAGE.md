# Quant Analysis Agent — 使用说明

面向系统所有者的日常操作手册。产品原则见 [QUANT_AGENT_PLAN.md](./QUANT_AGENT_PLAN.md)；实现架构见 [QUANT_AGENT_ARCHITECTURE.md](./QUANT_AGENT_ARCHITECTURE.md)。

**入口：** [http://43.159.136.65:8000/agent/](http://43.159.136.65:8000/agent/)  
**原则：** 只分析、不交易。界面数字来自确定性结算账本；Supervisor（LLM）只编排与解释，**不产生 claim**。

---

## 1. 先看懂页面

| 导航 | 用途 |
|------|------|
| **今日放行** | 主屏。按 `released / hold / quarantine` 看今日可结算观点与成绩单（n、Wilson CI） |
| **预测账本** | 全部 L1/L2 预测列表与状态 |
| **结算台** | 到期结算结果（hit / miss / PIC） |
| **成绩校准** | 方向命中率 vs 区间 PIC **分栏**，不混算 |
| **策略信任** | L3：champion / challenger / paused 与信任权重 |
| **研究旁路** | factor_lab 同步、Holm 晋升门、手动晋升 |
| **系统** | 数据日、口径版本、Peak LLM、鉴权状态 |

右侧 **Supervisor**：附着一条预测后提问，或问系统/策略总览。

### 印章含义

| 章 | 含义 |
|----|------|
| **released** | 已毕业且样本足够，可进主推荐区 |
| **hold** | shadow / 冷启动，仅观察 |
| **quarantine** | 样本不足或策略暂停，不得当主推荐 |
| **observe** | 已结算或 L2 等，账本可见、非主荐股 |

混权分（若有）：champion + 已晋升 challenger 的加权分，用于排序参考。

---

## 2. 日常怎么用

### 盘前 / 盘中

1. 打开「今日放行」，先看 **released** 区与成绩单样本量。
2. 点开一条预测 → 右侧 Supervisor 会附着 `pred_id`。
3. 用快捷芯片或输入，例如：「诊断成绩与失效条件」。
4. 等待编排（Peak LLM，常见 30–120s，代理最长约 600s）。简报按章节折叠阅读；可点「工具轨迹」看取数 meta。

### 盘后

流水线已挂在 `ops/run_daily`：

- **evening**：出信号后 `run_emit`（+ challenger shadow）
- **postclose**：净值后 `run_track`（结算到期预测 → 刷新信任 → 评估 challenger）

手动补跑：

```bash
cd quant
../quant-venv/bin/python agent/jobs/run_emit.py --day YYYY-MM-DD
../quant-venv/bin/python agent/jobs/run_shadow.py --day YYYY-MM-DD
../quant-venv/bin/python agent/jobs/run_track.py --day YYYY-MM-DD
```

### 研究晋升

1. 打开「研究旁路」→ 同步 factor_lab。
2. 看 Holm / 二项门结果；`pass_gate` 才可晋升。
3. 晋升后信任权重才进主推荐混权；**未晋升 challenger 权重为 0**。

---

## 3. 服务启停

需两套常驻进程（公网走 `:8000`）：

```bash
cd quant
bash agent_api/serve.sh start    # :8010 Agent API
bash webapp/serve.sh start       # :8000 看板 + /agent/ 静态 + API 反代
```

| 命令 | 作用 |
|------|------|
| `bash agent_api/serve.sh {start\|stop\|restart\|status\|logs}` | Agent API |
| `bash webapp/serve.sh {start\|stop\|restart\|status\|logs}` | Web |

前端有改动后：

```bash
cd quant/agent-ui && npm run build
# webapp 已挂 dist，一般无需重启；若缓存异常可 restart webapp
```

健康检查：

```bash
curl -sS http://127.0.0.1:8010/api/health
curl -sS http://127.0.0.1:8000/agent/api/health
```

---

## 4. 配置要点

凭证在 `quant/configs/secret.env`（模板：`secret.env.example`）：

| 变量 | 说明 |
|------|------|
| `LLM_PEAK_*` | Supervisor 优先用的自部署模型 |
| `AGENT_LLM_PEAK_ONLY=1` | 禁止回落闲时 DeepSeek |
| `AGENT_LLM_TIMEOUT` | LLM 超时秒数（默认 600） |
| `AGENT_API_TOKEN` | 可选 Bearer；可绕过/补充 IP 白名单 |
| `AGENT_API_TOKEN_STRICT=1` | 写操作强制 Bearer |
| `AGENT_USE_LANGGRAPH=0` | 关闭 LangGraph，直连 supervisor |

IP 白名单与 webapp 共用：`configs/webapp.local.yaml`。

---

## 5. 日志与排障

| 日志 | 路径 |
|------|------|
| Agent API 访问 | `quant/data/logs/agent_api.log` |
| Web / 反代 | `quant/data/logs/webapp.log` |

```bash
bash agent_api/serve.sh logs          # tail -f API
tail -f data/logs/webapp.log | grep supervisor
```

常见情况：

| 现象 | 处理 |
|------|------|
| Supervisor「编排中」很久 | Peak LLM 慢；看 `agent_api.log` 是否已有 `POST .../supervisor/ask`；代理超时 600s |
| 页面 502 / Agent 未启动 | `bash agent_api/serve.sh start` |
| 403 forbidden | IP 不在白名单，或配置 `AGENT_API_TOKEN` 后带 `Authorization: Bearer …` |
| 今日放行空 / 全 hold | 尚未 emit，或仍在 shadow（需结算样本毕业） |
| 账本演示水印 | SQLite 无真实行，API 回退 demo |

金标准自检：

```bash
cd quant && ../quant-venv/bin/python agent/jobs/run_tests.py
```

---

## 6. 口径与重算（少用）

当前活跃口径见系统页 / `GET /api/health` 的 `caliber`（如 `caliber.v1.1.events`）。

干跑 / 写库重算：

```bash
curl -X POST 'http://127.0.0.1:8010/api/admin/recompute?dry_run=true&limit=50'
# 确认后再 dry_run=false
```

旧 outcome 保留在 `error_metrics.outcomes_by_caliber`，与新口径并存。

---

## 7. 不该指望它做什么

- **不下单**、不接券商、不算真实滑点/冲击。
- L2 回撤/波动是**纸面**指标。
- 无成绩单或 n&lt;30 → 不得当主推荐。
- LLM 回复里的数字若与账本冲突，**以账本为准**。
