"""Geocoding (Nominatim, cached in SQLite) and haversine distance."""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable

from job_hunter.store import Store

log = logging.getLogger(__name__)

EARTH_RADIUS_MILES = 3958.8
USER_AGENT = "job-hunter/0.1 (self-hosted personal job search; geopy Nominatim)"

Coords = tuple[float, float]


def haversine_miles(a: Coords, b: Coords) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(h))


def clean_location(text: str) -> str:
    """Normalize job-board location text into a stable cache key / query."""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r",\s*(US|USA|United States)$", "", text, flags=re.IGNORECASE)
    return text.lower()


def _nominatim_lookup(query: str) -> Coords | None:
    from geopy.geocoders import Nominatim

    geolocator = Nominatim(user_agent=USER_AGENT, timeout=10)
    loc = geolocator.geocode(query)
    return (loc.latitude, loc.longitude) if loc else None


class Geocoder:
    def __init__(
        self,
        store: Store,
        *,
        lookup: Callable[[str], Coords | None] = _nominatim_lookup,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        min_interval: float = 1.0,
    ) -> None:
        self._store = store
        self._lookup = lookup
        self._sleep = sleep
        self._clock = clock
        self._min_interval = min_interval
        self._last_call: float | None = None

    def geocode(self, location: str) -> Coords | None:
        query = clean_location(location)
        if not query:
            return None
        hit, coords = self._store.geocache_get(query)
        if hit:
            return coords
        if self._last_call is not None:
            wait = self._min_interval - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()
        try:
            coords = self._lookup(query)
        except Exception:
            # Transient failure: don't cache, so a later run retries.
            log.warning("geocoding failed for %r", query, exc_info=True)
            return None
        self._store.geocache_put(query, coords)
        return coords
