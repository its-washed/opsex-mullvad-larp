"""Reverse geocoding and name lookup through OpenStreetMap's Nominatim.

Nominatim is contacted only when the caller opts in, because that sends the
image's coordinates (or a place name) to a third party. Otherwise the bundled
GeoNames table provides the nearest mapped place, which is reported as such
rather than as a street address.
"""

from __future__ import annotations

import time
import urllib.parse

from . import gazetteer, web

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"

# Nominatim's usage policy allows at most one request per second.
MIN_INTERVAL = 1.1


_last_request = 0.0


def _throttle(min_interval: float = MIN_INTERVAL) -> None:
    global _last_request
    wait = _last_request + min_interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def reverse(lat: float, lon: float, *, online: bool = False, timeout: float = 6.0):
    """Return a dict describing the coordinate, or None if nothing is known.

    The coordinate leaves the machine only when ``online`` is explicitly set.
    """
    if online:
        resolved = _nominatim(lat, lon, timeout)
        if resolved:
            return resolved
    return _offline(lat, lon)


def search_place(name: str, cc: str = "", *, timeout: float = 6.0):
    """Look a place name up on OpenStreetMap.

    Returns ``{"lat", "lon", "text"}`` for the best match, or None. Used to
    confirm (or contradict) a candidate from the offline datasets.
    """
    params = {"format": "jsonv2", "q": name, "limit": "1"}
    if cc:
        params["countrycodes"] = cc.lower()
    _throttle()
    payload = web.fetch_json(f"{NOMINATIM_SEARCH_URL}?{urllib.parse.urlencode(params)}", timeout)
    if not isinstance(payload, list) or not payload:
        return None
    first = payload[0]
    if not isinstance(first, dict):
        return None
    try:
        match = {"lat": float(first["lat"]), "lon": float(first["lon"])}
    except (KeyError, TypeError, ValueError):
        return None
    match["text"] = first.get("display_name") or name
    return match


def _nominatim(lat: float, lon: float, timeout: float):
    query = urllib.parse.urlencode({
        "format": "jsonv2",
        "lat": f"{lat:.6f}",
        "lon": f"{lon:.6f}",
        "zoom": "16",
        "addressdetails": "1",
    })
    _throttle()
    payload = web.fetch_json(f"{NOMINATIM_URL}?{query}", timeout)
    if not isinstance(payload, dict):
        return None
    label = payload.get("display_name")
    if not isinstance(label, str) or not label:
        return None
    return {"text": label, "source": "OpenStreetMap Nominatim", "distance_km": None}


def _offline(lat: float, lon: float):
    hits = gazetteer.nearest(lat, lon, limit=1)
    if not hits:
        return None
    city, distance = hits[0]
    region = gazetteer.region_name(city.admin1)
    country = gazetteer.country_name(city.cc)
    label = ", ".join(part for part in (city.name, region, country) if part)
    return {
        "text": label,
        "source": "offline GeoNames cities (nearest mapped city)",
        "distance_km": round(distance, 1),
    }
