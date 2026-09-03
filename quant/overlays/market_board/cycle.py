"""情绪周期定位（规则引擎，不用 LLM，可解释）+ 30 日行情周期 + 温度计。

输入：limit_stats.compute 的 daily 序列（按日聚合）。
输出：
  temperature   0~100 温度计（50 为中性）
  emotion       冰点/回暖/高潮/退潮 + 触发理由
  period30      30 日行情周期段（上升段/见顶回落/下跌段/筑底）+ 依据
  series        供前端画 15 日情绪曲线 / 30 日净值曲线

规则全部写死在阈值常数里，改动即改文档口径（docs/MARKET_BOARD.md §7）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .limit_stats import DayStat

# ---- 阈值（docs/MARKET_BOARD.md §7 同步维护） ----
COLD_LIMIT_UP = 25          # 冰点：涨停家数 <
HOT_LIMIT_UP = 60           # 高潮：涨停家数 >
HOT_MAX_STREAK = 5          # 高潮：最高连板 ≥
HOT_BROKEN_RATE = 0.30      # 高潮：炸板率 <
EBB_BROKEN_RATE = 0.40      # 退潮：炸板率 >
WARM_STREAK = 3             # 回暖：出现 N 连板
EQ_MA20_WINDOW = 20


@dataclass
class CycleResult:
    temperature: float = 50.0
    temperature_label: str = "中性"
    emotion: str = "未知"
    emotion_reasons: list[str] = field(default_factory=list)
    period30: str = "未知"
    period30_reasons: list[str] = field(default_factory=list)
    series15: list[dict[str, Any]] = field(default_factory=list)
    series30: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "temperature": round(self.temperature, 1),
            "temperature_label": self.temperature_label,
            "emotion": self.emotion,
            "emotion_reasons": self.emotion_reasons,
            "period30": self.period30,
            "period30_reasons": self.period30_reasons,
            "series15": self.series15,
            "series30": self.series30,
        }


def _temperature(d: DayStat) -> float:
    """温度计：50 为中性，涨停/连板加热，跌停/炸板降温。"""
    if d.traded == 0:
        return 50.0
    score = 50.0
    score += min(d.limit_up / d.traded * 100 * 8, 25)          # 涨停占比，封顶 +25
    score -= min(d.limit_down / d.traded * 100 * 12, 30)       # 跌停占比，封顶 -30
    score += min(d.max_streak * 2.5, 12)                       # 连板高度，封顶 +12
    score -= d.broken_rate * 25                                # 炸板率惩罚，封顶 -25
    score += (d.up - d.down) / d.traded * 20                   # 涨跌家数差，±20
    return max(0.0, min(100.0, score))


def _temperature_label(t: float) -> str:
    if t >= 75:
        return "亢奋"
    if t >= 60:
        return "偏热"
    if t >= 40:
        return "中性"
    if t >= 25:
        return "偏冷"
    return "冰点"


def _emotion(daily: list[DayStat]) -> tuple[str, list[str]]:
    """四态判定：优先退潮（风险优先），再高潮/冰点，最后回暖。默认沿用前一日状态。"""
    if not daily:
        return "未知", []
    d = daily[-1]
    reasons: list[str] = []

    # 退潮（风险优先）：炸板率高企，或高位股崩塌（最高板骤降 + 跌停放大）
    if d.broken_rate > EBB_BROKEN_RATE and (d.broken + d.limit_up) >= 10:
        reasons.append(f"炸板率 {d.broken_rate:.0%} > {EBB_BROKEN_RATE:.0%}")
        return "退潮", reasons
    if len(daily) >= 2:
        prev = daily[-2]
        if prev.max_streak >= 4 and d.max_streak <= prev.max_streak - 2 and d.limit_down >= 5:
            reasons.append(f"最高板 {prev.max_streak}→{d.max_streak} 且跌停 {d.limit_down} 家")
            return "退潮", reasons

    # 高潮
    if d.limit_up > HOT_LIMIT_UP and d.max_streak >= HOT_MAX_STREAK and d.broken_rate < HOT_BROKEN_RATE:
        reasons.append(f"涨停 {d.limit_up} 家 > {HOT_LIMIT_UP}，最高 {d.max_streak} 板，"
                       f"炸板率 {d.broken_rate:.0%} < {HOT_BROKEN_RATE:.0%}")
        return "高潮", reasons

    # 冰点
    if d.limit_up < COLD_LIMIT_UP:
        reasons.append(f"涨停仅 {d.limit_up} 家 < {COLD_LIMIT_UP}")
        if d.max_streak <= 2:
            reasons.append(f"最高连板仅 {d.max_streak}")
        return "冰点", reasons

    # 回暖：涨停数连续 2 日回升 + 出现 3 板
    if len(daily) >= 3:
        a, b, c = daily[-3].limit_up, daily[-2].limit_up, daily[-1].limit_up
        if a < b < c and d.max_streak >= WARM_STREAK:
            reasons.append(f"涨停家数 {a}→{b}→{c} 连续回升，出现 {d.max_streak} 连板")
            return "回暖", reasons

    # 默认：沿用前一日（若前一日也是默认态则给中性描述）
    if len(daily) >= 2:
        prev_emotion = daily[-2].__dict__.get("_emotion")
        if prev_emotion:
            return prev_emotion, [f"沿用前一日定位（无新触发条件）"]
    return "震荡", ["未触发四态阈值，按震荡处理"]


def _period30(daily: list[DayStat]) -> tuple[str, list[str], list[dict[str, Any]]]:
    """30 日行情周期：池内等权累计净值 → 斜率 + 相对均线位置。"""
    window = daily[-30:] if len(daily) >= 30 else daily[:]
    if len(window) < 10:
        return "未知", ["样本不足 10 个交易日"], []

    nav = [1.0]
    for d in window:
        nav.append(nav[-1] * (1 + d.eq_ret))
    nav = nav[1:]
    series = [{"day": d.day, "eq_nav": round(n, 4), "eq_ret": round(d.eq_ret, 5)}
              for d, n in zip(window, nav)]

    total_ret = nav[-1] / nav[0] - 1
    # 近 5 日斜率 vs 前段
    recent_ret = nav[-1] / nav[-6] - 1 if len(nav) >= 6 else 0.0
    # 均线位置：当前净值 vs 窗口内 20 日均线
    ma_win = nav[-EQ_MA20_WINDOW:] if len(nav) >= EQ_MA20_WINDOW else nav
    ma = sum(ma_win) / len(ma_win)
    above_ma = nav[-1] >= ma

    reasons = [f"30日池内等权 {total_ret:+.2%}，近5日 {recent_ret:+.2%}，"
               f"净值{'在' if above_ma else '跌破'}20日均线"]

    if total_ret > 0.02 and recent_ret > 0 and above_ma:
        return "上升段", reasons, series
    if total_ret > 0.02 and (recent_ret < -0.01 or not above_ma):
        return "见顶回落", reasons, series
    if total_ret < -0.02 and recent_ret < 0 and not above_ma:
        return "下跌段", reasons, series
    if total_ret < -0.02 and recent_ret > 0:
        return "筑底", reasons, series
    return "震荡", reasons, series


def compute(daily: list[DayStat]) -> CycleResult:
    res = CycleResult()
    if not daily:
        return res

    # 先按日打标（供"沿用前一日"逻辑）
    for i in range(len(daily)):
        emo, _ = _emotion(daily[:i + 1])
        daily[i].__dict__["_emotion"] = emo

    d = daily[-1]
    res.temperature = _temperature(d)
    res.temperature_label = _temperature_label(res.temperature)
    res.emotion, res.emotion_reasons = _emotion(daily)
    res.period30, res.period30_reasons, res.series30 = _period30(daily)

    # 15 日情绪曲线：温度 + 涨停家数 + 炸板率
    temps: list[float] = []
    for dd in daily[-15:]:
        # 用当日及之前窗口逐日重算温度（温度本身是当日量，无窗口依赖）
        temps.append(_temperature(dd))
    res.series15 = [
        {
            "day": dd.day,
            "temperature": round(t, 1),
            "limit_up": dd.limit_up,
            "limit_down": dd.limit_down,
            "broken_rate": dd.broken_rate if hasattr(dd, "broken_rate") else
            round(dd.broken / (dd.broken + dd.limit_up), 4) if (dd.broken + dd.limit_up) else 0.0,
            "max_streak": dd.max_streak,
        }
        for dd, t in zip(daily[-15:], temps)
    ]
    return res
