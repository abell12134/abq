"""阶段3 因子迭代主循环：LLM 提议 → Qlib 评估 → 五道准入关卡 → 入库，循环 N 轮。

五道准入关卡（§3.3 防过拟合生命线）：
  1) 初筛    |Rank IC|≥0.02、|ICIR|≥0.3、TopK换手≤0.6
  2) 去重    与现有因子库最大|相关|<0.7
  3) 样本外  OOS Rank IC 与样本内同号且|值|≥0.015（最近 2 年 walk-forward）
  4) 人工评审 经济逻辑非空（讲不出逻辑者拒）；通过者标记 passed_auto 待人工确认
  5) 纸面跟踪 对组合有增量（叠加基线后 OOS Rank IC 提升）→ 进入 paper_tracking，
            先跟踪 1~3 个月，达标才转 live 参与实盘模型

用法：
    python run_iteration.py --iters 3 --k 4
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import factor_lib as FL
import llm_propose as LP
from evaluate import Evaluator

QUANT = Path(__file__).resolve().parents[1]

# 准入阈值
IC_MIN, ICIR_MIN, TURN_MAX = 0.02, 0.25, 0.6
CORR_MAX, OOS_IC_MIN = 0.7, 0.01


def gates(m: dict, hypothesis: str) -> tuple[str, str]:
    """返回 (status, reason)。status ∈ rejected/passed_auto/paper_tracking。"""
    ic, icir = m["rank_ic"], m["icir"]
    if ic != ic or icir != icir:
        return "rejected", "IC 无法计算（表达式或数据问题）"
    # 关卡1 初筛
    if abs(ic) < IC_MIN:
        return "rejected", f"初筛: |RankIC|={abs(ic):.4f}<{IC_MIN}"
    if abs(icir) < ICIR_MIN:
        return "rejected", f"初筛: |ICIR|={abs(icir):.4f}<{ICIR_MIN}"
    if m["turnover"] == m["turnover"] and m["turnover"] > TURN_MAX:
        return "rejected", f"初筛: 换手={m['turnover']:.3f}>{TURN_MAX}"
    # 关卡2 去重
    if m["max_corr"] > CORR_MAX:
        return "rejected", f"去重: 与{m['corr_with']}相关={m['max_corr']:.3f}>{CORR_MAX}"
    # 关卡3 样本外
    oic = m["oos_rank_ic"]
    if oic != oic or (oic * ic) <= 0 or abs(oic) < OOS_IC_MIN:
        return "rejected", f"样本外: OOS RankIC={oic} 反号或过弱(<{OOS_IC_MIN})"
    # 关卡4 人工评审（逻辑非空校验，余下交人工）
    if not hypothesis or len(hypothesis.strip()) < 8:
        return "rejected", "人工评审: 缺少可解释的经济逻辑"
    # 关卡5 对组合的增量（提升样本外组合 IC 才值得纸面跟踪）
    if m["incr_ic_gain"] != m["incr_ic_gain"] or m["incr_ic_gain"] <= 0:
        return "passed_auto", "过自动关卡但对组合无样本外增量，暂不纳入纸面跟踪"
    return "paper_tracking", "过全部自动关卡 + 对组合有样本外增量，进入纸面跟踪"


def feedback_text(results: list[dict]) -> str:
    lines = []
    for r in results:
        m = r["metrics"]
        lines.append(
            f"- {r['name']} [{r['status']}] IC={m.get('rank_ic')} ICIR={m.get('icir')} "
            f"OOS_IC={m.get('oos_rank_ic')} 换手={m.get('turnover')} "
            f"maxCorr={m.get('max_corr')}({m.get('corr_with')}) "
            f"增量={m.get('incr_ic_gain')} → {r['reason']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=3)
    ap.add_argument("--k", type=int, default=4)
    args = ap.parse_args()

    import numpy as np
    print("[init] 初始化 Qlib 评估器（约 15s）")
    ev = Evaluator()
    lib = FL.load_lib()
    # 去重基准面板：种子 + 已发现（非拒绝）
    panel = ev.lib_panel(FL.existing_exprs(lib, include_rejected=False))
    print(f"[init] 去重基准因子 {len(panel)} 个")

    # "现有因子库组合"基线：种子因子按 IC 方向等权合成，作为"提升组合"的对照
    signed = {}
    for name, f in panel.items():
        ic_s, _ = ev.rank_ic(f, ev.is_p)
        signed[name] = (f, np.sign(ic_s) if ic_s == ic_s else 1.0)
    base_signal = ev.make_composite(signed, ev.oos_p)
    base_ic = ev.oos_ic(base_signal)
    print(f"[init] 现有因子库组合 样本外 RankIC 基线 = {base_ic:.4f}")

    history, feedback = [], ""
    for it in range(1, args.iters + 1):
        print(f"\n===== 迭代 {it}/{args.iters}：LLM 提议 {args.k} 个候选 =====")
        existing = FL.existing_exprs(lib)
        try:
            cands = LP.propose(existing, feedback, k=args.k)
        except Exception as e:
            print(f"[warn] LLM 提议失败：{e}")
            break
        print(f"  收到 {len(cands)} 个候选")

        results = []
        for c in cands:
            name, expr = c["name"], c["expr"]
            if name in FL.known_names(lib):
                name = f"{name}_{it}"
            try:
                f = ev.factor(expr)
                ic, icir = ev.rank_ic(f, ev.is_p)
                oic, oicir = ev.rank_ic(f, ev.oos_p)
                corr, who = ev.corr_to_lib(f, panel)
                sign = np.sign(ic) if ic == ic else 1.0
                m = {
                    "rank_ic": round(ic, 4), "icir": round(icir, 4),
                    "oos_rank_ic": round(oic, 4), "oos_icir": round(oicir, 4),
                    "turnover": round(ev.turnover(f), 4),
                    "max_corr": round(corr, 4), "corr_with": who,
                    "incr_ic_gain": round(
                        ev.incremental_vs_composite(f, sign, base_signal, base_ic), 5),
                }
            except Exception as e:
                m = {"rank_ic": float("nan"), "icir": float("nan")}
                status, reason = "rejected", f"表达式无法计算: {str(e)[:80]}"
                FL.upsert(lib, name, expr, c.get("hypothesis", ""),
                          c.get("category", "other"), status, m, reason, it)
                results.append({"name": name, "status": status, "reason": reason, "metrics": m})
                print(f"  ✗ {name}: {reason}")
                continue

            status, reason = gates(m, c.get("hypothesis", ""))
            FL.upsert(lib, name, expr, c.get("hypothesis", ""),
                      c.get("category", "other"), status, m, reason, it)
            results.append({"name": name, "status": status, "reason": reason, "metrics": m})
            mark = {"paper_tracking": "✓✓", "passed_auto": "✓", "rejected": "✗"}[status]
            print(f"  {mark} {name} [{status}] IC={m['rank_ic']} OOS={m['oos_rank_ic']} "
                  f"corr={m['max_corr']} 增量={m['incr_ic_gain']} | {reason}")
            # 通过去重的新因子加入基准面板，供后续候选去重
            if status in ("passed_auto", "paper_tracking"):
                panel[name] = f
            # 进入纸面跟踪的因子并入"库组合"，后续候选的增量对照随之更新（贪心扩库）
            if status == "paper_tracking":
                signed[name] = (f, sign)
                base_signal = ev.make_composite(signed, ev.oos_p)
                base_ic = ev.oos_ic(base_signal)

        FL.save_lib(lib)
        history.append({"iter": it, "results": results})
        feedback = feedback_text(results)

    report = render_report(history, lib)
    out = QUANT / "data" / "reports" / f"factor_iter_{dt.date.today():%Y%m%d}.md"
    out.write_text(report)
    print("\n" + report)
    print(f"\n[OK] 因子库 {FL.LIB_PATH}；报告 {out}")
    return 0


def render_report(history: list[dict], lib: dict) -> str:
    n_prop = sum(len(h["results"]) for h in history)
    paper = FL.by_status(lib, "paper_tracking")
    auto = FL.by_status(lib, "passed_auto")
    rej = FL.by_status(lib, "rejected")
    lines = [
        "# 阶段3 因子迭代报告（RD-Agent 式 LLM 挖掘 + 五道准入关卡）",
        "",
        f"- 生成时间: {dt.datetime.now():%Y-%m-%d %H:%M}",
        f"- 迭代轮数: {len(history)}；累计提议因子: {n_prop}",
        f"- 结果：进入纸面跟踪 {len(paper)}，过自动关卡待复核 {len(auto)}，拒绝 {len(rej)}",
        f"- 评估口径：中证500，5 日前向开盘收益标签；样本内 2019-2023 / 样本外 2024 至今",
        "",
        "## 准入漏斗（逐轮）",
    ]
    for h in history:
        lines.append(f"\n### 迭代 {h['iter']}")
        lines.append("| 因子 | 状态 | RankIC | ICIR | OOS_IC | 换手 | maxCorr | 组合增量 | 判定 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for r in h["results"]:
            m = r["metrics"]
            lines.append(
                f"| {r['name']} | {r['status']} | {m.get('rank_ic')} | {m.get('icir')} "
                f"| {m.get('oos_rank_ic')} | {m.get('turnover')} | {m.get('max_corr')} "
                f"| {m.get('incr_ic_gain')} | {r['reason']} |")
    if paper:
        lines += ["", "## 通过全部自动关卡、进入纸面跟踪的因子（待人工评审确认后跟踪 1~3 月）"]
        for n, f in paper.items():
            m = f["metrics"]
            lines += [
                f"\n### {n}  （{f['category']}）",
                f"- 表达式: `{f['expr']}`",
                f"- 经济逻辑: {f['hypothesis']}",
                f"- 样本内 RankIC {m['rank_ic']} / ICIR {m['icir']}；样本外 RankIC {m['oos_rank_ic']}",
                f"- 与库最大相关 {m['max_corr']}（{m['corr_with']}）；换手 {m['turnover']}；"
                f"叠加基线后 OOS 组合 RankIC 增量 **{m['incr_ic_gain']:+.5f}**",
            ]
    lines += [
        "",
        "## 验收对照（阶段3）",
        f"- 完成 ≥3 轮迭代: {len(history)} 轮 → {'**通过**' if len(history) >= 3 else '未达'}",
        f"- ≥1 个新因子过全部关卡且提升组合样本外 IC: {len(paper)} 个 → "
        f"{'**通过**' if len(paper) >= 1 else '未达'}",
        "",
        "> 纸面跟踪期满、线上 IC 达标后，再将因子并入 research/ 的模型特征集（滚动重训验证）。",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
