"""
Geography helpers: zone matching and reverse geocoding.

Zones come first, addresses second. Inside one large building or site every
tracker reverse-geocodes to the same street address, so the useful answer is
the zone name ("Office", "Warehouse"). The street address is most informative
when a tracker is somewhere it should not be.

Reverse geocoding providers:
  nominatim  free OpenStreetMap service. Policy: max 1 request/s, identify
             the app in the User-Agent, cache results. We do all three.
  google     Google Geocoding API (needs GOOGLE_GEOCODING_KEY with billing on;
             10k free requests a month, we use a few dozen).
  none       skip addresses entirely.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time

from aiohttp import ClientSession, ClientTimeout

from .config import Settings, Zone
from .store import Store

log = logging.getLogger("klepto.geo")

EARTH_RADIUS_M = 6_371_008.8
GEOCACHE_TTL_S = 30 * 24 * 3600


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def zone_for(lat: float, lon: float, zones: list[Zone]) -> Zone | None:
    """Return the closest zone whose circle contains the point, if any."""
    best: tuple[float, Zone] | None = None
    for z in zones:
        d = haversine_m(lat, lon, z.lat, z.lon)
        if d <= z.radius_m and (best is None or d < best[0]):
            best = (d, z)
    return best[1] if best else None


class Geocoder:
    def __init__(self, settings: Settings, store: Store, session: ClientSession) -> None:
        self.s = settings
        self.store = store
        self.session = session
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        contact = f" ({settings.geocoder_contact})" if settings.geocoder_contact else ""
        self._ua = f"KleptoCartographer/1.0 (+self-hosted equipment tracker){contact}"

    @staticmethod
    def _key(lat: float, lon: float) -> str:
        # 4 decimal places is ~11 m: plenty for an address label.
        return f"{lat:.4f},{lon:.4f}"

    async def reverse(self, lat: float, lon: float) -> str | None:
        if self.s.geocoder == "none" or self.s.demo:
            return None
        key = self._key(lat, lon)
        cached = self.store.geocache_get(key, GEOCACHE_TTL_S)
        if cached is not False:
            return cached  # may be None (cached "no address")
        try:
            if self.s.geocoder == "google":
                addr = await self._google(lat, lon)
            else:
                addr = await self._nominatim(lat, lon)
        except Exception as err:  # never let geocoding break a poll
            log.warning("Reverse geocode failed for %s: %s", key, err)
            return None
        self.store.geocache_put(key, addr, self.s.geocoder)
        return addr

    async def _nominatim(self, lat: float, lon: float) -> str | None:
        # Serialise and space calls >= 1.1 s apart (policy is 1/s absolute max).
        async with self._lock:
            wait = 1.1 - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with self.session.get(
                    "https://nominatim.openstreetmap.org/reverse",
                    params={
                        "format": "jsonv2",
                        "lat": f"{lat:.6f}",
                        "lon": f"{lon:.6f}",
                        "zoom": "18",
                        "addressdetails": "1",
                    },
                    headers={"User-Agent": self._ua, "Accept-Language": "en"},
                    timeout=ClientTimeout(total=15),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            finally:
                self._last_call = time.monotonic()

        if not data or "error" in data:
            return None
        a = data.get("address", {}) or {}
        street = " ".join(x for x in (a.get("house_number"), a.get("road")) if x)
        locality = (
            a.get("city") or a.get("town") or a.get("village") or a.get("hamlet")
            or a.get("suburb") or a.get("county")
        )
        name = data.get("name") or ""
        parts = [p for p in (name if name and name not in street else "", street, locality) if p]
        if parts:
            return ", ".join(parts)
        return ", ".join((data.get("display_name") or "").split(", ")[:3]) or None

    async def _google(self, lat: float, lon: float) -> str | None:
        async with self.session.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"latlng": f"{lat:.6f},{lon:.6f}", "key": self.s.google_geocoding_key},
            timeout=ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        status = data.get("status")
        if status == "ZERO_RESULTS":
            return None
        if status != "OK":
            # REQUEST_DENIED etc. Raise so it is logged and NOT cached.
            raise RuntimeError(f"Google geocoding status {status}: {data.get('error_message', '')}")
        addr = data["results"][0]["formatted_address"]
        return addr.removesuffix(", USA")
