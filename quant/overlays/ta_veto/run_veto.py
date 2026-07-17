"""日终定性买入否决：对 buy_candi 跑精简 Agent 图，写出 veto JSON。

流程对齐 make_trade_plan：
  signal → select_trades → 板块排除 →（可选）UMP → TA 裁判 → 写文件

LLM 失败 / 超时 / 无密钥：写 status=fail_open，exit 0（不阻断出单）。

用法：
    python overlays/ta_veto/run_veto.py --date 2026-07-16 --account shadow_ta_sim
    python overlays/ta_veto/run_veto.py --date 2026-07-16 --account shadow_ta_sim --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))
sys.path.insert(0, str(QUANT / "execution"))
sys.path.insert(0, str(QUANT / "ops"))

from overlays.ta_veto import adapt_ashare as ashare  # noqa: E402
from overlays.ta_veto import debate as debate_mod  # noqa: E402
from overlays.ta_veto import prompts_cn as prompts  # noqa: E402
from overlays.ta_veto import vendors as V  # noqa: E402
from overlays.ta_veto.schema import (  # noqa: E402
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_MAX_VETOES,
    VetoDecision,
    VetoFile,
    apply_veto_policy,
    write_veto_file,
)

SECRET = QUANT / "configs" / "secret.env"


def _load_secret() -> dict[str, str]:
    env: dict[str, str] = {}
    if SECRET.exists():
        for line in SECRET.read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _llm_client():
    import os

    from openai import OpenAI

    s = _load_secret()
    key = s.get("LLM_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key:
        raise RuntimeError("缺少 LLM_API_KEY（configs/secret.env）")
    base = s.get("LLM_BASE_URL") or os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com"
    model = s.get("LLM_MODEL") or os.environ.get("LLM_MODEL") or "deepseek-chat"
    return OpenAI(api_key=key, base_url=base), model


def _parse_decision(text: str, instrument: str) -> VetoDecision:
    text = (text or "").strip()
    obj: dict[str, Any] | None = None
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            obj = None
    if not isinstance(obj, dict):
        return VetoDecision(
            instrument=instrument,
            action="pass",
            confidence=0.0,
            risk_tags=[],
            reasons=["LLM 输出无法解析，默认 pass"],
        )
    action = str(obj.get("action", "pass")).lower()
    if action not in {"veto", "pass"}:
        action = "pass"
    try:
        conf = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    tags = [str(t) for t in (obj.get("risk_tags") or [])]
    reasons = [str(r) for r in (obj.get("reasons") or [])][:3]
    return VetoDecision(
        instrument=str(obj.get("instrument") or instrument),
        action=action,
        confidence=max(0.0, min(1.0, conf)),
        risk_tags=tags,
        reasons=reasons or ["无理由"],
    )


def judge_one(brief: str, instrument: str, temperature: float = 0.2) -> VetoDecision:
    """兼容旧路径：单次裁判（无多轮）。"""
    client, model = _llm_client()
    user = prompts.user_prompt_for_instrument(brief) + "\n\n" + prompts.DEBATE_HINT
    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=[
            {"role": "system", "content": prompts.SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
    )
    content = (resp.choices[0].message.content or "") if resp.choices else ""
    d = debate_mod.parse_decision(content, instrument)
    d.instrument = instrument
    return d


def judge_with_debate(
    brief: str,
    instrument: str,
    *,
    debate_rounds: int = 1,
) -> tuple[VetoDecision, dict[str, Any]]:
    client, model = _llm_client()
    return debate_mod.run_debate(
        client, model, instrument, brief, debate_rounds=debate_rounds
    )


def resolve_buy_candidates(
    day: str,
    account: str | None,
    *,
    use_ump: bool,
) -> tuple[list[str], pd.Series, pd.DataFrame]:
    """复用 make_trade_plan 的选股/板块/UMP，保证否决名单对得上。"""
    import make_trade_plan as mtp

    cfg = mtp.load_config(None, account)
    mtp.CFG = cfg
    sig_file = mtp.latest_signal_file(day)
    signals = pd.read_csv(sig_file)
    score = signals.set_index("instrument")["score"]
    holdings = mtp.load_holdings(account)
    held = holdings["instrument"].tolist()
    topk = int(cfg["strategy"]["topk"])
    n_drop = int(cfg["strategy"]["n_drop"])
    _, buy_candi = mtp.select_trades(score, held, topk, n_drop)
    exclude_boards = cfg.get("execution", {}).get("exclude_boards", []) or []
    if exclude_boards:
        buy_candi = [b for b in buy_candi if not mtp.excluded_by_board(b, exclude_boards)]
    buy_candi = buy_candi[: topk * 3]
    if use_ump:
        sys.path.insert(0, str(QUANT / "validation"))
        from ump_judge import live_veto_instruments

        vetoed = live_veto_instruments(day, score)
        buy_candi = [b for b in buy_candi if b not in vetoed]
    # 日频只裁判「可能真正买入」的前若干名，控 LLM 成本
    n_judge = max(int(cfg["strategy"].get("n_drop", 1)), 1) + 2
    buy_candi = buy_candi[:n_judge]
    return buy_candi, score, signals


def _fail_open(day: str, reason: str, candidates: list[str], meta: dict) -> Path:
    vf = VetoFile(
        date=day,
        status="fail_open",
        fail_reason=reason,
        candidates=candidates,
        decisions=[],
        vetoed=[],
        meta=meta,
    )
    return write_veto_file(vf)


def run(
    day: str,
    account: str | None,
    *,
    dry_run: bool = False,
    use_ump: bool | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    max_vetoes: int = DEFAULT_MAX_VETOES,
    timeout_hint_sec: float | None = None,
) -> Path:
    t0 = time.time()
    import make_trade_plan as mtp

    cfg = mtp.load_config(None, account)
    if use_ump is None:
        use_ump = bool(cfg.get("execution", {}).get("use_ump", False))
    ta_cfg = cfg.get("execution", {}).get("ta_veto", {}) or {}
    confidence_threshold = float(ta_cfg.get("confidence_threshold", confidence_threshold))
    max_vetoes = int(ta_cfg.get("max_vetoes", max_vetoes))
    debate_rounds = int(ta_cfg.get("debate_rounds", 1))
    vendors = ta_cfg.get("vendors") or V.DEFAULT_VENDORS
    lookback = ta_cfg.get("lookback_days") or {}
    limits = ta_cfg.get("max_items") or {}
    # 多轮辩论：每票约 2+2*rounds 次 LLM，默认放宽超时
    if timeout_hint_sec is None:
        timeout_hint_sec = float(ta_cfg.get("timeout_sec", 180 * max(1, debate_rounds)))

    meta: dict[str, Any] = {
        "account": account,
        "dry_run": dry_run,
        "use_ump": use_ump,
        "debate_rounds": debate_rounds,
        "vendors": vendors,
    }

    try:
        candidates, score, signals = resolve_buy_candidates(day, account, use_ump=use_ump)
    except Exception as exc:  # noqa: BLE001 — fail-open
        return _fail_open(day, f"resolve_candidates: {exc}", [], meta)

    if not candidates:
        vf = VetoFile(
            date=day,
            status="ok",
            candidates=[],
            decisions=[],
            vetoed=[],
            confidence_threshold=confidence_threshold,
            max_vetoes=max_vetoes,
            meta={**meta, "elapsed_sec": round(time.time() - t0, 3), "note": "no_candidates"},
        )
        return write_veto_file(vf)

    if dry_run:
        decisions = [
            VetoDecision(
                instrument=c,
                action="pass",
                confidence=0.0,
                risk_tags=[],
                reasons=["dry-run：跳过 LLM"],
            )
            for c in candidates
        ]
        vf = VetoFile(
            date=day,
            status="ok",
            candidates=candidates,
            decisions=decisions,
            vetoed=[],
            confidence_threshold=confidence_threshold,
            max_vetoes=max_vetoes,
            meta={**meta, "elapsed_sec": round(time.time() - t0, 3), "mode": "dry_run"},
        )
        return write_veto_file(vf)

    # 密钥检查提前 fail-open，避免半截调用
    try:
        _, model = _llm_client()
        meta["model"] = model
    except Exception as exc:  # noqa: BLE001
        return _fail_open(day, f"llm_init: {exc}", candidates, meta)

    rank_map: dict[str, int] = {}
    if "rank" in signals.columns:
        rank_map = dict(zip(signals["instrument"].astype(str), signals["rank"].astype(int)))

    try:
        price_ctx = ashare.load_price_context(candidates, day)
        flags_df = ashare.load_trade_flags(candidates, day)
    except Exception as exc:  # noqa: BLE001
        return _fail_open(day, f"ashare_data: {exc}", candidates, meta)

    decisions: list[VetoDecision] = []
    traces: list[dict[str, Any]] = []
    research_meta: list[dict[str, Any]] = []
    try:
        for inst in candidates:
            if time.time() - t0 > timeout_hint_sec:
                return _fail_open(
                    day,
                    f"timeout>{timeout_hint_sec}s after {len(decisions)} judges",
                    candidates,
                    {**meta, "partial_decisions": len(decisions)},
                )
            ctx = price_ctx.get(inst, {"instrument": inst})
            fl = {}
            if inst in flags_df.index:
                row = flags_df.loc[inst]
                fl = {
                    "limit_up": bool(row.get("limit_up", False)),
                    "limit_down": bool(row.get("limit_down", False)),
                    "suspended": bool(row.get("suspended", False)),
                }
            # 零 Key：公告 + 新闻 + 基本面（舆情关闭）
            try:
                bundle = V.fetch_research_bundle(
                    inst, day, vendors=vendors, lookback=lookback, limits=limits
                )
                research_text = V.format_bundle_for_brief(bundle)
                research_meta.append(bundle.get("meta") or {})
            except Exception as exc:  # noqa: BLE001
                research_text = f"研究数据拉取异常: {exc}"
                research_meta.append({"error": str(exc)})
            brief = ashare.build_candidate_brief(
                inst,
                float(score[inst]) if inst in score.index else None,
                rank_map.get(inst),
                ctx,
                fl,
                research_text=research_text,
            )
            decision, trace = judge_with_debate(
                brief, inst, debate_rounds=debate_rounds
            )
            decisions.append(decision)
            traces.append({
                "instrument": inst,
                "n_ann": (research_meta[-1] or {}).get("n_announcements"),
                "n_news": (research_meta[-1] or {}).get("n_news"),
                "has_fund": (research_meta[-1] or {}).get("has_fundamentals"),
                "decision": decision.to_dict(),
                "debate_steps": [
                    {"role": s.get("role"), "chars": len(str(s.get("content") or ""))}
                    for s in (trace.get("steps") or [])
                ],
            })
    except Exception as exc:  # noqa: BLE001
        return _fail_open(day, f"llm_judge: {exc}", candidates, meta)

    vetoed = apply_veto_policy(
        decisions,
        candidates=candidates,
        confidence_threshold=confidence_threshold,
        max_vetoes=max_vetoes,
    )
    vf = VetoFile(
        date=day,
        status="ok",
        candidates=candidates,
        decisions=decisions,
        vetoed=vetoed,
        confidence_threshold=confidence_threshold,
        max_vetoes=max_vetoes,
        meta={
            **meta,
            "elapsed_sec": round(time.time() - t0, 3),
            "research_meta": research_meta,
            "traces": traces,
        },
    )
    return write_veto_file(vf)


def main() -> int:
    p = argparse.ArgumentParser(description="TA qualitative buy-veto overlay")
    p.add_argument("--date", required=True, help="信号/交易日 YYYY-MM-DD")
    p.add_argument("--account", default=None, help="账户名（决定 topk/板块/UMP）")
    p.add_argument("--dry-run", action="store_true", help="不调 LLM，写全 pass")
    p.add_argument("--ump", action="store_true", help="强制启用 UMP 过滤后再裁判")
    p.add_argument("--no-ump", action="store_true", help="强制关闭 UMP")
    p.add_argument("--confidence", type=float, default=None)
    p.add_argument("--max-vetoes", type=int, default=None)
    args = p.parse_args()

    use_ump = None
    if args.ump:
        use_ump = True
    if args.no_ump:
        use_ump = False

    kw: dict[str, Any] = {"dry_run": args.dry_run, "use_ump": use_ump}
    if args.confidence is not None:
        kw["confidence_threshold"] = args.confidence
    if args.max_vetoes is not None:
        kw["max_vetoes"] = args.max_vetoes

    path = run(args.date, args.account, **kw)
    print(f"[OK] ta_veto → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
