"""板块预测 LLM 后置简报：解释已选出的 claim，禁止改排名 / p_beat / action。

复用 sentiment_memory.llm_router（本地高峰模型）。失败 fail-open，数字预测仍落盘。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from overlays.llm_json import parse_json_object
from overlays.sentiment_memory import llm_router

from .schema import LLM_NOTE

log = logging.getLogger(__name__)

_CODE_PREFIX = re.compile(r"^[A-Z]\d+")

SYSTEM = """你是 A 股行业研究助理（研究/学习用途，不构成投资建议）。
已有截面模型给出的行业占优/落后候选（相对中证500，10 或 20 个交易日）。
你的任务：根据给定的量化证据、题材、快讯，为每条候选写简短理由与风险。

硬规则：
- 不得改写排名、不得给出新的概率、不得增删候选行业；
- industry 与 horizon_days 必须从输入原样抄回；
- stance 只能是 agree（材料支持该方向）或 challenge（材料与模型方向冲突或证据不足）；
- 证据不足就 challenge，不要编造公告、政策或数字；
- thesis ≤120 字，risks 至多 2 条、每条≤40 字。
禁止输出分析过程或思维链。回复的第一个字符必须是 `{`，只输出一个 JSON 对象。"""
_RETRY = "不要分析、不要复述规则。只输出 JSON，第一个字符必须是 {，字段为 headline 与 items。"
_PREFER = frozenset({"items", "headline"})


def _display_name(industry: str) -> str:
    return _CODE_PREFIX.sub("", industry or "") or industry


def _norm_ind(s: str) -> str:
    return _CODE_PREFIX.sub("", (s or "").strip())


def _claim_pack(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "industry": c.get("industry"),
        "horizon_days": c.get("horizon_days"),
        "action": c.get("action"),
        "p_beat": c.get("p_beat"),
        "expected_excess": c.get("expected_excess"),
        "n_stocks": c.get("n_stocks"),
        "evidence": (c.get("evidence") or [])[:3],
        "related_concepts": (c.get("related_concepts") or [])[:4],
        "news_hits": c.get("news_hits") or 0,
    }


def _news_for_claims(day: str, industries: set[str]) -> dict[str, list[str]]:
    """从大盘快照快讯里抽入选行业标题，失败则空。"""
    out: dict[str, list[str]] = {i: [] for i in industries}
    try:
        from overlays.market_board import store as board_store
        snap = board_store.load_daily(day) or {}
    except Exception:  # noqa: BLE001
        return out
    policy: list[str] = []
    for it in (snap.get("news") or {}).get("pool_feed") or []:
        ind = it.get("industry")
        title = str(it.get("title") or "").strip()
        if ind in out and title and title not in out[ind]:
            out[ind].append(title[:80])
            out[ind] = out[ind][:4]
    for it in (snap.get("news") or {}).get("global_feed") or []:
        if not it.get("is_policy"):
            continue
        title = str(it.get("title") or "").strip()
        if title:
            policy.append(title[:80])
        if len(policy) >= 6:
            break
    cyc = snap.get("cycle") or {}
    return {"__policy__": policy, "__emotion__": [str(cyc.get("emotion") or "")], **out}


def build_user_prompt(day: str, claims: list[dict[str, Any]]) -> str:
    inds = {str(c.get("industry")) for c in claims if c.get("industry")}
    news = _news_for_claims(day, inds)
    emotion = (news.pop("__emotion__", None) or [""])[0]
    policy = news.pop("__policy__", []) or []
    packed = []
    for c in claims:
        row = _claim_pack(c)
        row["display_name"] = _display_name(str(row.get("industry") or ""))
        row["headlines"] = news.get(str(row.get("industry") or ""), [])[:4]
        packed.append(row)
    return (
        f"预测日 {day}。市场情绪（规则引擎，非你的判断）: {emotion or '未知'}。\n"
        f"近几日政策/宏观标题: {json.dumps(policy, ensure_ascii=False)}\n"
        "候选（必须逐条原样抄回 industry 与 horizon_days）:\n"
        f"{json.dumps(packed, ensure_ascii=False, indent=2)}\n"
        "输出 JSON（从 { 开始，不要前言）：\n"
        '{"headline":"≤40字总览","items":[{"industry":"与输入完全一致",'
        '"horizon_days":10,"stance":"agree|challenge","thesis":"…","risks":["…"]}]}'
    )


_STANCE = {
    "agree": "agree", "支持": "agree", "同意": "agree", "认同": "agree",
    "challenge": "challenge", "质疑": "challenge", "反对": "challenge",
    "冲突": "challenge", "不足": "challenge", "disagree": "challenge",
}


def _horizon(v: Any) -> int:
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"(\d+)", str(v or ""))
    return int(m.group(1)) if m else 0


def _extract_items(brief: Any) -> list[dict[str, Any]]:
    if isinstance(brief, list):
        return [x for x in brief if isinstance(x, dict)]
    if not isinstance(brief, dict):
        return []
    for k in ("items", "briefs", "candidates", "reports"):
        v = brief.get(k)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def _match_item(ind: str, h: int, unused: list[dict[str, Any]]) -> dict[str, Any] | None:
    name = _norm_ind(ind)
    for i, it in enumerate(unused):
        if _horizon(it.get("horizon_days")) != h:
            continue
        got = str(it.get("industry") or "")
        if got == ind or _norm_ind(got) == name:
            return unused.pop(i)
    best_i = -1
    best_len = 0
    for i, it in enumerate(unused):
        if _horizon(it.get("horizon_days")) != h:
            continue
        got = _norm_ind(str(it.get("industry") or ""))
        if not got or not name:
            continue
        if got in name or name in got:
            n = min(len(got), len(name))
            if n > best_len:
                best_len = n
                best_i = i
    if best_i >= 0:
        return unused.pop(best_i)
    return None


def merge_brief(claims: list[dict[str, Any]], by_industry: dict[str, Any],
                brief: dict[str, Any]) -> dict[str, Any]:
    """把 LLM 结果写到 llm 字段。不改 p_beat / rank / action / shadow。"""
    unused = _extract_items(brief)
    n = 0
    for c in claims:
        ind = str(c.get("industry") or "")
        h = int(c.get("horizon_days") or 0)
        it = _match_item(ind, h, unused)
        if not it:
            continue
        raw_stance = str(it.get("stance") or "").strip().lower()
        stance = _STANCE.get(raw_stance) or _STANCE.get(str(it.get("stance") or "").strip()) or "agree"
        if stance not in ("agree", "challenge"):
            stance = "agree"
        risks = [str(x).strip()[:80] for x in (it.get("risks") or []) if str(x).strip()][:2]
        thesis = str(it.get("thesis") or "").strip()[:200]
        blob = {
            "stance": stance,
            "thesis": thesis,
            "risks": risks,
            "note": LLM_NOTE,
        }
        c["llm"] = blob
        slot = (by_industry or {}).get(ind) or {}
        hslot = slot.get(f"h{h}")
        if isinstance(hslot, dict):
            hslot["llm"] = blob
        n += 1
    headline = str((brief or {}).get("headline") or "").strip()[:80]
    return {"headline": headline, "n_attached": n, "note": LLM_NOTE}


def attach(pred: dict[str, Any], day: str, *,
           force_llm: str | None = None, skip: bool = False) -> dict[str, Any]:
    """对已发出的预测文件补 LLM 字段。失败只记 errors。"""
    meta_out: dict[str, Any] = {"ok": False}
    if skip:
        pred["llm"] = {"ok": False, "skipped": True, "note": LLM_NOTE}
        return meta_out
    claims = pred.get("claims") or []
    if not claims:
        pred["llm"] = {"ok": False, "error": "无 claim", "note": LLM_NOTE}
        return meta_out
    pred["errors"] = [e for e in (pred.get("errors") or [])
                      if not str(e).startswith("LLM 简报")]
    force = force_llm if force_llm is not None else llm_router.default_overlay_force()
    user = build_user_prompt(day, claims)
    t0 = time.monotonic()
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": user}]
    text, chat_meta = "", {}
    try:
        text, chat_meta = llm_router.chat(
            messages, temperature=0.2, force=force, timeout=180.0)
        obj = parse_json_object(text, prefer_keys=_PREFER) or {}
        if not _extract_items(obj):
            text, chat_meta = llm_router.chat(
                messages + [{"role": "user", "content": _RETRY}],
                temperature=0.1, force=force, timeout=180.0)
            obj = parse_json_object(text, prefer_keys=_PREFER) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("板块 LLM 简报失败: %s", e)
        err = str(e)[:240]
        pred.setdefault("errors", []).append(f"LLM 简报: {err}")
        pred["llm"] = {"ok": False, "error": err, "note": LLM_NOTE}
        if pred.get("status") == "ok":
            pred["status"] = "partial"
        return meta_out

    attached = merge_brief(claims, pred.get("by_industry") or {}, obj)
    n_ok = int(attached.get("n_attached") or 0)
    pred["claims"] = claims
    usage = (chat_meta or {}).get("usage") or {}
    meta = {
        "ok": n_ok > 0,
        "headline": attached.get("headline") or "",
        "n_attached": n_ok,
        "latency_sec": round(time.monotonic() - t0, 2),
        "model": (chat_meta or {}).get("model"),
        "endpoint": (chat_meta or {}).get("label") or (chat_meta or {}).get("endpoint"),
        "backend": (chat_meta or {}).get("backend"),
        "completion_tokens": usage.get("completion_tokens"),
        "note": LLM_NOTE,
    }
    if n_ok == 0:
        meta["error"] = "未能把简报挂到候选"
        meta["parse_preview"] = (text or "")[:800]
        log.warning("板块 LLM 简报未挂上候选 preview=%s", meta["parse_preview"][:240])
        pred.setdefault("errors", []).append("LLM 简报: 未能挂到候选")
        if pred.get("status") == "ok":
            pred["status"] = "partial"
    elif not pred.get("errors"):
        pred["status"] = "ok"
    pred["llm"] = meta
    return pred["llm"]
