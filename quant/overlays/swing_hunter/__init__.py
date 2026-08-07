"""短线猎手（swing hunter）：5~15 日 10%~20% 高赔率候选的预测与持续跟踪。

定位（与指数增强主线严格隔离）：
  - 纯看板建议层：不改 orders/、不开账户、不影响 LGBM 主线与 UMP/TA 门禁；
  - 候选四路汇流：量化强势 + 事件催化 + 跟踪延伸（多账户并集）+ 短线动量/突破；
    live 模式库对规则分加权；
  - LLM 只做「催化识别 + 多空辩论 + 分档预测」，风控过滤全部由规则完成；
  - 验证口径（收盘口径）：T+1 开盘价入场，10 个交易日内任意收盘价 ≥ +10% 记 hit，
    任意收盘价 ≤ −5% 且先于 hit 记 stopped，满 10 日未触发记 expired；MFE 仅辅助。
  - 四账户 evening 均可触发；同日 status=ok 则跳过新预测 LLM（跟踪仍更新）。

数据布局（data/overlays/swing_hunter/）：
  predictions/YYYY-MM-DD.json   当日预测文件（含 watch/reject 决策，便于复盘）
  tracker/{instrument}.json     每票全部预测与逐日跟踪（永久累积，长期记忆）
  catalog.json                  看板目录：最新预测 + 累计统计
  eval/cases/live_wins.yaml     实盘达标评测夹具
"""
