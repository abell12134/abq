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
# 东方财富：个股新闻 JSONP 直连（可翻页，标准回看近 90 日）
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
# 公司公告 / 财报（东财 + 巨潮）
# --------------------------------------------------------------------------- #

# 定期报告与业绩类优先保留
_REPORT_HINTS = (
    "年度报告", "半年报", "半年度报告", "一季报", "三季报", "季度报告",
    "业绩预告", "业绩快报", "业绩报告", "定期报告", "财务报告",
    "审计报告", "招股说明书", "募集说明书",
)


def fetch_announcements(
    instrument: str,
    lookback_days: int = 90,
    limit: int = 40,
    asof: str | None = None,
) -> list[dict[str, Any]]:
    """个股公告：东财优先，巨潮补齐；突出财报/业绩类。"""
    asof = asof or _now().strftime("%Y-%m-%d")
    items = _fetch_ann_eastmoney(instrument, lookback_days, limit=max(limit, 50), asof=asof)
    if len(items) < 5:
        extra = _fetch_ann_cninfo(instrument, lookback_days, limit=limit, asof=asof)
        seen = {it["id"] for it in items}
        for it in extra:
            if it["id"] not in seen:
                items.append(it)
                seen.add(it["id"])
    # 财报/业绩置顶，其余按时间
    def _rank(it: dict) -> tuple:
        title = it.get("title") or ""
        cat = it.get("category") or ""
        is_report = any(h in title or h in cat for h in _REPORT_HINTS)
        return (0 if is_report else 1, str(it.get("published", "")),)
    items.sort(key=_rank)
    # 稳定：报告类全留，再按时间补到 limit
    reports = [it for it in items if any(
        h in (it.get("title") or "") or h in (it.get("category") or "")
        for h in _REPORT_HINTS)]
    others = [it for it in items if it not in reports]
    others.sort(key=lambda x: str(x.get("published", "")), reverse=True)
    out = reports + others
    seen: set[str] = set()
    uniq = []
    for it in out:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        uniq.append(it)
        if len(uniq) >= limit:
            break
    uniq.sort(key=lambda x: str(x.get("published", "")), reverse=True)
    return uniq


def _fetch_ann_eastmoney(
    instrument: str,
    lookback_days: int,
    limit: int,
    asof: str,
) -> list[dict[str, Any]]:
    code = instrument[2:] if instrument[:2].isalpha() else instrument
    inst = instrument.upper() if instrument[:2].isalpha() else (
        ("SZ" if code[0] in "03" else "SH") + code)
    url = ("https://np-anotice-stock.eastmoney.com/api/security/ann?"
           + urllib.parse.urlencode({
               "sr": -1,
               "page_size": min(max(limit * 2, 40), 100),
               "page_index": 1,
               "ann_type": "A",
               "client_source": "web",
               "stock_list": code,
           }))
    try:
        data = _http_json(
            url, timeout=20.0,
            headers={"Referer": "https://data.eastmoney.com/"},
        )
    except Exception as e:  # noqa: BLE001
        log.warning("eastmoney 公告失败 %s: %s", instrument, e)
        return []
    rows = ((data.get("data") or {}).get("list")) or []
    out = []
    for row in rows:
        title = str(row.get("title") or row.get("title_ch") or "").strip()
        published = str(
            row.get("display_time") or row.get("notice_date")
            or row.get("eiTime") or ""
        ).strip()
        # display_time 偶发带毫秒 :631
        if published.count(":") >= 3:
            published = published.rsplit(":", 1)[0]
        if not _within(published, lookback_days):
            continue
        cats = [c.get("column_name", "") for c in (row.get("columns") or [])
                if isinstance(c, dict) and c.get("column_name")]
        art = str(row.get("art_code") or "").strip()
        url_row = (f"https://data.eastmoney.com/notices/detail/{code}/{art}.html"
                   if art else "")
        kind = "财报公告" if any(h in title or h in ",".join(cats) for h in _REPORT_HINTS) \
            else "公司公告"
        out.append(_normalize({
            "source": "ann_eastmoney",
            "instrument": inst,
            "title": f"[{kind}] {title}" if title else title,
            "content": f"类别: {','.join(cats)}" if cats else kind,
            "published": published[:19],
            "url": url_row,
            "category": ",".join(cats),
        }))
        # 把 category 塞进 normalize 后会丢掉；补回
        out[-1]["category"] = ",".join(cats)
        out[-1]["kind"] = kind
    return out[:limit]


