# 大盘行情监控看板（market_board）设计方案

> **定位**：L5 展示层 / 与 `swing_hunter` 同级的只读建议层 overlay。
> **纪律**：只读——**不改 `orders/`、不改信号、不碰账户**；任何数据源失败 fail-open
> 降级展示并标注，不阻断看板其他模块；不构成投资建议。

统计口径统一限定为 **Plan C 生产信号池**（约 499 只，下称"池内"），
即用户口径的"我的股票筛选池 300"（实际以生产信号文件为准）。

---

## 1. 决策记录（2026-09-01 与所有者确认）

| 决策点 | 结论 |
|---|---|
| 池子真相源 | `workflow_baseline_planC.yaml` 对应的生产信号池（`market: csi500`，`data/signals/YYYY-MM-DD.csv` 实际约 499 只） |
| 前端落点 | 现有运维看板 `webapp`（:8000）新增一级 tab「大盘看板」，内部 6 个子页签；Chart.js 4.4 + 原生 JS，风格与现有看板一致 |
| 盘中实时性 | **手动刷新**（「刷新行情」按钮拉一次批量报价），不做自动轮询 |
| 附带修复 | 现有「市场热度」生成端 `research/sector_pulse.py` 仓库中缺失（仅消费端存在，点击必然报错）；本模块 P3 的 `themes.py` 产出兼容 `/api/market/pulse` 字段格式，顺手补全，不另起两套热度数据 |

---

## 2. 池子定义（pool.py，单点真相源）

三级加载链，全部 9 个功能统一经此加载，换池只改配置：

```
1. data/meta/board_pool.csv          # 可选人工覆盖，存在则优先
2. data/signals/<最新日>.csv          # 生产信号池（默认路径，~499 只）
3. datasets/qlib_data/cn_data/instruments/csi500.txt   # 历史时点成分兜底
```

`configs/global.yaml` 新增：

```yaml
market_board:
  pool_source: signals            # signals | custom_csv | csi500_file
  pool_file: data/meta/board_pool.csv
  exclude_st: true
  intraday_batch_size: 60         # 腾讯批量报价单批上限
```

---

## 3. 页面信息架构（一级 tab + 6 子页签）

```
一级导航: 总览 | 账户×4 | 对比 | 操作清单 | 大盘看板★ | 舆情 | 短线 | 研究 | 告警
子页签:   [全景] [涨停复盘] [连板梯队] [题材轮动] [强势资金] [快讯]
全局状态条(所有子页共享): 盘后快照日期▾ | [刷新行情] | 数据源/时间戳 | 池规模
```

设计要点：每页**视觉主角唯一**；盘后快照 JSON 一次加载、切子页不重取；
仅点「刷新行情」走实时批量报价。

### 3.1 全景（需求 1、2、4-指数环境）

- 温度计条：涨停/跌停/炸板率、涨跌家数分档（>5% / 0~5%）、温度值与冷热定性
- 情绪周期曲线：15 日温度 + 涨停家数双轴，当前态标注（冰点/回暖/高潮/退潮）
- 30 日行情周期：池内等权净值 vs 指数，周期段定位（上升/见顶/下跌/筑底）
- 指数环境定位：中证500 相对 20 日线、量能、趋势，综合环境分
- 涨榜 TOP20 / 跌榜 TOP20（池内实时涨幅排序）

### 3.2 涨停复盘（需求 3、5-涨停类型）

- **封单强度散点图**（整页主角，~420px）：x=封单额/流通市值，y=连板数，
  气泡=成交额，色=涨停类型（一字/T字/换手）
- 点击气泡 → **侧滑抽屉评分卡**（不离开图）：五维评分 + S/A/B/C 分级 +
  板数/类型/封单/题材；带「看K线」「看舆情」「加入研究宇宙」联动
- 底部：涨停类型分布横条 + 涨停预警列表（池内涨幅≥8% 未封板，标红）

### 3.3 连板梯队（需求 4）

- 梯队金字塔：最高板→首板分层，逐只标注抢筹强度（🔥 档）与解禁雷区 ⚠
- 梯队存活率：昨 N 板今晋级率 vs 近 10 日均值
- 右列：抢筹强度榜 TOP10（集合竞价额/昨成交额）；解禁雷区表（30 日内，占流通>10% 标红）

### 3.4 题材轮动（需求 5-热度、6）

- 题材热度榜：按涨停家数排序，附总额与近 5 日迷你走势
- 行业分布：申万一级涨停家数条形图（复用 `industry_map.csv`）
- 板块轮动矩阵：行业 × 近 5/10/20 日涨跌幅热力表（红涨绿跌），识别资金切换
- 个股 K 线查看器：复用现有 `quotes.py` / `loadStock` 组件

### 3.5 强势资金（需求 7、8）

- 强势个股榜：综合评分排序，S 级置顶，行内「评分卡」入口
- 主力净流入 TOP20（占成交额比、连续日数）/ 净流出 TOP10（避雷）

### 3.6 快讯（需求 9）

