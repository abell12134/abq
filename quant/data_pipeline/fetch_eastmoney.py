"""东方财富新闻数据抓取模块。

从东方财富网抓取股票相关新闻和公告，用于情绪分析。

数据源:
1. 个股新闻: https://guba.eastmoney.com/
2. 公告数据: https://data.eastmoney.com/notices/
3. 行业新闻: https://finance.eastmoney.com/

注意: 请遵守东方财富的使用条款，合理使用数据。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
import pandas as pd

# 请求头，模拟浏览器
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 东方财富 API 基础 URL
BASE_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _code_to_secid(code: str) -> str:
    """将股票代码转换为东方财富格式。
    
    SH600519 -> 1.600519
    SZ000858 -> 0.000858
    """
    if code.startswith("SH"):
        return f"1.{code[2:]}"
    elif code.startswith("SZ"):
        return f"0.{code[2:]}"
    return code


def fetch_stock_news(code: str, days: int = 7, limit: int = 20) -> list[dict]:
    """抓取个股新闻。
    
    Args:
        code: 股票代码 (SH600519 格式)
        days: 抓取最近几天的新闻
        limit: 最多抓取条数
        
    Returns:
        新闻列表: [{"title": ..., "content": ..., "date": ..., "source": ...}]
    """
    secid = _code_to_secid(code)
    
    # 东方财富个股新闻 API
    url = "https://search-api-web.eastmoney.com/search/jsonp"
    
    params = {
        "cb": "jQuery_callback",
        "param": json.dumps({
            "uid": "",
            "keyword": code[2:],
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": limit,
                    "preTag": "",
                    "postTag": ""
                }
            }
        }),
        "_": int(time.time() * 1000)
    }
    
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        text = resp.text
        
        # 解析 JSONP
        match = re.search(r'jQuery_callback\((.*)\)', text, re.S)
        if match:
            data = json.loads(match.group(1))
            
            news_list = []
            articles = data.get("result", {}).get("cmsArticleWebOld", [])
            
            for article in articles:
                title = article.get("title", "")
                # 清理 HTML 标签
                title = re.sub(r'<[^>]+>', '', title)
                
                content = article.get("content", "")
                content = re.sub(r'<[^>]+>', '', content)[:500]  # 截取前500字
                
                pub_date = article.get("date", "")
                
                news_list.append({
                    "title": title,
                    "content": content,
                    "date": pub_date,
                    "source": article.get("mediaName", "东方财富"),
                    "url": article.get("url", ""),
                })
            
            return news_list
            
    except Exception as e:
        print(f"抓取 {code} 新闻失败: {e}")
    
    return []


def fetch_stock_announcements(code: str, days: int = 30, limit: int = 10) -> list[dict]:
    """抓取个股公告。"""
    stock_code = code[2:]  # 去掉 SH/SZ 前缀
    
    # 东方财富公告 API
    url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
    
    params = {
        "sr": "-1",
        "page_size": limit,
        "page_index": 1,
        "ann_type": "SHA,SZA",
        "client_source": "web",
        "stock_list": stock_code,
        "f_node": "0",
        "s_node": "0",
    }
    
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        data = resp.json()
        
        announcements = []
        for item in data.get("data", {}).get("list", []):
            announcements.append({
                "title": item.get("title", ""),
                "date": item.get("notice_date", "")[:10],
                "type": item.get("columns", [{}])[0].get("column_name", ""),
                "url": f"https://data.eastmoney.com/notices/detail/{stock_code}/{item.get('art_code', '')}.html",
            })
        
        return announcements
        
    except Exception as e:
        print(f"抓取 {code} 公告失败: {e}")
    
    return []


def fetch_batch_news(codes: list[str], days: int = 3) -> dict[str, list[str]]:
    """批量抓取股票新闻。
    
    Args:
        codes: 股票代码列表
        days: 抓取最近几天
        
    Returns:
        {code: [news_text1, news_text2, ...]}
    """
    result = {}
    
    for i, code in enumerate(codes):
        print(f"  抓取 {code} ({i+1}/{len(codes)})...")
        
        # 抓取新闻
        news_list = fetch_stock_news(code, days=days, limit=10)
        
        # 抓取公告
        announcements = fetch_stock_announcements(code, days=days, limit=5)
        
        # 合并为文本列表
        texts = []
        for news in news_list:
            text = f"{news['title']}。{news.get('content', '')[:100]}"
            texts.append(text)
        
        for ann in announcements:
            text = f"公告: {ann['title']}"
            texts.append(text)
        
        result[code] = texts
        
        # 避免请求过快
        time.sleep(0.5)
    
    return result


def save_news_data(news_dict: dict[str, list[str]], date: str = None):
    """保存新闻数据到文件。"""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    
    data_dir = Path.home() / "abq/quant/data/news"
    data_dir.mkdir(parents=True, exist_ok=True)
    
    filepath = data_dir / f"news_{date}.json"
    
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(news_dict, f, ensure_ascii=False, indent=2)
    
    print(f"新闻数据已保存: {filepath}")
    return filepath


# ============================================================
# 测试函数
# ============================================================
def test_fetch():
    """测试抓取功能。"""
    print("=" * 60)
    print("东方财富新闻数据抓取测试")
    print("=" * 60)
    
    # 测试股票
    test_codes = ["SH600519", "SZ000858", "SH601318"]
    
    for code in test_codes:
        print(f"\n【{code}】")
        
        # 抓取新闻
        news = fetch_stock_news(code, days=3, limit=3)
        print(f"  新闻 ({len(news)} 条):")
        for n in news[:2]:
            print(f"    - {n['title'][:50]}...")
        
        # 抓取公告
        announcements = fetch_stock_announcements(code, days=30, limit=3)
        print(f"  公告 ({len(announcements)} 条):")
        for a in announcements[:2]:
            print(f"    - [{a['date']}] {a['title'][:50]}...")
        
        time.sleep(1)


if __name__ == "__main__":
    test_fetch()
