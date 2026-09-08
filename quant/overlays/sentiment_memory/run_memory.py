"""舆情长期记忆流水线：采集 → 入库/向量化 → LLM 摘要。

用法：
  python overlays/sentiment_memory/run_memory.py --account live_manual_10k
  python overlays/sentiment_memory/run_memory.py --account live_manual_10k --dry-run
  python overlays/sentiment_memory/run_memory.py --instruments SH600299,SZ002739
  python overlays/sentiment_memory/run_memory.py --ingest-only   # 只拉全局电报入库
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))
sys.path.insert(0, str(QUANT / "ops"))

import common as C  # noqa: E402

from overlays.sentiment_memory import analyze as A  # noqa: E402
from overlays.sentiment_memory import sources as S  # noqa: E402
from overlays.sentiment_memory import store  # noqa: E402
from overlays.sentiment_memory.llm_router import (  # noqa: E402
    default_overlay_force,
    is_peak_hour,
    overlays_local_only,
)

TZ = ZoneInfo("Asia/Shanghai")

# 持仓追踪同口径：聚合 fills 时的账户范围（与 webapp.server.ACCOUNTS 对齐）
TRADED_ACCOUNTS = [
    "research_sim_100k",
    "live_manual_10k",
    "shadow_ctrl_sim",
    "shadow_ta_sim",
]


def _lookup_names(instruments: list[str]) -> dict[str, str]:
    names = {i: "" for i in instruments}
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


def normalize_instrument(raw: str) -> str | None:
    """把用户输入规范成 SH600000 / SZ000001。支持 600000、sh600000、600000.SH 等。"""
    s = (raw or "").strip().upper().replace(" ", "")
    if not s:
        return None
    s = s.replace(".SH", "").replace(".SZ", "").replace(".", "")
    if s.startswith(("SH", "SZ")) and len(s) >= 8 and s[2:].isdigit():
        return s[:2] + s[2:8]
    if s.isdigit() and len(s) == 6:
        # 6 开头上交所；0/3 深交所；其余也按常见规则：5/9 上证基金/B，默认 SH
        if s[0] in {"0", "3"}:
            return "SZ" + s
        return "SH" + s
    return None


def resolve_universe(account: str | None,
                     instruments: list[str] | None = None) -> list[str]:
    """待跟踪标的。显式传入 instruments 时只分析这些；否则取账户持仓/订单/已跟踪。"""
    found: set[str] = set()
    if instruments:
        for raw in instruments:
            inst = normalize_instrument(raw) or (
                raw.strip().upper() if raw and raw.strip() else None)
            if inst:
                found.add(inst)
        return sorted(i for i in found if len(i) >= 8 and i[:2] in {"SH", "SZ"}
                      and i[2:].isdigit())

    if account:
        dirs = C.ensure_account_dirs(account)
        hf = dirs["nav"] / "holdings.csv"
        if hf.exists():
            try:
                import pandas as pd
                h = pd.read_csv(hf)
                if "instrument" in h.columns:
                    found.update(h["instrument"].astype(str).str.upper().tolist())
            except Exception:  # noqa: BLE001
                pass
        odir = dirs["orders"]
        if odir.exists():
            days = sorted(f.stem for f in odir.glob("????-??-??.csv"))
            if days:
                try:
                    import pandas as pd
                    o = pd.read_csv(odir / f"{days[-1]}.csv")
                    if "instrument" in o.columns:
                        found.update(o["instrument"].astype(str).str.upper().tolist())
                except Exception:  # noqa: BLE001
                    pass
        found.update(store.list_tracked_instruments())
    return sorted(i for i in found if len(i) >= 6 and i[:2] in {"SH", "SZ"})


def ingest_global(lookback_days: int = 7) -> dict[str, Any]:
    items = S.collect_global_feeds(lookback_days=lookback_days)
    added = store.append_raw(items)
    return {"fetched": len(items), "added": added,
            "by_source": {
                "cls": sum(1 for x in items if x["source"] == "cls"),
                "sina": sum(1 for x in items if x["source"] == "sina"),
            }}


def _traded_universe() -> list[str]:
    """聚合各账户 fills，返回所有曾买卖过的标的（与持仓追踪页同口径）。"""
    found: set[str] = set()
    try:
        import pandas as pd
    except ImportError:
        return []
    for acc in TRADED_ACCOUNTS:
        try:
            dirs = C.ensure_account_dirs(acc)
            fdir = dirs["fills"]
            for f in sorted(fdir.glob("????-??-??.csv")):
                if not f.with_suffix(".done").exists():
                    continue
                try:
                    df = pd.read_csv(f)
                except Exception:  # noqa: BLE001
                    continue
                if "instrument" in df.columns:
                    found.update(df["instrument"].astype(str).str.upper().tolist())
        except Exception:  # noqa: BLE001
            continue
    return sorted(i for i in found if len(i) >= 6 and i[:2] in {"SH", "SZ"})


def run(
    day: str | None = None,
    account: str | None = "live_manual_10k",
    instruments: list[str] | None = None,
    lookback_days: int = 90,
    dry_run: bool = False,
    ingest_only: bool = False,
    force_llm: str | None = None,
    limit: int | None = None,
    all_traded: bool = False,
    only_new: bool = False,
) -> int:
    day = day or datetime.now(TZ).strftime("%Y-%m-%d")
    store.ensure_dirs()
    force_llm = force_llm if force_llm is not None else default_overlay_force()
    print(f"[sentiment_memory] day={day} local_only={overlays_local_only()} "
          f"force_llm={force_llm or 'auto'} peak_hour={is_peak_hour()} "
          f"lookback={lookback_days}d limit={limit} all_traded={all_traded} only_new={only_new}")

    g = ingest_global(lookback_days=min(lookback_days, 14))
    print(f"[OK] 全局电报入库 fetched={g['fetched']} added={g['added']} "
          f"{g['by_source']}")
    if ingest_only:
        return 0

    if all_traded:
        universe = _traded_universe()
        if not universe:
            print("[WARN] --all-traded 但未从 fills 聚合到任何标的")
            return 0
    else:
        universe = resolve_universe(account, instruments)
    if not universe:
        print("[WARN] 无跟踪标的（持仓/订单为空且未指定 --instruments）")
        return 0

    cat = store.load_catalog().get("instruments") or {}
    # only_new：只跑尚无报告的票（不在 catalog 或无 latest_date）
    if only_new:
        before = len(universe)
        universe = [i for i in universe
                    if not (cat.get(i) or {}).get("latest_date")]
        print(f"[OK] --only-new 过滤：{before} → {len(universe)} 只（剔除已有报告）")

    # limit：优先跑尚未分析（不在 catalog）或报告最旧的票，避免反复刷新头部几只
    if limit and limit > 0:
        def _rank(inst: str) -> tuple:
            e = cat.get(inst)
            # 0=从未分析；否则按 latest_date 升序（最旧优先）；再按代码兜底
            if not e or not e.get("latest_date"):
                return (0, "", inst)
            return (1, str(e.get("latest_date")), inst)
        universe = sorted(universe, key=_rank)[:limit]
        print(f"[OK] 限流 {limit}：实际排队 {len(universe)} 只（优先未分析/最旧）")

    names = _lookup_names(universe)
    print(f"[OK] 跟踪标的 {len(universe)}: "
          + ", ".join(f"{i}{(' '+names[i]) if names.get(i) else ''}"
                      for i in universe))

    cache: dict[str, list] = {}
    # 预拉全局源一次，各票复用
    cache["cls"] = S.fetch_cls(lookback_days=min(lookback_days, 14))
    cache["sina"] = S.fetch_sina(lookback_days=min(lookback_days, 14))

    ok, fail = 0, 0
    for inst in universe:
        name = names.get(inst, "")
        try:
            news = S.collect_for_instrument(
                inst, name=name, lookback_days=lookback_days, global_cache=cache)
            # 把全局库里已存、关键字命中的也并入（长期记忆回放）
            hist = S.filter_for_instrument(
                store.load_raw(lookback_days=lookback_days), inst, name)
            seen = {n["id"] for n in news}
            for h in hist:
                if h.get("id") not in seen:
                    news.append(h)
                    seen.add(h["id"])
            news.sort(key=lambda x: str(x.get("published", "")), reverse=True)
            print(f"  · {inst} {name}: 舆情 {len(news)} 条 → 分析中…")
            report = A.analyze_instrument(
                day, inst, name, news, dry_run=dry_run, force_llm=force_llm)
            print(f"    sentiment={report.get('sentiment')} "
                  f"score={report.get('score')} | {report.get('headline')}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] {inst}: {e}")
            C.alert("WARN", f"舆情记忆分析失败 {inst}: {e}", day)
            fail += 1

    print(f"[DONE] ok={ok} fail={fail} catalog="
          f"{store.ROOT / 'catalog.json'}")
    return 0 if fail == 0 else 0  # 不阻断流水线


def main() -> int:
    p = argparse.ArgumentParser(description="舆情长期记忆：采集+摘要+向量库")
    p.add_argument("--date", default=None, help="分析日 YYYY-MM-DD")
    p.add_argument("--account", default="live_manual_10k")
    p.add_argument("--instruments", default=None,
                   help="逗号分隔代码，如 SH600299,SZ002739")
    p.add_argument("--lookback", type=int, default=90,
                   help="回看天数，默认 90（约三个月）；允许 30–90")
    p.add_argument("--dry-run", action="store_true", help="只采集入库，不调 LLM")
    p.add_argument("--ingest-only", action="store_true", help="只拉全局电报")
    p.add_argument("--force-llm", choices=["peak", "offpeak"], default="peak",
                   help="LLM 路由：默认 peak=本地自部署；offpeak=DeepSeek")
    p.add_argument("--limit", type=int, default=None,
                   help="本次最多分析 N 只（优先未分析/报告最旧），0 或不传=不限制")
    p.add_argument("--all-traded", action="store_true",
                   help="聚合四账户 fills 作为宇宙（与持仓追踪页同口径）")
    p.add_argument("--only-new", action="store_true",
                   help="只跑尚无报告的票（需先有宇宙：--all-traded 或 --account）")
    args = p.parse_args()
    lookback = max(30, min(90, int(args.lookback)))
    instruments = None
    if args.instruments:
        instruments = [x.strip() for x in args.instruments.split(",") if x.strip()]
    account = (args.account or "").strip() or None
    limit = args.limit if (args.limit and args.limit > 0) else None
    return run(
        day=args.date,
        account=account,
        instruments=instruments,
        lookback_days=lookback,
        dry_run=args.dry_run,
        ingest_only=args.ingest_only,
        force_llm=args.force_llm,
        limit=limit,
        all_traded=args.all_traded,
        only_new=args.only_new,
    )


if __name__ == "__main__":
    raise SystemExit(main())
