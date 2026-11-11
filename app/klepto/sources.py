"""
Where tracker positions come from.

TileSource   the real thing, via pytile (unofficial Tile cloud API).
DemoSource   simulated trackers so the UI and alerts can be exercised without
             a Tile account (KLEPTO_DEMO=1).

Both return a list of plain dicts so the rest of the app never touches pytile
objects directly. If Tile ever breaks pytile, only this file changes.

Important property of Tile data: a Tile has no GPS. Its "location" is the GPS
fix of whichever phone (yours, or any stranger running the Tile or Life360
app) last heard it over Bluetooth. So `ts` is "when a phone last heard it",
not "now", and accuracy is phone-GPS accuracy (often 10-65 m indoors).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid as uuidlib
from datetime import timezone
from pathlib import Path
from typing import Any

from aiohttp import ClientSession

log = logging.getLogger("klepto.source")


def _safe(obj: Any, attr: str, default: Any = None) -> Any:
    """pytile properties index straight into the JSON; a missing key raises.
    Treat any such failure as 'unknown' rather than failing the whole poll."""
    try:
        return getattr(obj, attr)
    except Exception:
        return default


class TileSource:
    def __init__(self, email: str, password: str, session: ClientSession, data_dir: Path) -> None:
        self.email = email
        self.password = password
        self.session = session
        self._api = None
        # A stable client UUID makes us look like the same "phone" to Tile on
        # every restart, instead of registering a brand-new client each time
        # (which is the kind of thing that gets accounts flagged).
        uuid_file = data_dir / "tile_client_uuid"
        if uuid_file.exists():
            self.client_uuid = uuid_file.read_text().strip()
        else:
            self.client_uuid = str(uuidlib.uuid4()).upper()
            uuid_file.write_text(self.client_uuid)

    async def fetch(self) -> list[dict[str, Any]]:
        # Imported here so demo mode and tests do not need pytile installed.
        from pytile import async_login

        if self._api is None:
            log.info("Logging in to Tile as %s", self.email)
            self._api = await async_login(
                self.email, self.password, self.session, client_uuid=self.client_uuid
            )
        try:
            tiles = await self._api.async_get_tiles()
        except Exception:
            # Drop the session so the next poll performs a clean re-login.
            self._api = None
            raise

        out: list[dict[str, Any]] = []
        for tile_uuid, t in tiles.items():
            last = _safe(t, "last_timestamp")
            ts = None
            if last is not None:
                # pytile returns a naive datetime that is actually UTC.
                ts = int(last.replace(tzinfo=timezone.utc).timestamp())
            out.append(
                {
                    "uuid": _safe(t, "uuid", tile_uuid),
                    "name": _safe(t, "name", tile_uuid),
                    "kind": _safe(t, "kind"),
                    "archetype": _safe(t, "archetype"),
                    "firmware": _safe(t, "firmware_version"),
                    "hardware": _safe(t, "hardware_version"),
                    "lost": _safe(t, "lost", False),
                    "dead": _safe(t, "dead", False),
                    "lat": _safe(t, "latitude"),
                    "lon": _safe(t, "longitude"),
                    "accuracy": _safe(t, "accuracy"),
                    "altitude": _safe(t, "altitude"),
                    "ts": ts,
                }
            )
        return out


class DemoSource:
    """Six fake Tiles with a little story (pairs with the demo zones
    "Office" and "Warehouse" in config.DEMO_ZONES):

    Camera kit    at the Office, freshly heard every poll (wanders a few metres)
    Drill case    at the Office, heard ~40 min ago
    Projector     at the Warehouse, heard ~3 h ago          -> "Hours old"
    Laptop bag    starts at the Office, then leaves         -> "Away" + alert
    Spare keys    at the Office, not heard for 3 days        -> "Stale"
    Old rucksack  far away and long silent (handy for testing TRACKER_n filtering)
    """

    OFFICE = (40.75490, -73.98400)
    WAREHOUSE = (40.74400, -73.93600)
    ELSEWHERE = (40.67150, -73.96350)
    FAR_AWAY = (41.88270, -87.62330)

    def __init__(self) -> None:
        self.polls = 0
        self.t0 = int(time.time())
        self.rng = random.Random(1234)

    async def fetch(self) -> list[dict[str, Any]]:
        await asyncio.sleep(0.05)
        self.polls += 1
        now = int(time.time())
        r = self.rng

        def tile(n: int, name: str, lat: float, lon: float, age_s: int, acc: float, **kw: Any) -> dict:
            return {
                "uuid": f"demo{n:012d}",
                "name": name,
                "kind": "TILE",
                "archetype": "MATE",
                "firmware": "48.01.02.0",
                "hardware": "24.00",
                "lost": False,
                "dead": False,
                "lat": lat,
                "lon": lon,
                "accuracy": acc,
                "altitude": 30.0,
                # Static demo fixes are anchored to start-up time so repeat
                # polls return the same fix, exactly like the real Tile cloud.
                "ts": (now if kw.pop("live", False) else self.t0) - age_s,
                **kw,
            }

        j = (r.uniform(-0.0002, 0.0002), r.uniform(-0.0002, 0.0002))
        left = self.polls >= 2  # the laptop bag leaves on the second poll
        bag = self.ELSEWHERE if left else (self.OFFICE[0] + 0.0004, self.OFFICE[1] - 0.0005)
        o, w = self.OFFICE, self.WAREHOUSE

        return [
            tile(1, "Camera kit", o[0] + j[0], o[1] + j[1], r.randint(30, 300), 18, live=True,
                 demo_address="West 40th Street, Midtown (demo)"),
            tile(2, "Drill case", o[0] - 0.0003, o[1] + 0.0002, 40 * 60, 35,
                 demo_address="West 40th Street, Midtown (demo)"),
            tile(3, "Projector", w[0], w[1], 3 * 3600 + 600, 25,
                 demo_address="Skillman Avenue, Long Island City (demo)"),
            tile(4, "Laptop bag", bag[0], bag[1], -20 if left else 6 * 3600, 12,
                 demo_address="Eastern Parkway, Brooklyn (demo)" if left
                 else "West 40th Street, Midtown (demo)"),
            tile(5, "Spare keys", o[0] + 0.0002, o[1] + 0.0001, 3 * 86400 + 4000, 60,
                 demo_address="West 40th Street, Midtown (demo)"),
            tile(6, "Old rucksack", *self.FAR_AWAY, 200 * 86400, 30,
                 demo_address="Michigan Avenue, Chicago (demo)"),
        ]
