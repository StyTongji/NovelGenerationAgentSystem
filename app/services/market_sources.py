"""Bounded public metadata collection; no login, chapters or anti-bot bypass.

The SSR approach and sample interchange were informed by fanqie-radar (MIT).
Its small display-font decoder is vendored with license; no reference checkout
is needed at runtime.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import re
from datetime import datetime
from urllib.request import getproxies, proxy_bypass
from zoneinfo import ZoneInfo

import httpx

from app.schemas.book_launch import MarketBook, MarketSample
from app.services.fanqie_text import decode_text

CATEGORIES = {258: "传统玄幻", 257: "玄幻脑洞", 1140: "东方仙侠", 1141: "西方奇幻"}


def import_sample(text: str, format: str = "json") -> MarketSample:
    """Native schema, portable CSV, or previously collected radar snapshot."""
    if len(text.encode()) > 1_000_000:
        raise ValueError("sample exceeds 1 MB; import up to 60 observations")
    if format == "csv":
        rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
        # CSV has the same names as MarketBook; empty optional numeric fields are null.
        return MarketSample(books=[MarketBook.model_validate({
            **row, "rank": row.get("rank") or None,
            "metric_value": row.get("metric_value") or None,
        }) for row in rows], coverage="用户导入 CSV；覆盖范围由所提供记录限定")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("JSON sample must be an object with books or categories")
    if "books" in data:
        return MarketSample.model_validate(data)
    # Compatibility with the existing bounded public-rank snapshots.
    books = []
    for group in data.get("categories", []):
        for b in group["books"]:
            url = b.get("source_url") or group["source_url"]
            date = b.get("observed_at") or data["observed_at"]
            identity = f"{url}:{date}:{b.get('bookid', b['title'])}"
            books.append(MarketBook(
                id=hashlib.sha256(identity.encode()).hexdigest()[:20], platform="番茄",
                title=b["title"], author=b.get("author", ""), category=group["name"],
                synopsis=b.get("intro", ""), source_url=url, observed_at=date,
                metric_name="readCount（平台页面字段，口径未核实）", metric_value=b.get("reads"),
                rank=b.get("source_rank"), rank_type=data.get("rank_type", ""),
            ))
    return MarketSample(books=books, coverage=str(data.get("coverage", "导入的番茄榜单小样本")))


def parse_fanqie(html: str, *, url: str, category: str, rank_type: str,
                 observed_at: datetime, limit: int) -> list[MarketBook]:
    match = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html)
    if not match:
        raise ValueError("番茄公开页面没有可解析 SSR 数据；请改用 JSON/CSV 导入")
    state, _ = json.JSONDecoder().raw_decode(html[match.end():].lstrip())
    rows = state.get("rank", {}).get("book_list", [])
    if not rows:
        raise ValueError("番茄页面没有 book_list；未伪造替代数据")
    books = []
    for row in rows[:limit]:
        title = row.get("bookName") or row.get("book_name")
        if not title:
            raise ValueError("番茄页面字段变化：缺少标题")
        identity = f"{url}:{observed_at.isoformat()}:{row.get('bookId', title)}"
        # Do not infer an official rank from the order of our truncated sample.
        rank = row.get("currentPos")
        rank = int(rank) if str(rank).isdigit() and int(rank) > 0 else None
        reads = row.get("readCount", row.get("read_count"))
        books.append(MarketBook(
            id=hashlib.sha256(identity.encode()).hexdigest()[:20], platform="番茄",
            title=decode_text(title), author=decode_text(row.get("author", "")), category=category,
            synopsis=decode_text(row.get("abstract", ""))[:4000], source_url=url, observed_at=observed_at,
            metric_name="readCount（平台页面字段，口径未核实）",
            metric_value=decode_text(str(reads)) if reads is not None else None, rank=rank, rank_type=rank_type,
        ))
    return books


async def collect_fanqie(categories: list[int], rank_type: int = 2, limit: int = 10) -> MarketSample:
    if not 1 <= len(categories) <= 2 or len(set(categories)) != len(categories) or set(categories) - CATEGORIES.keys():
        raise ValueError("请选择 1–2 个支持的番茄玄幻分类")
    if rank_type not in {1, 2} or not 1 <= limit <= 10:
        raise ValueError("rank_type=1 新书 / 2 阅读，每类最多 10 条")
    books = []
    # Some desktop proxy clients export socks://, while HTTPX accepts
    # socks5://. Resolve the HTTPS proxy explicitly and respect NO_PROXY;
    # otherwise HTTPX tries to parse every environment proxy at construction.
    proxies = getproxies()
    proxy = None if proxy_bypass("fanqienovel.com") else proxies.get("https") or proxies.get("all")
    if proxy and proxy.lower().startswith("socks://"):
        proxy = "socks5://" + proxy[len("socks://"):]
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, proxy=proxy, trust_env=False) as client:
        for index, category in enumerate(categories):
            if index:
                await asyncio.sleep(1)
            url = f"https://fanqienovel.com/rank/1_{rank_type}_{category}"
            response = await client.get(url, headers={"User-Agent": "NovelEngine-MarketSample/1.0"})
            if response.status_code != 200:
                raise ValueError(f"公开榜单返回 HTTP {response.status_code}；请手动导入，不绕过访问限制")
            books.extend(parse_fanqie(response.text, url=url, category=CATEGORIES[category],
                rank_type="男频新书榜" if rank_type == 1 else "男频阅读榜",
                observed_at=datetime.now(ZoneInfo("Asia/Shanghai")), limit=limit))
    return MarketSample(books=books, coverage=f"番茄男频，{len(categories)} 个分类，各最多 {limit} 条，单次公开页面样本；未采集起点")
