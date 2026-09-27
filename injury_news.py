"""
Recent injury news for a player, pulled from Google News RSS search.

No API key needed. Results are cached in memory for 30 minutes per query so
hovering the injury list doesn't hammer the feed.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

FEED_URL = "https://news.google.com/rss/search"
CACHE_TTL = 30 * 60
_cache: dict[str, tuple[float, list[dict]]] = {}


def player_injury_news(name: str, limit: int = 6, days: int = 14) -> list[dict]:
    """List of {title, source, source_url, url, published} for
    articles mentioning `name` alongside injury terms in the last `days` days."""
    key = f"{name.lower()}|{days}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1][:limit]

    q = f'"{name}" (injury OR injured OR hurt OR "injury report") when:{days}d'
    resp = requests.get(FEED_URL, params={"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"}, timeout=8)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    items = []
    for it in root.iter("item"):
        src = it.find("source")
        source = src.text if src is not None else ""
        title = it.findtext("title") or ""
        # Google appends " - <source>" to every title.
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        pub = it.findtext("pubDate")
        try:
            published = parsedate_to_datetime(pub).isoformat() if pub else None
        except (TypeError, ValueError):
            published = None
        items.append({
            "title": title,
            "source": source,
            "source_url": src.get("url") if src is not None else None,
            "url": it.findtext("link"),
            "published": published,
        })
    # Headlines naming the player first (team roundups mention everyone), then newest.
    last = name.split()[-1].lower()
    items.sort(key=lambda x: (last in x["title"].lower(), x["published"] or ""), reverse=True)
    _cache[key] = (time.time(), items)
    return items[:limit]
