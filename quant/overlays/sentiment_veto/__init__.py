"""实盘舆情硬伤筛（Cursor Agent 联网检索）。

仅挂 live_manual_10k：对调仓清单中的 BUY 做公开舆情硬伤否决，
产出 veto JSON + 可执行清单 Markdown + orders_exec（人工下单用）。
失败 fail-open，不阻断 evening。
"""
