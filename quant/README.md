# quant — A 股 AI 量化交易系统（自有代码）

总体设计见仓库根目录《设计实现方案.md》。本目录是方案中的"自有系统"部分，
上游开源项目（qlib / rd-agent / backtrader / vnpy / abu）只读引用，不直接修改。

> **流程图**：根目录 [README.md](../README.md#流程图) 与 [docs/FLOWCHARTS.md](../docs/FLOWCHARTS.md)  
> （含 ASCII 图，Cursor 里可直接看；GitHub 上 Mermaid 可渲染成图形）

## 当前进度

- [x] 阶段 1：数据底座 + 研究基线（**Plan C** csi500，净超额 **IR 1.05**，过验收门槛 0.8；recorder `826b98ae`）
- [x] 阶段 2：backtrader 验证闭环（Plan C：复演 vs qlib 年化差异 **−2.70pct**≤3pct；0.2%滑点超额 **7.86%**≥5%，双门槛通过）
- [x] 阶段 2b：UMP 裁判模型（借鉴 abu，对买入信号二次否决）— 样本外 A/B：超额IR 0.21→0.48
- [x] 阶段 3：LLM 因子迭代闭环 + 五道准入关卡（3 轮迭代，2 个因子过全部关卡并提升库组合样本外 IC）
- [~] 阶段 4：路线一每日闭环已实跑（`live_manual_10k` manual）；7 月曾因买入通道故障被动清仓至 2 只/约 50% 现金，08-03 已按新模型重出补仓单（目标 5 只）
- [~] 阶段 5a：后台常驻看板 + 内置定时（FastAPI:8000 + APScheduler），双线 + TA 影子线并行中（**TA 未过门禁，不上 live**）
- [ ] 阶段 5：全自动实盘（miniQMT）

## 环境

```bash
python3 -m venv ../quant-venv
../quant-venv/bin/pip install -r requirements.txt
```

机器要求：内存 ≥3GB（已配 8G swap 兜底），磁盘 ≥5GB。

## 数据

- 唯一真相源：仓库内 `datasets/qlib_data/cn_data`（由 `datasets/qlib_cn_data.tar.gz.part_*` 首次自动解压）
  或兼容旧路径 `~/.qlib/qlib_data/cn_data`
- 历史底座：[investment_data](https://github.com/chenditc/investment_data) 社区数据包已打进仓库分卷；
  `git clone` 后运行 `python quant/ops/ensure_qlib_data.py`（或直接 `run_baseline.py`）即可解压
- 每日增量：baostock 抓取（后复权价 + factor 列，与官方格式一致）

```bash
# 每日收盘后由常驻服务的内置定时自动执行（webapp/server.py，见"后台看板"一节；
# 不跑常驻服务时也可用 ops/crontab.example）。手动单步：
python data_pipeline/update_daily.py     # 抓取 → 质检 → 入库，质检不过即阻断
python research/predict_daily.py         # 生成当日信号 → data/signals/
python research/predict_range.py --start <起> --end <止>  # 批量补一段区间信号
```

## 研究基线

```bash
python research/run_baseline.py          # 训练 + 回测 + 报告
python research/rerun_backtest.py --recorder <id> --topk 50 --n_drop 3 --hold_thresh 10
                                         # 不重训，仅调组合参数快速迭代
```

- 配置：`research/workflow_baseline.yaml`（Alpha158 + **LGBM Plan C**，**中证500 `csi500`**，基准 SH000905；
  TopK50 / n_drop3 / hold_thresh10）。Plan C = 降学习率 + 加强 L1/L2 + 更长 early-stop，
  缓解 valid l2 过早恶化；对照实验配置见 `workflow_baseline_planC.yaml`（已并入生产 yaml）。
  全 A 实验配置见 `workflow_baseline_all.yaml`（IR 未过关，勿作生产）。
- 线上模型：MLflow recorder `826b98ae354e4f94bcfe0f3358b94089`
  （样本外 IR **1.0459** / 年化超额 **11.19%**；未过门禁的重训会进 `mlruns_rejected/`，不会被 `predict_daily` 采用）
- 切分：训练 2010-2021 / 验证 2022-2023 / 测试 2024 至今（严格样本外；当前回测窗至 2026-07-31）
- 标签：5 日开盘价收益（1 日标签换手过高、被成本吃光，见 yaml 注释）
- 回测约束：涨跌停 9.5% 限制、次日开盘价成交、双边费率含印花税与滑点
- 产出：`data/reports/baseline_*.md`、`data/signals/latest_pred.csv`、MLflow 记录（`research/mlruns/`）

### 换手率是阶段1的关键发现

毛超额（无成本）IR 约 0.6~0.8，但**每日全量调仓时交易成本把超额全部吃光**
（净 IR 一度为 -0.17）。压低换手是过验收的生命线：`hold_thresh=10`（最小持有 10 日，
匹配 5 日标签）使净超额 IR 先升至约 **1.04**；2026-08 Plan C 超参后为 **IR 1.05**、
年化净超额约 +11.2%、超额回撤约 -17%，通过阶段1验收门槛（IR ≥ 0.8）。
`execution/make_trade_plan.py` 已同步该低换手逻辑，确保实盘（路线一手动执行）与回测口径一致。

## 目录

```
data_pipeline/   抓取(fetch_baostock) / 质检(quality_check) / 更新编排(update_daily)
research/        workflow_baseline.yaml / run_baseline / predict_daily / predict_range(批量信号)
contracts/       层间数据契约（信号、目标持仓等 CSV schema）
configs/         global.yaml 全局配置 + accounts/<account>.yaml 账户 profile（资金/策略/模式）
validation/      backtrader 复演引擎(replay_backtrader.py) + UMP 裁判(ump_judge.py)，阶段2 已实现
factor_lab/      （阶段3）LLM 因子提议(llm_propose) + qlib 评估(evaluate) + 五道准入关卡(run_iteration) + 因子库(factors.yaml)
overlays/        （TA 否决层）ta_veto：定性买入否决 JSON 契约 + A 股适配 + run_veto / gate_report
execution/       调仓清单(make_trade_plan) + 成交回填(record_fills) + 模拟成交(simulate_fills) + 对账(reconcile)
ops/             运维层：编排(run_daily) + 净值(compute_nav) + 日报(daily_report) + 监控(monitor) + 双线复盘(review_accounts) + TA复盘(review_ta_overlay) + 回填(backfill) + 公共库(common) + crontab.example
contracts/       层间数据契约 schema 校验/读写(schemas.py)
webapp/          （阶段5a）看板服务 server.py(FastAPI+APScheduler) + serve.sh + templates/ + static/
data/            运行时数据（signals/reports/nav/fills、accounts/<account>/ 双线隔离，overlays/ta_veto，不入 git）
```

## 验证层（阶段2，backtrader 独立复演）

```bash
python validation/replay_backtrader.py                 # 全期 + 滑点敏感性 0.1/0.2/0.3%
python validation/replay_backtrader.py --start 2024-01-02 --end 2024-06-30 --slippage 0.002
```

- 输入：`data/signals/latest_pred.csv`（qlib 测试期信号）+ qlib bin 行情
- 用第二套独立实现复核 qlib 向量化回测，强制建模 **T+1 / 涨跌停(9.5%) / 停牌 /
  整手 / 佣金万2.5(最低5) + 卖出印花税0.05% + 可配滑点**，次日开盘价成交（cheat-on-open）
- 调仓逻辑严格对齐研究层 `TopkDropoutStrategy` + `hold_thresh` 与 `make_trade_plan`
- 产出：`data/reports/validation_*.md`（滑点敏感性 + 与 qlib 年化差异 + 验收判定）
- 验收（Plan C / 2026-08-03 已通过）：复演与 qlib 年化超额差异 **−2.70pct**≤3pct；
  0.2% 滑点下年化超额 **7.86%**≥5%；日历对齐 624 交易日（勿再出现 feed 交集缩短事故）
- 复演结果比 qlib 略保守（整手现金拖累 + 显式滑点），这正是"杜绝纸面成交"的意义；
  真钱下限看 astock+0.2% 滑点行，勿按 Qlib 纸面数字承诺

### UMP 裁判模型（阶段2b，借鉴 abu 的信号二次否决）

```bash
python validation/ump_judge.py train                          # 训练裁判 + 样本外评估
python validation/replay_backtrader.py --start 2025-07-01 --compare-ump   # 有/无 UMP 的 A/B
```

- 用历史买入候选的入场特征（**市场状态**：基准动量/波动；**个股波动**：vol20/区间位置；
  **信号分位**：score/分位/z 值）训练 LightGBM 分类器，预测该笔交易持有期内是否跑输基准；
  每日对买入候选做最后否决，砍掉胜率最差的尾部交易（只移植 abu 思想，无运行时依赖）
- 防前视：入场特征只用信号日及之前数据；按时间切分训练/评估；阈值只在训练集标定
- 产物：`validation/ump_model.pkl`、`data/reports/ump_*.md`、`data/reports/ump_replay_ab_*.md`
- 样本外效果：被否决交易胜率 39.8% < 保留 42.3%；接入复演 A/B（2025-07~2026-06）
  超额 IR 由 0.21 提升至 0.48、年化超额 1.76%→4.11%（UMP 定位降尾部风险，提升风险收益比）

## 因子迭代闭环（阶段3，LLM 挖掘 + 五道准入关卡）

```bash
# 凭证放 configs/secret.env（已 gitignore）：LLM_API_KEY / LLM_MODEL / LLM_BASE_URL
python factor_lab/run_iteration.py --iters 3 --k 5
```

借鉴 RD-Agent 的 `fin_factor` 思想自研轻量闭环：**LLM 提假设 → Qlib 算指标 →
五道准入关卡 → 入库 → 反馈下一轮**。LLM 只产生因子假设（看不到未来数据），
评估与准入全部在本地基于 Qlib 完成，杜绝数据穿越。

- 五道准入关卡（§3.3 防过拟合生命线，`run_iteration.py:gates`）：
  1. **初筛**：|Rank IC|≥0.02、|ICIR|≥0.25、TopK 换手≤0.6
  2. **去重**：与现有因子库（种子族 + 已入库）最大 |相关| <0.7
  3. **样本外**：OOS Rank IC 与样本内同号且 |值|≥0.01（2024 至今 walk-forward）
  4. **人工评审**：经济逻辑非空（讲不出逻辑的高 IC 因子默认可疑、拒绝），余下交人工确认
  5. **纸面跟踪**：对"现有因子库等权组合"有样本外 Rank IC 增量者，才进入 `paper_tracking`，
     先跟踪 1~3 个月；`track_paper.py` 评近期 Rank IC，`--promote` 晋升 `live`；
     `alpha158_plus_lab` + `rolling_retrain.py` 重训过 IR 门禁后才进生产模型
- 因子库 `factor_lab/factors.yaml`：种子族（代表 Alpha158 各因子族，作去重基准）+
  已发现因子（含表达式、经济逻辑、各项指标、状态机 candidate→rejected/passed_auto→paper_tracking→live）
- 评估口径与研究层一致：中证500、5 日前向开盘收益标签；样本内 2019-2023 / 样本外 2024 至今
- 产出：`data/reports/factor_iter_*.md` / `paper_track_*.md` / `retrain_*.md`
- 首轮结果（3 轮 / 15 候选）：`pv_corr`、`delta_pv_corr` 两个量价相关因子过全部关卡，
  与库相关 0.25/0.57、换手 0.17、叠加库组合后 OOS Rank IC 增量 +0.0021 / +0.0031；
  反馈闭环可见效（第 2 轮在第 1 轮成功因子上做了"增量版"改良）

## 数据契约（contracts/schemas.py 统一校验/读写）

```
data/signals/YYYY-MM-DD.csv          instrument,score,rank                     （L2→L3，.done 触发下游）
data/orders/YYYY-MM-DD.csv           instrument,side,shares,ref_price          （人工执行清单）
data/fills/YYYY-MM-DD.csv            instrument,side,shares,price,amount,fee    （实际成交回填）
data/target_position/YYYY-MM-DD.csv  instrument,shares,last_price,entry_date    （执行后应达到的持仓）
data/nav/holdings.csv                instrument,shares,last_price,entry_date    （当前持仓）
data/nav/daily.csv                   date,nav,cash,position_value,n_pos,turnover,daily_ret,bench_ret,excess_ret
data/nav/account.json                start_capital,cash,halt_active,halt_since…（现金账本+熔断）
data/meta/industry_map.csv           instrument,industry,name                  （行业约束）
```

所有跨层 CSV 经 `contracts/schemas.py` 读写：校验列/类型/取值（代码格式、side、份额），
写入后落 `.done` 标记。人工录入的 fills 尤其依赖此校验拦住录错。

## 路线一每日闭环（阶段4，人工下单 + 收盘回填）

系统全自动产出调仓清单，人工在同花顺下单，收盘后录入成交，系统对账/算净值/出日报。
两个人工触点：**次日开盘照单下单**、**收盘后录入实际成交**；其余全自动。

当前采用双线记录：

- 研究模拟线：`research_sim_100k`，启动资金 100000 元，系统按**次日开盘价**自动模拟成交，
  记录每日模拟操作与收盘净值。
- 实盘线：`live_manual_10k`，真实资金 12000 元（`mode: manual`，2026-06-22 起），目标持仓 5-10 只；
  系统生成订单，用户回填实际 `fills`，系统收盘后自动对账/净值/复盘。
  截至 2026-07-31：净值约 12,034 / 现金约 6,032 / 持仓 **2 只**（7 月买入故障后的被动清仓残留）；
  08-03 已按 Plan C 新模型重出补仓单（买方正科技/宗申动力/兆驰股份，目标 5 只）。
- 两条线共享行情、信号、UMP、风控预检，但运行数据隔离在 `data/accounts/<account>/`。

### TA 定性否决影子线（TradingAgents 精简融入）

LGBM 仍是主选股；TA 只做买入候选的定性否决（类似加强版 UMP），**默认不上 live**。

- 对照线 `shadow_ctrl_sim`：与 live 同参（topk=7），仅 LGBM+UMP
- 影子线 `shadow_ta_sim`：同上 + `use_ta_veto: true`
- **零 Key 研究源（首期）**：公告=`eastmoney`（`cninfo` 备选）、新闻=`eastmoney`、基本面=`baostock`；舆情关闭
- **流程**：拉三源简报 → Analyst 摘要 → Bull/Bear 多轮 → Judge VETO/PASS（置信度+风险标签门禁）
- 产出：`data/overlays/ta_veto/YYYY-MM-DD.json`；缺文件/LLM 失败 → fail-open（不阻断出单）
- **2026-08-03 门禁状态：未通过，禁止改 live**
  - 影子线 07-31 清账重置后净值样本 **0/40**；须在 Plan C 新模型信号上重新累计 ≥40 交易日
  - 既有 veto 文件约 11 天、有效否决仅 2 次，质量样本不足；偶发单日否决率 33% 踩稳定性红线
  - 结论：live 维持 **LGBM(Plan C)+UMP**；TA 仅继续跑影子 A/B，双门禁 PASS 后再议

```bash
# 日终（可 --dry-run-ta 跳过 LLM）
python ops/run_daily.py --stage evening --account shadow_ctrl_sim --skip-data
python ops/run_daily.py --stage evening --account shadow_ta_sim --skip-data
python ops/run_daily.py --stage postclose --account shadow_ctrl_sim
python ops/run_daily.py --stage postclose --account shadow_ta_sim

# A/B + 门禁（≥40 交易日；未 PASS 禁止改 live_manual_10k）
python ops/review_ta_overlay.py
python overlays/ta_veto/gate_report.py
```

```bash
# 首次建账
python execution/record_fills.py --account live_manual_10k --init-capital 10000 --day <交易日>
# research_sim_100k 可由 postclose 自动建账，也可手动初始化：
python execution/record_fills.py --account research_sim_100k --init-capital 100000 --day <交易日>

# 晚间：分别生成两条线调仓单
python ops/run_daily.py --stage evening --account research_sim_100k --ump
python ops/run_daily.py --stage evening --account live_manual_10k --ump

# 次日开盘：研究线自动模拟；实盘线人工照 data/accounts/live_manual_10k/orders/ 下单
# 收盘后：用前一交易日订单生成当天 fills 模板，用户只填实际成交并 apply
python execution/record_fills.py --account live_manual_10k --day <成交日> --order-day <订单日> --template
python execution/record_fills.py --account live_manual_10k --day <成交日> --apply

# 收盘后：两条线分别对账/算净值/出日报
python ops/run_daily.py --stage postclose --account research_sim_100k --day <成交日> --order-day <订单日>
python ops/run_daily.py --stage postclose --account live_manual_10k --day <成交日> --order-day <订单日>

# 双线复盘：比较研究模拟线与实盘线的收益、成交、费用、现金闲置、跟踪偏差
python ops/review_accounts.py --research research_sim_100k --live live_manual_10k
```

- **make_trade_plan**：严格对齐回测的 TopkDropout + `hold_thresh=10` 低换手；
  `--ump` 接入阶段2b 裁判否决尾部买入；默认做涨跌停/停牌预检（涨停/停牌不买、跌停/停牌不卖）；
  日亏熔断禁买；`topk≥industry_min_positions` 时启用行业偏离约束
- **record_fills**：`--template` 按订单预填回填模板；`--apply` 应用实际成交，
  买入扣现金、卖出加现金（含费用），新建仓写 entry_date，收盘价盯市
- **reconcile**：订单 vs 成交（未成交/部分/计划外/滑点）、目标 vs 实际持仓（市值偏离超 2% 告警）
- **compute_nav**：现金 + 持仓盯市 → 净值/超额/换手；单日亏损触及 -3% **硬熔断**
  （`account.halt_active` + 剥除当日订单 BUY）
- **daily_report**：净值/盈亏/持仓/换手 + 与回测预期的年化跟踪偏差（验收门槛 ≤2pct，需 ≥20 日累计）
- **simulate_fills**：研究模拟线按次日开盘价自动生成成交，计入统一佣金/印花税
- **review_accounts**：双线复盘，比较研究模拟线与实盘线的收益、成交、费用、现金闲置、
  持仓数量、成交偏差和跟踪偏差
- **run_daily**：编排 evening/postclose；evening 在 `update_daily` 成功后若未显式 `--day` 会
  刷新为最新交易日（避免数据已更新却仍用旧日重复出单）；postclose 默认 `order_day=prev(成交日)`
- **backfill**：离线按交易日顺序重放 evening→postclose，用于初始化或断档补跑（见下）
- **monitor**：数据新鲜度、信号/成交/净值是否就绪、熔断中是否仍有 BUY 等健康检查，告警写 `data/logs/alerts.log`
- 验收（连续 20 个交易日无故障 + 年化跟踪偏差 ≤2pct + 对账零差错）需进入连续实跑累计，
  工具链已用两日 dry-run 跑通全链路（建仓→对账零差错→净值；次日 hold_thresh 控住换手为 0）

## 后台看板 + 常驻定时（阶段5a）

把双线跑成一个长期运行的后台项目：一个 FastAPI 服务（默认 `0.0.0.0:8000`）同时承担
**可视化看板** 与 **内置定时调度**（APScheduler，无需 cron）。

```bash
# 启停看板服务（默认 8000，可传端口）
bash webapp/serve.sh start 8000
bash webapp/serve.sh status
bash webapp/serve.sh logs       # tail -f data/logs/webapp.log
bash webapp/serve.sh stop

# 让看板启动即有数据：回填最近 ~1 周交易日（两条线，离线重放）
python ops/backfill.py --days 6
python ops/backfill.py --start 2026-06-04 --end 2026-06-11

# 断档补跑（服务停跑、节假日、或账户净值落后于日历）：
# 1) 补缺失信号（缺哪几天补哪几天）
python research/predict_range.py --start 2026-06-12 --end 2026-06-15
# 2) 从账户最后净值日到数据最新日，顺序重放两条线
python ops/backfill.py --start 2026-06-11 --end 2026-06-15
```

内置定时（Asia/Shanghai，工作日；逻辑即调用 `ops/run_daily.py`）：

- `22:30 evening`：两条线 `update_daily` → **刷新交易日** → 信号 → 调仓清单（UMP/风控）
- `23:30 postclose`：`order_day=上一交易日` 模拟成交 → 对账 → 净值 → 日报
- `周五 23:45`：双线复盘

**evening 日期纪律**：入口在未传 `--day` 时先取当前日历最新日；若随后 `update_daily` 把日历
推进（例如从 06-11 拉到 06-15），会**再次刷新**为更新后的最新日再出信号/清单。显式 `--day`
（如 `backfill`）则不刷新，用于历史重放。

**postclose 与 order_day**：成交日 `day=T` 的 postclose 读取 `orders/<order_day>.csv`，默认
`order_day=prev(T)`。例：周一 06-15 的 postclose 用周五 06-12 的订单模拟周一开盘成交；若 06-12
晚间 evening 未跑，会告警「缺少订单 06-12.csv」并 CRIT——用上面的断档补跑修复。

```bash
# 手动单步（与定时等价）
python ops/run_daily.py --stage evening --account research_sim_100k --ump
python ops/run_daily.py --stage postclose --account research_sim_100k
```

看板页签：总览（大盘指数条 + 双线净值/累计收益/超额对比）、各账户（净值/收益vs基准/持仓数·换手/现金vs市值、
持仓表、成交、报告）、双线对比（共同交易日收益差、成交滑点偏差）、告警/调度（任务下次运行时间 + alerts）。
API 见 `webapp/server.py`（`/api/overview`、`/api/account/{acct}/{daily|holdings|fills|reports}`、`/api/compare`、`/api/alerts`）。

**个股行情（选中股票实际数据）**：总览的大盘指数、持仓/成交/对账表里的标的均可点击，弹出该股
K线（红涨绿跌）+ 成交量 + 最近 OHLC 表，支持日/周/60分/15分切换。数据源（`webapp/quotes.py`）：

- 首选 **腾讯 TXApi**（`gtimg` fqkline/mkline，免费无 token，含近实时真实价，结果缓存）；
- 连不通时回退 **东方财富 `push2his`**；再不行回退 **本地 qlib 日线**
  （`$close/$factor` 还原真实价，仅到最近 release，每晚随流水线刷新）；
- 接口 `/api/quote/{instrument}?klt=&n=&fqt=`、`/api/indices`；返回里带 `source` 字段标明实际取数来源。

> 注：部分机房 IP 会被行情源限流；此时看板自动降级并在来源处标注。

**正式实盘状态**：`live_manual_10k` 已于 2026-06-22 切 `mode: manual`、初始资金 12000，
同花顺人工下单 + `record_fills` 回填。研究线仍 `simulated`，用于度量执行偏差。

```bash
# 每个交易日：照 orders/ 清单人工下单 → 收盘后
python execution/record_fills.py --account live_manual_10k --day <成交日> --order-day <订单日> --template
# 按实际成交改 price/shares（未成交 shares=0）→
python execution/record_fills.py --account live_manual_10k --day <成交日> --apply
```

> 说明：研究模拟线 `simulate_fills` 已贴近实盘摩擦（停牌/开盘涨跌停不成交），但仍是理想成交，
> 系统性略乐观；双线对比中的「成交偏差」即用于度量实盘相对它的真实滑点。
