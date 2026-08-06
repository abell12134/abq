"""每日预测 Markdown 报告（看板展示 + 落盘复盘）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .schema import PredictionFile, ROOT, read_predictions

TZ = ZoneInfo("Asia/Shanghai")


def _pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.1f}%"


def write_daily_report(day: str, pf: PredictionFile | None = None) -> Path:
    pf = pf or read_predictions(day)
    path = ROOT / "predictions" / f"{day}.md"
    if not pf:
        path.write_text(f"# 短线猎手 {day}\n\n无预测数据。\n", encoding="utf-8")
        return path

    preds = sorted(
        pf.predictions,
        key=lambda p: (-p.swing_score, p.instrument),
    )
    predicts = [p for p in preds if p.action == "predict"]
    watches = [p for p in preds if p.action == "watch"]
    rejects = [p for p in preds if p.action == "reject"]

    lines = [
        f"# 短线猎手日报 · {day}",
        "",
        f"> 生成 {datetime.now(TZ):%Y-%m-%d %H:%M:%S} · 状态 `{pf.status}` · "
        f"候选池 {len(pf.candidates)} · LLM 深析 {pf.meta.get('n_llm_ok', '—')}",
        "",
        "## 汇总",
        "",
        f"| 预测 predict | 观察 watch | 否决 reject |",
        f"|---|---|---|",
        f"| {len(predicts)} | {len(watches)} | {len(rejects)} |",
        "",
    ]

    if pf.meta.get("sentiment_prep"):
        sp = pf.meta["sentiment_prep"]
        lines.append(f"舆情预采集：补齐 {sp.get('collected', 0)} 只，跳过 {sp.get('skipped', 0)} 只")
        lines.append("")

    if pf.meta.get("gate"):
        g = pf.meta["gate"]
        lines.append(
            f"预测门槛：{g.get('applied_tier')}（{g.get('label_applied')}）"
            + (" · **已降档**" if g.get("fallback_used") else "")
            + f" · predict {g.get('n_predict_initial')}→{g.get('n_predict_final')}"
        )
        lines.append("")

    def _gate_tag(p: Prediction) -> str:
        gt = (p.meta or {}).get("gate_tier") or "—"
        if (p.meta or {}).get("gate_fallback"):
            return f"{gt} ↘"
        return str(gt)

    def _section(title: str, rows: list) -> None:
        if not rows:
            lines.append(f"## {title}\n\n（无）\n")
            return
        lines.append(f"## {title}\n")
        for p in rows[:20]:
            tiers = " · ".join(
                f"+{t.get('pct', 0)*100:.0f}%@{t.get('prob', 0)*100:.0f}%"
                for t in (p.target_tiers or [])[:3]
            )
            lines.append(
                f"### {p.instrument} {p.name} · {p.action} · swing {p.swing_score:.2f}"
                f" · 门槛 {_gate_tag(p)}"
            )
            lines.append(f"- **置信度** {p.confidence:.2f} · **目标档** {tiers or '—'}")
            if p.catalysts:
                lines.append(f"- **催化** {'、'.join(p.catalysts)}")
            if p.risk_tags:
                lines.append(f"- **风险** {'、'.join(p.risk_tags)}")
            for r in (p.reasons or [])[:3]:
                lines.append(f"- {r}")
            ev = p.news_brief or []
            if ev:
                lines.append("- **近期材料**")
                for e in ev[:5]:
                    lines.append(f"  - [{e.get('source')}] {e.get('published')} {e.get('title')}")
            lines.append("")

    _section("预测 predict（入跟踪）", predicts)
    _section("观察 watch（Top 关注）", watches[:10])
    _section("否决 reject（硬伤/过滤）", rejects[:8])

    if pf.meta.get("delta_summary"):
        ds = pf.meta["delta_summary"]
        lines += ["## 活跃跟踪 delta", "",
                  f"更新 {ds.get('updated', 0)} 只，跳过 {ds.get('skipped', 0)} 只", ""]
        for d in (ds.get("details") or [])[:10]:
            lines.append(f"- {d}")

    lines += ["---", "*研究用途，不构成投资建议。*"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
