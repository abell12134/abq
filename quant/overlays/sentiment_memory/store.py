"""本地舆情向量库与报告持久化。

布局（均在 data/overlays/sentiment_memory/）：
  raw/YYYY-MM-DD/{source}.jsonl     原始条目（按日追加，幂等去重）
  vectors/{instrument}.json         向量索引（hashing 嵌入 + 元数据）
  reports/{instrument}/{date}.json  个股分析报告
  catalog.json                      标的目录（最近报告、条数等）
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

QUANT = Path(__file__).resolve().parents[2]
ROOT = QUANT / "data" / "overlays" / "sentiment_memory"
TZ = ZoneInfo("Asia/Shanghai")
EMBED_DIM = 256


def ensure_dirs() -> Path:
    for sub in ("raw", "vectors", "reports"):
        (ROOT / sub).mkdir(parents=True, exist_ok=True)
    return ROOT


def _today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


# ---------------- hashing embedding（无额外模型依赖） ----------------


def _stable_hash(token: str) -> int:
    """跨进程稳定的 token hash（避免 PYTHONHASHSEED 随机化）。"""
    import hashlib
    return int(hashlib.md5(token.encode("utf-8", "ignore")).hexdigest()[:8], 16)


def _tokenize(tokens: list[str], dim: int = EMBED_DIM) -> list[float]:
    vec = [0.0] * dim
    if not tokens:
        return vec
    for t in tokens:
        h = _stable_hash(t)
        idx = h % dim
        sign = 1.0 if (h & 1) == 0 else -1.0
        vec[idx] += sign
    # L2 normalize
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def embed_text(text: str, dim: int = EMBED_DIM) -> list[float]:
    """字符 bigram + 词片 hashing，足够做同票内相似检索。"""
    text = (text or "").lower().strip()
    text = re.sub(r"\s+", " ", text)
    tokens: list[str] = []
    # CJK 字符 bigram
    chars = [c for c in text if "\u4e00" <= c <= "\u9fff" or c.isalnum()]
    tokens.extend(chars)
    tokens.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    # 空白分词
    for w in re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}", text):
        tokens.append(w)
    return _tokenize(tokens, dim)


def cosine(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    return sum(a[i] * b[i] for i in range(n))


# ---------------- raw JSONL ----------------


def append_raw(items: list[dict[str, Any]], day: str | None = None) -> int:
    """按 source 写入当日 jsonl，跳过已存在 id。返回新增条数。"""
    ensure_dirs()
    day = day or _today()
    by_src: dict[str, list[dict]] = {}
    for it in items:
        by_src.setdefault(str(it.get("source") or "unknown"), []).append(it)
    added = 0
    for src, rows in by_src.items():
        path = ROOT / "raw" / day / f"{src}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: set[str] = set()
        if path.exists():
            for ln in path.read_text().splitlines():
                try:
                    existing.add(json.loads(ln).get("id", ""))
                except json.JSONDecodeError:
                    continue
        with path.open("a", encoding="utf-8") as fh:
            for it in rows:
                iid = it.get("id")
                if not iid or iid in existing:
                    continue
                fh.write(json.dumps(it, ensure_ascii=False) + "\n")
                existing.add(iid)
                added += 1
    return added


def load_raw(lookback_days: int = 90,
             instrument: str | None = None) -> list[dict[str, Any]]:
    ensure_dirs()
    raw_root = ROOT / "raw"
    if not raw_root.exists():
        return []
    days = sorted(p.name for p in raw_root.iterdir() if p.is_dir())[-lookback_days:]
    out: list[dict[str, Any]] = []
    inst = instrument.upper() if instrument else None
    for day in days:
        for f in (raw_root / day).glob("*.jsonl"):
            for ln in f.read_text().splitlines():
                try:
                    obj = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if inst and obj.get("instrument") and obj["instrument"] != inst:
                    # 无 instrument 的全局电报仍可能相关，留给调用方再滤
                    if obj.get("instrument"):
                        continue
                out.append(obj)
    return out


# ---------------- vector index per instrument ----------------


def _vec_path(instrument: str) -> Path:
    return ROOT / "vectors" / f"{instrument.upper()}.json"


def upsert_vectors(instrument: str, items: list[dict[str, Any]]) -> int:
    """把条目嵌入并写入个股向量索引。返回新增条数。"""
    ensure_dirs()
    path = _vec_path(instrument)
    data = {"instrument": instrument.upper(), "dim": EMBED_DIM, "items": []}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    by_id = {it["id"]: it for it in data.get("items", []) if "id" in it}
    added = 0
    for it in items:
        iid = it.get("id")
        if not iid or iid in by_id:
            continue
        text = f"{it.get('title', '')} {it.get('content', '')}"
        by_id[iid] = {
            "id": iid,
            "source": it.get("source"),
            "title": it.get("title"),
            "published": it.get("published"),
            "url": it.get("url"),
            "snippet": (it.get("content") or "")[:160],
            "embedding": embed_text(text),
        }
        added += 1
    data["items"] = list(by_id.values())
    data["updated_at"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(json.dumps(data, ensure_ascii=False))
    return added


def search_memory(instrument: str, query: str, top_k: int = 8) -> list[dict[str, Any]]:
    path = _vec_path(instrument)
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    q = embed_text(query)
    scored = []
    for it in data.get("items", []):
        emb = it.get("embedding") or []
        scored.append((cosine(q, emb), it))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, it in scored[:top_k]:
        row = {k: v for k, v in it.items() if k != "embedding"}
        row["score"] = round(float(score), 4)
        out.append(row)
    return out


def vector_stats(instrument: str) -> dict[str, Any]:
    path = _vec_path(instrument)
    if not path.exists():
        return {"instrument": instrument, "count": 0}
    data = json.loads(path.read_text())
    return {
        "instrument": instrument.upper(),
        "count": len(data.get("items", [])),
        "updated_at": data.get("updated_at"),
    }


# ---------------- reports & catalog ----------------


def save_report(instrument: str, day: str, report: dict[str, Any]) -> Path:
    ensure_dirs()
    d = ROOT / "reports" / instrument.upper()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{day}.json"
    payload = dict(report)
    payload.setdefault("instrument", instrument.upper())
    payload.setdefault("date", day)
    payload["saved_at"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    _update_catalog(instrument, day, payload)
    return path


def load_report(instrument: str, day: str | None = None) -> dict[str, Any] | None:
    d = ROOT / "reports" / instrument.upper()
    if not d.exists():
        return None
    if day:
        p = d / f"{day}.json"
        return json.loads(p.read_text()) if p.exists() else None
    files = sorted(d.glob("????-??-??.json"), reverse=True)
    return json.loads(files[0].read_text()) if files else None


def list_reports(instrument: str, limit: int = 30) -> list[dict[str, Any]]:
    d = ROOT / "reports" / instrument.upper()
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("????-??-??.json"), reverse=True)[:limit]:
        try:
            obj = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        out.append({
            "date": obj.get("date") or p.stem,
            "sentiment": obj.get("sentiment"),
            "score": obj.get("score"),
            "headline": obj.get("headline"),
            "risk_tags": obj.get("risk_tags") or [],
        })
    return out


def _update_catalog(instrument: str, day: str, report: dict[str, Any]) -> None:
    ensure_dirs()
    path = ROOT / "catalog.json"
    cat: dict[str, Any] = {"instruments": {}}
    if path.exists():
        try:
            cat = json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    inst = instrument.upper()
    entry = cat.setdefault("instruments", {}).setdefault(inst, {})
    entry.update({
        "instrument": inst,
        "name": report.get("name") or entry.get("name", ""),
        "latest_date": day,
        "sentiment": report.get("sentiment"),
        "score": report.get("score"),
        "headline": report.get("headline"),
        "risk_tags": report.get("risk_tags") or [],
        "news_count": report.get("news_count"),
        "vector_count": vector_stats(inst).get("count"),
        "updated_at": report.get("saved_at"),
    })
    cat["updated_at"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(json.dumps(cat, ensure_ascii=False, indent=2))


def load_catalog() -> dict[str, Any]:
    ensure_dirs()
    path = ROOT / "catalog.json"
    if not path.exists():
        return {"instruments": {}, "updated_at": None}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"instruments": {}, "updated_at": None}


def list_tracked_instruments() -> list[str]:
    cat = load_catalog()
    return sorted(cat.get("instruments", {}).keys())
