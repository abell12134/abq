"""实盘舆情硬伤筛：Cursor Agent 联网检索 → veto JSON + 执行清单 + orders_exec。

仅用于 live 手工线。失败 / 无密钥 / SDK 异常 → fail-open（exit 0）。

用法：
    python overlays/sentiment_veto/run_sentiment.py --date 2026-08-03 --account live_manual_10k
    python overlays/sentiment_veto/run_sentiment.py --date 2026-08-03 --account live_manual_10k --dry-run
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
sys.path.insert(0, str(QUANT / "ops"))
sys.path.insert(0, str(QUANT / "contracts"))

import common as C  # noqa: E402
import schemas as S  # noqa: E402
from overlays.sentiment_veto import prompts_cn as prompts  # noqa: E402
from overlays.sentiment_veto.schema import (  # noqa: E402
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
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _secret_get(key: str, default: str | None = None) -> str | None:
    import os

    s = _load_secret()
    return s.get(key) or os.environ.get(key) or default


def _fail_open(day: str, candidates: list[str], reason: str,
               meta: dict[str, Any] | None = None) -> int:
    payload = VetoFile(
        date=day,
        status="fail_open",
        candidates=candidates,
        decisions=[],
        vetoed=[],
        fail_reason=reason,
        meta=meta or {},
    )
    path = write_veto_file(payload)
    print(f"[WARN] sentiment fail-open: {reason} → {path}")
    C.alert("WARN", f"舆情硬伤筛 fail-open：{reason}", day)
    return 0


def _parse_agent_json(text: str) -> list[VetoDecision]:
    text = (text or "").strip()
    if not text:
        return []
    # 允许 ```json ... ``` 包裹
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    blob = fence.group(1) if fence else None
    if blob is None:
        m = re.search(r"\{.*\}", text, re.S)
        blob = m.group(0) if m else None
    if not blob:
        return []
    try:
        obj = json.loads(blob)
    except json.JSONDecodeError:
        return []
    rows = obj.get("decisions") if isinstance(obj, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[VetoDecision] = []
    for r in rows:
        if not isinstance(r, dict) or "instrument" not in r:
            continue
        action = str(r.get("action", "pass")).lower()
        if action not in {"veto", "pass"}:
            action = "pass"
        try:
            conf = float(r.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        out.append(VetoDecision(
            instrument=str(r["instrument"]).upper(),
            name=str(r.get("name") or ""),
            action=action,
            confidence=max(0.0, min(1.0, conf)),
            risk_tags=[str(t) for t in (r.get("risk_tags") or [])][:5],
            reasons=[str(x) for x in (r.get("reasons") or [])][:3],
            sources=[str(x) for x in (r.get("sources") or [])][:5],
        ))
    return out


def _lookup_names(instruments: list[str]) -> dict[str, str]:
    """尽力用 baostock 取简称；失败则空串。"""
    names: dict[str, str] = {i: "" for i in instruments}
    try:
        import baostock as bs
    except ImportError:
        return names
    lg = bs.login()
    if lg.error_code != "0":
        return names
    try:
        for inst in instruments:
            code = inst[2:]
            pref = "sh." if inst.startswith("SH") else "sz."
            rs = bs.query_stock_basic(code=pref + code)
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if rows:
                names[inst] = str(rows[0][1] or "")
    finally:
        bs.logout()
    return names


def _run_cursor_agent(prompt_text: str) -> tuple[str, dict[str, Any]]:
    """调用 Cursor SDK Local Agent；返回 (assistant_text, meta)。"""
    api_key = _secret_get("CURSOR_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 CURSOR_API_KEY（configs/secret.env）")
    model = _secret_get("CURSOR_AGENT_MODEL", "composer-2.5") or "composer-2.5"
    runtime = (_secret_get("CURSOR_AGENT_RUNTIME", "local") or "local").lower()
    if runtime != "local":
        raise RuntimeError(
            f"当前仅支持 CURSOR_AGENT_RUNTIME=local（收到 {runtime}）；"
            "cloud 会因 data/ gitignore 看不到订单文件"
        )

    from cursor_sdk import Agent, AgentOptions, LocalAgentOptions

    full = prompts.SYSTEM_RULES + "\n\n" + prompt_text
    meta: dict[str, Any] = {"model": model, "runtime": "local"}
    t0 = time.time()
    # 优先 one-shot；失败再回退 create+send
    try:
        result = Agent.prompt(
            full,
            AgentOptions(
                api_key=api_key,
                model=model,
                local=LocalAgentOptions(cwd=str(QUANT)),
            ),
        )
        status = getattr(result, "status", None)
        text = getattr(result, "result", None) or getattr(result, "text", None) or ""
        if callable(text):
            text = text()
        meta["elapsed_sec"] = round(time.time() - t0, 1)
        meta["agent_api"] = "Agent.prompt"
        meta["run_status"] = str(status)
        if str(status) == "error":
            raise RuntimeError(f"Cursor Agent run status=error id={getattr(result, 'id', '?')}")
        return str(text), meta
    except Exception as first:
        meta["prompt_error"] = str(first)[:200]

    with Agent.create(
        model=model,
        api_key=api_key,
        local=LocalAgentOptions(cwd=str(QUANT)),
    ) as agent:
        meta["agent_id"] = getattr(agent, "agent_id", None)
        run = agent.send(full)
        meta["run_id"] = getattr(run, "id", None)
        text = run.text()
        result = run.wait()
        meta["elapsed_sec"] = round(time.time() - t0, 1)
        meta["agent_api"] = "Agent.create+send"
        meta["run_status"] = str(getattr(result, "status", None))
        if str(getattr(result, "status", "")) == "error":
            raise RuntimeError(f"Cursor Agent run status=error id={meta.get('run_id')}")
        return str(text), meta


def _write_orders_exec(account: str, day: str, orders: pd.DataFrame,
                       vetoed: set[str]) -> Path:
    dirs = C.ensure_account_dirs(account)
    out_dir = dirs.get("orders_exec") or (dirs["orders"].parent / "orders_exec")
    out_dir.mkdir(parents=True, exist_ok=True)
    keep = orders.copy()
    if not keep.empty:
        mask = ~((keep["side"].str.upper() == "BUY") & keep["instrument"].isin(vetoed))
        keep = keep.loc[mask].copy()
    out = out_dir / f"{day}.csv"
    S.write_csv("orders", keep, out)
    return out


def _write_checklist(account: str, day: str, orders: pd.DataFrame,
                     decisions: list[VetoDecision], vetoed: list[str],
                     names: dict[str, str], meta: dict[str, Any]) -> Path:
    dirs = C.ensure_account_dirs(account)
    reports = dirs["reports"]
    reports.mkdir(parents=True, exist_ok=True)
    out = reports / f"sentiment_checklist_{day}.md"
    dec_map = {d.instrument: d for d in decisions}

    lines = [
        f"# 舆情硬伤筛 · 可执行清单 {day}",
        "",
        "> **研究/学习用途，不构成投资建议。** 本层仅过滤公开舆情硬伤；模型原单见 `orders/`，",
        "> 人工请优先执行 `orders_exec/`。失败时 fail-open（不否决）。",
        "",
        f"- 账户: `{account}`　模型: `{meta.get('model', '?')}`　耗时: {meta.get('elapsed_sec', '?')}s",
        f"- 生效否决: {', '.join(vetoed) if vetoed else '无'}",
        "",
        "## 执行顺序（先卖后买）",
        "",
        "| 顺序 | 方向 | 代码 | 名称 | 股数 | 参考价 | 舆情 |",
        "|---:|---|---|---|---:|---:|---|",
    ]
    side_ord = {"SELL": 0, "BUY": 1}
    sorted_o = orders.copy()
    if not sorted_o.empty:
        sorted_o["_ord"] = sorted_o["side"].str.upper().map(side_ord)
        sorted_o = sorted_o.sort_values(["_ord", "instrument"])
    seq = 0
    for r in sorted_o.itertuples():
        side = str(r.side).upper()
        inst = str(r.instrument)
        name = names.get(inst) or (dec_map.get(inst).name if inst in dec_map else "") or ""
        if side == "BUY" and inst in set(vetoed):
            tag = "⛔ 否决（勿买）"
        elif side == "BUY":
            tag = "✅ 通过"
        else:
            tag = "—（卖出不筛）"
        if side == "BUY" and inst in set(vetoed):
            continue  # 执行清单不列否决买单
        seq += 1
        lines.append(
            f"| {seq} | {side} | {inst} | {name} | {int(r.shares)} | "
            f"{float(r.ref_price):.2f} | {tag} |"
        )

    lines += ["", "## 逐票说明", ""]
    for inst in [str(x) for x in orders.loc[
            orders["side"].str.upper() == "BUY", "instrument"].tolist()]:
        d = dec_map.get(inst)
        name = names.get(inst) or (d.name if d else "") or ""
        if inst in set(vetoed):
            verdict = "**否决**"
        elif d and d.action == "veto":
            verdict = "建议否决但未达阈值/上限（未生效）"
        else:
            verdict = "通过"
        lines.append(f"### {inst} {name} — {verdict}")
        if d:
            lines.append(f"- action={d.action}　confidence={d.confidence:.2f}")
            if d.risk_tags:
                lines.append(f"- tags: {', '.join(d.risk_tags)}")
            for reason in d.reasons:
                lines.append(f"- {reason}")
            for src in d.sources:
                lines.append(f"- 来源: {src}")
        else:
            lines.append("- （无模型决策明细）")
        lines.append("")

    lines += [
        "## 文件",
        f"- 模型原单: `data/accounts/{account}/orders/{day}.csv`",
        f"- 执行清单: `data/accounts/{account}/orders_exec/{day}.csv`",
        f"- 否决 JSON: `data/overlays/sentiment_veto/{day}.json`",
        "",
    ]
    out.write_text("\n".join(lines))
    return out


def run(day: str, account: str, dry_run: bool = False,
        conf_th: float = DEFAULT_CONFIDENCE_THRESHOLD,
        max_vetoes: int = DEFAULT_MAX_VETOES) -> int:
    dirs = C.ensure_account_dirs(account)
    of = dirs["orders"] / f"{day}.csv"
    if not of.exists():
        return _fail_open(day, [], f"缺少订单 {of}")

    orders = S.read_csv("orders", of)
    buys = orders[orders["side"].str.upper() == "BUY"].copy() if not orders.empty else orders
    candidates = buys["instrument"].astype(str).tolist()
    if not candidates:
        payload = VetoFile(
            date=day, status="ok", candidates=[], decisions=[], vetoed=[],
            meta={"note": "无 BUY，跳过舆情筛"},
        )
        write_veto_file(payload)
        _write_orders_exec(account, day, orders, set())
        print(f"[OK] {day} 无买入单，舆情筛跳过；orders_exec 已同步原单")
        return 0

    all_inst = orders["instrument"].astype(str).tolist() if not orders.empty else []
    names = _lookup_names(sorted(set(all_inst) | set(candidates)))
    rows = []
    for r in buys.itertuples():
        inst = str(r.instrument)
        rows.append({
            "instrument": inst,
            "name": names.get(inst, ""),
            "shares": int(r.shares),
            "ref_price": float(r.ref_price),
        })

    user_prompt = prompts.build_user_prompt(day, rows)
    if dry_run:
        print("[dry-run] 将送入 Cursor Agent 的候选：")
        for r in rows:
            print(f"  {r['instrument']} {r['name']} BUY {r['shares']}@{r['ref_price']}")
        return _fail_open(day, candidates, "dry-run：未调用 Agent",
                          meta={"dry_run": True})

    try:
        text, meta = _run_cursor_agent(user_prompt)
    except Exception as e:
        return _fail_open(day, candidates, f"Cursor Agent 调用失败: {e}")

    decisions = _parse_agent_json(text)
    if not decisions:
        # 保留原始文本片段便于排障
        snippet = (text or "")[:500]
        return _fail_open(
            day, candidates, "Agent 输出无法解析为 decisions JSON",
            meta={**meta, "raw_snippet": snippet},
        )

    # 补全未返回的候选为 pass
    got = {d.instrument for d in decisions}
    for inst in candidates:
        if inst not in got:
            decisions.append(VetoDecision(
                instrument=inst, name=names.get(inst, ""),
                action="pass", confidence=0.0,
                reasons=["Agent 未返回该标的，默认 pass"],
            ))

    vetoed = apply_veto_policy(
        decisions, candidates=candidates,
        confidence_threshold=conf_th, max_vetoes=max_vetoes,
    )
    payload = VetoFile(
        date=day,
        status="ok",
        candidates=candidates,
        decisions=decisions,
        vetoed=vetoed,
        confidence_threshold=conf_th,
        max_vetoes=max_vetoes,
        meta=meta,
    )
    vpath = write_veto_file(payload)
    epath = _write_orders_exec(account, day, orders, set(vetoed))
    cpath = _write_checklist(account, day, orders, decisions, vetoed, names, meta)

    print(f"[OK] 舆情硬伤筛完成：候选 {len(candidates)}，否决 {len(vetoed)}"
          f"{(' → ' + ', '.join(vetoed)) if vetoed else ''}")
    print(f"  JSON: {vpath}")
    print(f"  执行单: {epath}")
    print(f"  清单: {cpath}")
    if vetoed:
        C.alert("INFO", f"[{account}] 舆情否决 {len(vetoed)} 只："
                f"{', '.join(vetoed)}；请执行 orders_exec/{day}.csv", day)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="实盘舆情硬伤筛（Cursor Agent）")
    p.add_argument("--date", required=True, help="订单日 YYYY-MM-DD")
    p.add_argument("--account", default="live_manual_10k")
    p.add_argument("--dry-run", action="store_true", help="只列候选，不调用 Agent")
    p.add_argument("--confidence", type=float, default=DEFAULT_CONFIDENCE_THRESHOLD)
    p.add_argument("--max-vetoes", type=int, default=DEFAULT_MAX_VETOES)
    args = p.parse_args()
    return run(args.date, args.account, args.dry_run, args.confidence, args.max_vetoes)


if __name__ == "__main__":
    raise SystemExit(main())
