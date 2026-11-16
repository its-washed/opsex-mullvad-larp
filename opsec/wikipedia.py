"""Wikipedia geosearch: an independent check that a named place is really there.

The coordinates come from Wikimedia's own geotagged articles, so an article for
the candidate name within a few kilometres is evidence from a second dataset.
"""

from __future__ import annotations

import urllib.parse

from . import web

API_URL = "https://en.wikipedia.org/w/api.php"


def nearby(lat: float, lon: float, *, radius: int = 10_000, limit: int = 10, timeout: float = 6.0) -> list[dict]:
    """Geotagged articles within ``radius`` metres. Empty list on any failure."""
    params = {
        "action": "query",
        "list": "geosearch",
        "gscoord": f"{lat}|{lon}",
        "gsradius": str(radius),
        "gslimit": str(limit),
        "format": "json",
    }
    payload = web.fetch_json(f"{API_URL}?{urllib.parse.urlencode(params)}", timeout)
    if not isinstance(payload, dict):
        return []
    results = (payload.get("query") or {}).get("geosearch") or []
    articles = []
    for item in results:
        title = item.get("title")
        if not isinstance(title, str) or not title:
            continue
        articles.append({"title": title, "dist_m": item.get("dist")})
    return articles
