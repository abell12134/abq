"""多源舆情采集：东方财富 JSONP、财联社、新浪财经。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Shanghai")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _now() -> datetime:
    return datetime.now(TZ)


def item_id(source: str, title: str, published: str) -> str:
    raw = f"{source}|{published}|{title}".encode("utf-8", "ignore")
    return hashlib.sha1(raw).hexdigest()[:16]


def _normalize(item: dict[str, Any]) -> dict[str, Any]:
    title = str(item.get("title") or "").strip()
    content = str(item.get("content") or "").strip()
    published = str(item.get("published") or "").strip()
    source = str(item.get("source") or "").strip()
    url = str(item.get("url") or "").strip()
    instrument = str(item.get("instrument") or "").strip().upper()
    return {
        "id": item.get("id") or item_id(source, title or content[:40], published),
        "source": source,
        "instrument": instrument,
        "title": title,
        "content": content,
        "published": published,
        "url": url,
        "fetched_at": item.get("fetched_at") or _now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _http_json(url: str, *, timeout: float = 15.0,
               headers: dict[str, str] | None = None) -> Any:
    hdrs = {"User-Agent": UA, "Accept": "application/json,*/*"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def _http_text(url: str, *, timeout: float = 15.0,
               headers: dict[str, str] | None = None) -> str:
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def _parse_dt(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def _within(published: str, lookback_days: int) -> bool:
    dt = _parse_dt(published)
    if dt is None:
        return True  # 无法解析时保留，交由下游过滤
    return dt >= _now() - timedelta(days=lookback_days)


# --------------------------------------------------------------------------- #
# 东方财富：个股新闻 JSONP 直连（可翻页，覆盖 30–90 日）
# --------------------------------------------------------------------------- #


def fetch_eastmoney_stock(
    instrument: str,
    lookback_days: int = 90,
    max_pages: int = 8,
    page_size: int = 20,
) -> list[dict[str, Any]]:
    """东方财富搜索 API（JSONP）按代码/简称检索个股新闻。"""
    code = instrument[2:] if len(instrument) > 2 and instrument[:2].isalpha() else instrument
    out: list[dict[str, Any]] = []
    cutoff_hit = False
    for page in range(1, max_pages + 1):
        param = json.dumps({
            "uid": "",
            "keyword": code,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": page,
                    "pageSize": page_size,
                    "preTag": "",
                    "postTag": "",
                }
            },
        }, ensure_ascii=False)
        url = ("https://search-api-web.eastmoney.com/search/jsonp"
               f"?cb=jQuery&param={urllib.parse.quote(param)}")
        try:
            text = _http_text(
                url, timeout=15.0,
                headers={"Referer": "https://so.eastmoney.com/"},
            )
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.warning("eastmoney JSONP 失败 page=%s: %s", page, e)
            break
        m = re.search(r"jQuery\((.*)\)\s*$", text, re.S)
        if not m:
            break
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            break
        rows = (data.get("result") or {}).get("cmsArticleWebOld") or []
        if not rows:
            break
        for row in rows:
            title = re.sub(r"</?em>", "", str(row.get("title") or ""))
            content = re.sub(r"</?em>", "", str(row.get("content") or ""))
            published = str(row.get("date") or "")
            if not _within(published, lookback_days):
                cutoff_hit = True
                continue
            url_row = str(row.get("url") or row.get("uniqueUrl") or "")
            if url_row and not url_row.startswith("http"):
                url_row = "https://finance.eastmoney.com/a/" + url_row.lstrip("/")
            out.append(_normalize({
                "source": "eastmoney",
                "instrument": instrument.upper(),
                "title": title,
                "content": content,
                "published": published,
                "url": url_row,
            }))
        if cutoff_hit:
            break
        time.sleep(0.15)
    # 去重保序
    seen: set[str] = set()
    uniq = []
    for it in out:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        uniq.append(it)
    return uniq


# --------------------------------------------------------------------------- #
# 财联社 / 新浪：7x24 电报（近期滚动；靠本地库累积长期记忆）
# --------------------------------------------------------------------------- #


def fetch_cls(lookback_days: int = 7, timeout: float = 8.0) -> list[dict[str, Any]]:
    """财联社电报。先 HTTP 备用，再短超时试 AKShare（其内部重试极慢）。"""
    items: list[dict[str, Any]] = []
    try:
        items = _fetch_cls_http(timeout=timeout)
    except Exception as e:  # noqa: BLE001
        log.info("CLS HTTP 不可用: %s", e)
    if not items:
        try:
            items = _fetch_cls_akshare(timeout=min(timeout, 6.0))
        except Exception as e:  # noqa: BLE001
            log.warning("AKShare CLS 失败: %s", e or type(e).__name__)
    return [it for it in items if _within(it["published"], lookback_days)]


def _fetch_cls_akshare(timeout: float = 6.0) -> list[dict[str, Any]]:
    import concurrent.futures

    def _call():
        import akshare as ak
        return ak.stock_info_global_cls(symbol="全部")

    # wait=False：超时后不要堵在 executor 关闭上
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_call)
        df = fut.result(timeout=timeout)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for _, row in df.iterrows():
        date_s = str(row.get("发布日期", "")).strip()
        time_s = str(row.get("发布时间", "")).strip()
        published = f"{date_s} {time_s}".strip()
        title = str(row.get("标题") or "").strip()
        content = str(row.get("内容") or "").strip()
        out.append(_normalize({
            "source": "cls",
            "title": title or content[:40],
            "content": content or title,
            "published": published,
            "url": "https://www.cls.cn/telegraph",
        }))
    return out


def _fetch_cls_http(timeout: float = 12.0) -> list[dict[str, Any]]:
    """备用：api3.cls.cn（无签名时可能返回加载中，尽力解析）。"""
    url = "https://api3.cls.cn/nodeapi/telegraphList?app=CailianpressWeb&os=web&sv=8.4.6"
    data = _http_json(
        url, timeout=timeout,
        headers={"Referer": "https://www.cls.cn/telegraph"},
    )
    rows = ((data.get("data") or {}).get("roll_data")) or []
    out = []
    for row in rows:
        ctime = row.get("ctime")
        if isinstance(ctime, (int, float)):
            published = datetime.fromtimestamp(ctime, tz=TZ).strftime("%Y-%m-%d %H:%M:%S")
        else:
            published = str(ctime or "")
        title = str(row.get("title") or "").strip()
        content = str(row.get("content") or "").strip()
        out.append(_normalize({
            "source": "cls",
            "title": title or content[:40],
            "content": content or title,
            "published": published,
            "url": "https://www.cls.cn/telegraph",
        }))
    return out


def fetch_sina(lookback_days: int = 7, pages: int = 5,
               timeout: float = 12.0) -> list[dict[str, Any]]:
    """新浪 7x24 快讯（直连翻页；AKShare 仅作回退）。"""
    out: list[dict[str, Any]] = []
    try:
        out.extend(_fetch_sina_http(pages=pages, timeout=timeout))
    except Exception as e:  # noqa: BLE001
        log.warning("Sina HTTP 失败: %s", e)
    if not out:
        try:
            out.extend(_fetch_sina_akshare(timeout=timeout))
        except Exception as e:  # noqa: BLE001
            log.info("AKShare Sina 不可用: %s", e)
    filtered = [it for it in out if _within(it["published"], lookback_days)]
    seen: set[str] = set()
    uniq = []
    for it in filtered:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        uniq.append(it)
    return uniq


def _fetch_sina_akshare(timeout: float = 12.0) -> list[dict[str, Any]]:
    import concurrent.futures

    def _call():
        import akshare as ak
        return ak.stock_info_global_sina()

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_call)
        df = fut.result(timeout=timeout)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for _, row in df.iterrows():
        published = str(row.get("时间") or "").strip()
        content = str(row.get("内容") or "").strip()
        out.append(_normalize({
            "source": "sina",
            "title": content[:48],
            "content": content,
            "published": published,
            "url": "https://finance.sina.com.cn/7x24/",
        }))
    return out


def _fetch_sina_http(pages: int = 5, timeout: float = 12.0) -> list[dict[str, Any]]:
    out = []
    for page in range(1, pages + 1):
        params = urllib.parse.urlencode({
            "page": str(page),
            "page_size": "20",
            "zhibo_id": "152",
            "tag_id": "0",
            "dire": "f",
            "dpc": "1",
            "pagesize": "20",
            "type": "1",
        })
        url = f"https://zhibo.sina.com.cn/api/zhibo/feed?{params}"
        data = _http_json(url, timeout=timeout)
        rows = (((data.get("result") or {}).get("data") or {})
                .get("feed") or {}).get("list") or []
        if not rows:
            break
        for row in rows:
            content = str(row.get("rich_text") or "").strip()
            published = str(row.get("create_time") or "").strip()
            out.append(_normalize({
                "source": "sina",
                "title": content[:48],
                "content": content,
                "published": published,
                "url": "https://finance.sina.com.cn/7x24/",
            }))
        time.sleep(0.1)
    return out


# --------------------------------------------------------------------------- #
# 标的过滤 + 汇总采集
# --------------------------------------------------------------------------- #


def filter_for_instrument(
    items: list[dict[str, Any]],
    instrument: str,
    name: str = "",
) -> list[dict[str, Any]]:
    """全局电报按代码/简称关键字过滤；已带 instrument 的东财新闻直接保留。"""
    code = instrument[2:] if instrument[:2].isalpha() else instrument
    inst = instrument.upper()
    keys = [code, inst]
    if name:
        keys.append(name)
        # 去掉常见后缀便于匹配简称
        for suf in ("股份", "集团", "科技", "有限", "公司", "A", "B"):
            if name.endswith(suf) and len(name) > len(suf) + 1:
                keys.append(name[: -len(suf)])
    keys = [k for k in keys if k]
    out = []
    for it in items:
        if it.get("instrument") == inst:
            out.append(it)
            continue
        blob = f"{it.get('title', '')} {it.get('content', '')}"
        if any(k and k in blob for k in keys):
            tagged = dict(it)
            tagged["instrument"] = inst
            out.append(tagged)
    return out


def collect_for_instrument(
    instrument: str,
    name: str = "",
    lookback_days: int = 90,
    global_cache: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """采集单票相关舆情：东财历史 + 财联社/新浪近期过滤。"""
    cache = global_cache if global_cache is not None else {}
    em = fetch_eastmoney_stock(instrument, lookback_days=lookback_days)
    if "cls" not in cache:
        cache["cls"] = fetch_cls(lookback_days=min(lookback_days, 14))
    if "sina" not in cache:
        cache["sina"] = fetch_sina(lookback_days=min(lookback_days, 14))
    cls_hit = filter_for_instrument(cache["cls"], instrument, name)
    sina_hit = filter_for_instrument(cache["sina"], instrument, name)
    merged = em + cls_hit + sina_hit
    seen: set[str] = set()
    out = []
    for it in merged:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        out.append(it)
    out.sort(key=lambda x: str(x.get("published", "")), reverse=True)
    return out


def collect_global_feeds(lookback_days: int = 7) -> list[dict[str, Any]]:
    """只拉全局电报（用于定时入库，不绑定个股）。"""
    items = fetch_cls(lookback_days=lookback_days) + fetch_sina(lookback_days=lookback_days)
    seen: set[str] = set()
    out = []
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        out.append(it)
    return out
