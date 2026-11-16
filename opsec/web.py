"""One HTTP GET helper shared by the optional online sources.

Coordinates leave the machine only through this module, and only when the
caller asked for an online lookup. Failures are returned as None so the caller
can fall back to offline data instead of aborting the run.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

USER_AGENT = "opsec-image-locator/1.0 (image geolocation CLI)"


def fetch_json(url: str, timeout: float = 6.0):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