def _fetch_ann_cninfo(
    instrument: str,
    lookback_days: int,
    limit: int,
    asof: str,
) -> list[dict[str, Any]]:
    import concurrent.futures
    from datetime import datetime as _dt

    code = instrument[2:] if instrument[:2].isalpha() else instrument
    inst = instrument.upper() if instrument[:2].isalpha() else (
        ("SZ" if code[0] in "03" else "SH") + code)
    start = (_dt.strptime(asof[:10], "%Y-%m-%d") - timedelta(days=lookback_days)
             ).strftime("%Y%m%d")
    end = asof[:10].replace("-", "")

    def _call():
        import akshare as ak
        return ak.stock_zh_a_disclosure_report_cninfo(
            symbol=code, market="沪深京", start_date=start, end_date=end)

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        df = ex.submit(_call).result(timeout=25.0)
    except Exception as e:  # noqa: BLE001
        log.info("cninfo 公告失败 %s: %s", instrument, e or type(e).__name__)
        return []
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    if df is None or getattr(df, "empty", True):
        return []
    title_col = next((c for c in df.columns if "标题" in str(c)), None)
    date_col = next((c for c in df.columns if "时间" in str(c) or "日期" in str(c)), None)
    url_col = next((c for c in df.columns if "链接" in str(c) or "url" in str(c).lower()), None)
    if not title_col or not date_col:
        return []
    out = []
    for _, row in df.iterrows():
        title = str(row[title_col]).strip()
        published = str(row[date_col]).strip()
        if not _within(published, lookback_days):
            continue
        kind = "财报公告" if any(h in title for h in _REPORT_HINTS) else "公司公告"
        out.append(_normalize({
            "source": "ann_cninfo",
            "instrument": inst,
            "title": f"[{kind}] {title}",
            "content": kind,
            "published": published[:19],
            "url": str(row[url_col]) if url_col else "",
        }))
        out[-1]["category"] = kind
        out[-1]["kind"] = kind
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# 政策 / 宏观快讯（从全局电报 + 东财要闻中筛）
# --------------------------------------------------------------------------- #

POLICY_KEYWORDS = (
    "国务院", "央行", "人民银行", "证监会", "财政部", "发改委", "工信部",
    "降准", "降息", "MLF", "LPR", "再贷款", "财政政策", "货币政策",
    "产业政策", "补贴", "关税", "出口管制", "反垄断", "注册制",
    "印花税", "减税", "退税", "地方债", "专项债", "稳增长", "新质生产力",
    "半导体", "芯片", "新能源", "光伏", "锂电", "房地产", "化债",
    "证监会发布", "央行宣布", "政策", "监管", "窗口指导",
)


def is_policy_item(item: dict[str, Any]) -> bool:
    blob = f"{item.get('title', '')} {item.get('content', '')}"
    return any(k in blob for k in POLICY_KEYWORDS)


def filter_policy(items: list[dict[str, Any]], limit: int = 30) -> list[dict[str, Any]]:
    out = []
    for it in items:
        if not is_policy_item(it):
            continue
        tagged = dict(it)
        tagged["source"] = f"policy_{it.get('source', 'news')}"
        title = tagged.get("title") or ""
        if not title.startswith("[政策]"):
            tagged["title"] = f"[政策] {title}"[:120]
        tagged["kind"] = "政策宏观"
        out.append(_normalize(tagged))
        out[-1]["kind"] = "政策宏观"
        if len(out) >= limit:
            break
    return out


def fetch_eastmoney_global(lookback_days: int = 14, limit: int = 80) -> list[dict[str, Any]]:
    """东财财经要闻（akshare stock_info_global_em），作政策/宏观补充。"""
    import concurrent.futures

    def _call():
        import akshare as ak
        return ak.stock_info_global_em()

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        df = ex.submit(_call).result(timeout=15.0)
    except Exception as e:  # noqa: BLE001
        log.info("eastmoney global 失败: %s", e or type(e).__name__)
        return []
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    if df is None or getattr(df, "empty", True):
        return []
    title_col = "标题" if "标题" in df.columns else df.columns[0]
    summary_col = "摘要" if "摘要" in df.columns else None
    date_col = "发布时间" if "发布时间" in df.columns else None
    url_col = "链接" if "链接" in df.columns else None
    if date_col is None:
        return []
    out = []
    for _, row in df.iterrows():
        published = str(row[date_col]).strip()
        if not _within(published, lookback_days):
            continue
        title = str(row[title_col]).strip()
        content = str(row[summary_col]).strip() if summary_col else ""
        out.append(_normalize({
            "source": "eastmoney_global",
            "title": title,
            "content": content,
            "published": published,
            "url": str(row[url_col]) if url_col else "",
        }))
        if len(out) >= limit:
            break
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
    """采集单票相关材料：公告/财报 + 东财新闻 + 财联社/新浪过滤 + 政策宏观。"""
    cache = global_cache if global_cache is not None else {}
    asof = _now().strftime("%Y-%m-%d")

    anns = fetch_announcements(instrument, lookback_days=lookback_days, asof=asof)
    em = fetch_eastmoney_stock(instrument, lookback_days=lookback_days)

    if "cls" not in cache:
        cache["cls"] = fetch_cls(lookback_days=min(lookback_days, 14))
    if "sina" not in cache:
        cache["sina"] = fetch_sina(lookback_days=min(lookback_days, 14))
    if "em_global" not in cache:
        cache["em_global"] = fetch_eastmoney_global(lookback_days=min(lookback_days, 14))
    if "policy" not in cache:
        cache["policy"] = filter_policy(
            cache["cls"] + cache["sina"] + cache["em_global"], limit=40)

    cls_hit = filter_for_instrument(cache["cls"], instrument, name)
    sina_hit = filter_for_instrument(cache["sina"], instrument, name)
    # 政策：全量近期政策 + 与公司简称/行业关键词重叠的条目（已在 filter_policy）
    policy = list(cache["policy"])

    # 公告优先，再新闻/电报，再政策
    merged = anns + em + cls_hit + sina_hit + policy
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
    """只拉全局电报 + 政策要闻（用于定时入库，不绑定个股）。"""
    items = (fetch_cls(lookback_days=lookback_days)
             + fetch_sina(lookback_days=lookback_days)
             + fetch_eastmoney_global(lookback_days=lookback_days))
    policy = filter_policy(items, limit=50)
    merged = items + policy
    seen: set[str] = set()
    out = []
    for it in merged:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        out.append(it)
    return out