- 复用 `sentiment_memory` 采集管道（财联社 7x24 / 东财 / 新浪 / 公告）
- 筛选器：全部 / 仅池内相关 / 政策宏观 / 公告
- 命中池内股票的条目自动标红、可点击跳评分卡

---

## 4. 后端模块（`quant/overlays/market_board/`）

```
pool.py          # 池子三级加载（§2）
batch_quotes.py  # 腾讯批量报价 qt.gtimg.cn（60只/批）+ 进程内 60s 缓存
limit_stats.py   # 涨停/跌停/连板/炸板统计（qlib 历史 + 盘中实时合并）
cycle.py         # 情绪周期规则引擎 + 30 日行情周期定位
ladder.py        # 连板梯队 + 抢筹强度 + 梯队存活率
unlock.py        # 解禁雷区（akshare 东财解禁，本地缓存周刷）
themes.py        # 题材热度 + 涨停类型 + 行业分布；兼出 sector_pulse 兼容字段
rotation.py      # 板块轮动矩阵（池内按行业聚合历史涨跌）
strong.py        # 强势个股五维评分 + S/A/B/C 分级
fundflow.py      # 资金流向榜（东财 push2 / akshare stock_individual_fund_flow）
news.py          # 快讯流（读 sentiment_memory raw + 池内过滤）
run_board.py     # 盘后全量快照编排（evening 接入，fail-open）
store.py         # JSON 契约读写 + .done 标记
```

## 5. 数据契约

```
data/overlays/market_board/
├── pool_snapshot.json        # 池子构成 + 生成日
├── daily/YYYY-MM-DD.json     # 盘后全量快照：温度计/周期/梯队/题材/强势/资金
├── daily/YYYY-MM-DD.done
├── intraday/latest.json      # 盘中实时快照（手动刷新时写入）
└── unlock_cache.json         # 解禁缓存（周刷）
```

## 6. API（`webapp/server.py` 新增，遵循现有 job 模式）

| 路由 | 功能 | 需求 |
|---|---|---|
| `GET /api/board/overview` | 温度计 + 涨跌榜 + 周期定位 + 指数环境 | 1/2/4 |
| `GET /api/board/cycle` | 15 日情绪曲线 + 30 日周期明细 | 2 |
| `GET /api/board/limit-scatter` | 封单散点图数据 + 涨停预警列表 | 3 |
| `GET /api/board/stock/{instrument}` | 单票五维评分卡 + 分级 | 3 |
| `GET /api/board/ladder` | 梯队 + 抢筹 + 解禁 + 存活率 | 4 |
| `GET /api/board/themes` | 题材热度 + 涨停类型 + 行业分布 | 5 |
| `GET /api/board/rotation` | 板块轮动矩阵 | 6 |
| `GET /api/board/strong` | 强势个股榜 | 7 |
| `GET /api/board/fundflow` | 资金流向榜 | 8 |
| `GET /api/board/news` | 快讯流（池内命中高亮） | 9 |
| `POST /api/board/run` | 手动重跑盘后快照（后台 job + 进度轮询） | — |
| `POST /api/board/refresh` | 盘中实时批量报价刷新（写 intraday/latest.json） | 1/3 |

## 7. 核心算法口径

- **涨停判定**：复用 `ops/common.py:_limit_pct`（主板 10%、科创/创业 20%），
  `ret ≥ limit - 0.002` 计涨停；炸板 = 盘中最高触板但收盘未封（ qlib 日线可判）。
- **涨停类型**：一字板 = 开=收=高=低且涨停；T 字板 = 开盘涨停、盘中开过、收盘涨停；
  其余收盘涨停 = 换手板。
- **连板数**：向前逐日递推收盘涨停天数。
- **封单强度**：封单额 = 买一价 × 买一量（腾讯盘口）；强度 = 封单额 / 流通市值。
- **抢筹强度**：集合竞价成交额 / 昨日成交额（>8% 记强抢筹）。
- **情绪周期四态**（规则引擎，不用 LLM，可解释）：
  - 冰点：涨停 <25 家，或最高连板 ≤2 且昨涨停今日均收益 <0
  - 回暖：涨停数连续 2 日回升 + 出现 3 板
  - 高潮：涨停 >60 家 + 最高连板 ≥5 + 炸板率 <30%
  - 退潮：炸板率 >40% 或 ≥4 板高位股出现跌停
- **强势评分**（百分制加权）：20 日动量 25% + 量比/换手 20% + 相对指数强度 20%
  \+ 题材热度 20% + 主力净流入 15%；**S≥80 / A≥65 / B≥50 / C<50**。
- **30 日周期定位**：池内等权净值 30 日窗口的斜率 + 相对 20 日均线位置 →
  上升段 / 见顶回落 / 下跌段 / 筑底。

## 8. 调度

| 时段 | 动作 |
|---|---|
| 盘中 | 看板「刷新行情」→ 腾讯批量报价 → `intraday/latest.json`（温度计/榜单/散点/预警实时化） |
| 盘后 | `run_daily` evening 末端追加 `run_board.py`（fail-open，失败不阻断主线）→ `daily/<day>.json` |
| 每周末 | 解禁缓存、概念板块成分缓存刷新 |

