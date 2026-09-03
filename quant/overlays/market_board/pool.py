"""看板统计池加载（单点真相源）：三级加载链。

  1. data/meta/board_pool.csv              # 可选人工覆盖（instrument 列），存在且非空则优先
  2. data/signals/<最新日>.csv              # Plan C 生产信号池（默认，~499 只）
  3. datasets/.../instruments/csi500.txt   # 历史时点成分兜底（取当前在期）

返回 list[dict]: {instrument, name, industry}，全部大写归一。
行业/名称来自 data/meta/industry_map.csv（申万一级，离线可用）；
缺失时名称留空、行业标 "未知"，不触发外网请求（盘后管线必须离线可跑）。
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

log = logging.getLogger(__name__)

QUANT = Path(__file__).resolve().parents[2]


def _normalize(raw: str) -> str | None:
    s = (raw or "").strip().upper().replace(".", "")
    if len(s) < 3:
        return None
    if s[:2] in {"SH", "SZ", "BJ"} and s[2:].isdigit():
        return s
    if s.isdigit() and len(s) == 6:
        if s.startswith(("60", "68", "90")):
            return "SH" + s
        if s.startswith(("00", "30", "20")):
            return "SZ" + s
        if s.startswith(("4", "8")):
            return "BJ" + s
    return None


def _load_meta() -> dict[str, dict[str, str]]:
    """industry_map.csv -> {instrument: {industry, name}}。"""
    path = QUANT / "data" / "meta" / "industry_map.csv"
    out: dict[str, dict[str, str]] = {}
    if not path.exists():
        return out
    try:
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                inst = _normalize(row.get("instrument", ""))
                if inst:
                    out[inst] = {
                        "industry": (row.get("industry") or "").strip(),
                        "name": (row.get("name") or "").strip(),
                    }
    except Exception as e:  # noqa: BLE001
        log.warning("industry_map 读取失败: %s", e)
    return out


def _from_custom_csv(path: Path) -> list[str] | None:
    if not path.exists():
        return None
    try:
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:  # noqa: BLE001
        log.warning("board_pool.csv 读取失败: %s", e)
        return None
    out = []
    for row in rows:
        inst = _normalize(row.get("instrument", ""))
        if inst:
            out.append(inst)
    return sorted(set(out)) or None


def _from_signals() -> list[str] | None:
    sig_dir = QUANT / "data" / "signals"
    if not sig_dir.exists():
        return None
    days = sorted(p.stem for p in sig_dir.glob("20*.csv"))
    for day in reversed(days):
        try:
            with (sig_dir / f"{day}.csv").open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except Exception:  # noqa: BLE001
            continue
        out = [i for i in (_normalize(r.get("instrument", "")) for r in rows) if i]
        if out:
            log.info("池子来源: signals/%s.csv (%d 只)", day, len(out))
            return sorted(set(out))
    return None


def _from_csi500_file(day: str | None = None) -> list[str] | None:
    """qlib instruments/csi500.txt：code<TAB>start<TAB>end，取 day 在期成分。"""
    try:
        import sys
        sys.path.insert(0, str(QUANT / "ops"))
        from ensure_qlib_data import resolve_provider_uri
        provider = Path(resolve_provider_uri(None, ensure=False))
    except Exception:  # noqa: BLE001
        provider = None
    candidates = [
        provider / "instruments" / "csi500.txt" if provider else None,
        QUANT.parent / "datasets" / "qlib_data" / "cn_data" / "instruments" / "csi500.txt",
    ]
    path = next((p for p in candidates if p and p.exists()), None)
    if not path:
        return None
    ref = day or "9999-12-31"
    out = []
    for line in path.read_text().splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 3:
            continue
        inst = _normalize(parts[0])
        if inst and parts[1] <= ref and (parts[2] >= ref or not parts[2]):
            out.append(inst)
    return sorted(set(out)) or None


def load_pool(day: str | None = None, exclude_st: bool = True) -> list[dict]:
    """加载池子并附加名称/行业。day 用于 csi500 兜底口径（默认最新在期）。"""
    custom = _from_custom_csv(QUANT / "data" / "meta" / "board_pool.csv")
    instruments = custom or _from_signals() or _from_csi500_file(day) or []
    source = ("board_pool.csv" if custom else
              "signals" if _from_signals else "csi500.txt")
    meta = _load_meta()
    pool = []
    for inst in instruments:
        m = meta.get(inst, {})
        name = m.get("name", "")
        if exclude_st and "ST" in name.upper():
            continue
        pool.append({
            "instrument": inst,
            "name": name,
            "industry": m.get("industry") or "未知",
        })
    log.info("market_board 池子: %d 只 (source=%s, exclude_st=%s)",
             len(pool), source, exclude_st)
    return pool


def pool_snapshot(pool: list[dict], source_note: str = "") -> dict:
    return {
        "size": len(pool),
        "source": source_note,
        "instruments": pool,
    }
