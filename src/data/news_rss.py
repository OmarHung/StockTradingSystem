"""免費新聞標題來源（政策題材偵察的輸入）。

三個來源皆為平台公開提供的程式化介面（RSS/JSON），非爬網頁，無封鎖疑慮：
- Google News RSS：支援中文關鍵字搜尋（偵察關鍵字可在 settings.yaml 調整）
- Yahoo 奇摩財經 RSS：財經頭條快照
- 鉅亨網 newslist API：台股新聞清單（非官方文件化介面，失效時自動略過）

單一來源失敗不影響其他來源；全部失敗回空清單（scout 會退回 web search 備援）。
"""
from __future__ import annotations

import datetime as dt
import email.utils
import html
import xml.etree.ElementTree as ET

import requests

from src.logging_setup import get_logger

log = get_logger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (StockTradingSystem news scout)"}
_TIMEOUT = 15


def fetch_policy_headlines(keywords: list[str], days: int = 5, limit: int = 80) -> list[dict]:
    """彙整三來源的近 N 日新聞標題，去重、新到舊排序、截取上限。

    回傳 [{date, title, source, url}]（date 為 YYYY-MM-DD）。
    """
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    items: list[dict] = []
    for kw in keywords:
        items += _safe(_google_news, kw)
    items += _safe(_yahoo_finance)
    items += _safe(_cnyes)

    seen: set[str] = set()
    out: list[dict] = []
    for it in sorted(items, key=lambda x: x["date"], reverse=True):
        key = it["title"].strip()
        if not key or key in seen or it["date"] < cutoff:
            continue
        seen.add(key)
        out.append(it)
        if len(out) >= limit:
            break
    return out


def _safe(fn, *args) -> list[dict]:
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 — 單一來源失敗不中斷
        log.warning("新聞來源 %s 失敗：%s", fn.__name__, e)
        return []


def _google_news(query: str, per_kw: int = 15) -> list[dict]:
    r = requests.get(
        "https://news.google.com/rss/search",
        params={"q": query, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"},
        headers=_UA, timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return _parse_rss(r.text, default_source="GoogleNews")[:per_kw]


def _yahoo_finance(cap: int = 20) -> list[dict]:
    r = requests.get("https://tw.news.yahoo.com/rss/finance", headers=_UA, timeout=_TIMEOUT)
    r.raise_for_status()
    return _parse_rss(r.text, default_source="Yahoo財經")[:cap]


def _cnyes(cap: int = 30) -> list[dict]:
    r = requests.get(
        "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock",
        params={"limit": cap}, headers={**_UA, "accept": "application/json"},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for it in (r.json().get("items", {}).get("data") or []):
        ts = it.get("publishAt")
        date = dt.date.fromtimestamp(ts).isoformat() if ts else ""
        title = html.unescape(it.get("title") or "")
        if title and date:
            out.append({"date": date, "title": title, "source": "鉅亨網",
                        "url": f"https://news.cnyes.com/news/id/{it.get('newsId', '')}"})
    return out


def _parse_rss(xml_text: str, default_source: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        pub = item.findtext("pubDate") or ""
        src = (item.findtext("source") or default_source).strip()
        url = (item.findtext("link") or "").strip()
        try:
            date = email.utils.parsedate_to_datetime(pub).date().isoformat()
        except (TypeError, ValueError):
            continue
        if title:
            out.append({"date": date, "title": html.unescape(title), "source": src, "url": url})
    return out