## 9. 数据源矩阵

| 需求 | 源 | 频次 |
|---|---|---|
| 池内批量行情/涨跌停 | 腾讯 `qt.gtimg.cn` 批量（60 只/批） | 手动刷新 |
| 涨停/连板/炸板历史 | 本地 qlib 日线 | 盘后 |
| 封单额 | 腾讯盘口（买一量价）/ 东财 push2 兜底 | 手动刷新 |
| 解禁 | akshare 东财解禁接口 | 周缓存 |
| 资金流向 | 东财 push2 / akshare 个股资金流 | 盘后+刷新 |
| 题材/概念 | 东财概念板块（akshare `stock_board_concept_*`） | 日缓存 |
| 行业 | `data/meta/industry_map.csv`（申万一级） | 已有 |
| 快讯 | 复用 `sentiment_memory/sources.py` | 已有管道 |
| K 线 | `webapp/quotes.py`（腾讯→东财→qlib） | 已有 |

## 10. 实施分期

| 期 | 交付 | 数据依赖 | 状态 |
|---|---|---|---|
| **P1** | pool/limit_stats/cycle/ladder/strong + 全景/梯队/强势资金子页（需求 1/2/4部分/5行业/7） | 纯本地 qlib，最稳 | ✅ 2026-09-01 完成并上线验证 |
| **P2** | batch_quotes + 涨停复盘子页：实时刷新/预警/封单散点/评分卡（需求 3） | 腾讯批量/盘口 | ✅ 2026-09-01 完成并上线验证 |
| **P3** | unlock/themes/rotation/fundflow/news + 题材轮动/快讯子页（需求 4/5/6/8/9 剩余）；**同步补全 sector_pulse 兼容产出** | akshare/东财/舆情管道 | ✅ 2026-09-01 完成并上线验证 |

每期独立可用、fail-open；落地时同步登记 `quant/README.md` 模块手册与
`docs/FLOWCHARTS.md` 流程图。

### P2 落地备注（2026-09-01）

- `batch_quotes.py`：腾讯 `qt.gtimg.cn` 批量报价（60 只/批，499 只约 8s），
  GBK 解码；字段 9/10 买一价量（封单）、44 流通市值、47/48 涨跌停价、49 量比；
  封单额=买一价×买一量×100（涨停且卖一空时）；进程内 60s 缓存。
- `intraday.py`：盘中快照合并盘后连板（昨 N 板 + 今涨停 = N+1）；
  涨停预警线 = 涨停幅度 −2pct（主板 8% / 创业科创 18%→15% 口径可调）。
- `stock_card.py`：五维评分卡 = 盘后三维分 ×60% + 五维均分 ×40%；
  资金维度 P2 为量比×换手近似，P3 升级主力净流入。
- API 新增：`POST /api/board/refresh`（后台 job）、`GET /api/board/intraday`、
  `GET /api/board/limit-scatter`（盘中/盘后双口径）、`GET /api/board/stock/{inst}`。
- 前端：散点图 bubble 图（x=封单强度 y=连板数 泡=成交额，点击出抽屉评分卡）、
  「刷新行情」按钮 + 盘中徽章、预警列表、盘后/盘中双口径明细表。

### P3 落地备注（2026-09-01）

- **东财 push2 主节点本机偶发 502**：`themes.py`/`fundflow.py` 手写 clist 调用，
  HOSTS 首选 `push2delay.eastmoney.com`（延时节点，盘后统计场景足够），
  akshare 对应封装（资金流排名/概念榜）在本机被断连，故未用。
- **解禁**：`stock_restricted_release_queue_em`（个股）只有历史记录、无未来预告；
  改用 `stock_restricted_release_detail_em(start,end)` 全市场未来解禁明细一次拉取，
  池内过滤。口径：占解禁前流通市值 ≥1% 标注、≥10% 高危；当天缓存。
- **快讯双流**：raw 实测 90% 为个股新闻（自带 instrument 字段）——
  「池内动态」直接关联（无文本匹配）；全局电报（sina/eastmoney_global/policy_*）
  走代码带边界 + 标题名称（≥3 字）命中标注。无 LLM、无外网。
- **板块轮动**：纯本地，池内按申万一级聚合近 5/10/20 日涨幅热力矩阵，
  「流入↑/流出↓」切换信号 = 5 日与 20 日方向背离。
- **评分卡资金维度升级**：盘后 fundflow 榜内的主力净流入/净占比替代 P2 近似口径。
- **sector_pulse 兼容**：`themes.sector_pulse_compat` 产出
  `data/reports/sector_pulse_YYYYMMDD.json`（industries_top/concepts_top/indices 含
  chg_5d/chg_20d），旧「市场热度」看板功能恢复可用。
- API 新增：`GET /api/board/{themes,rotation,fundflow,news,unlock}`。
- 全量快照耗时约 60~100s（含解禁/题材/资金流外网调用），evening 定时无压力。
