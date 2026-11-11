"""
List every Tile on the account, so you can fill in TRACKER_n.

    python -m klepto.tiles

Inside the running container (Portainer: Containers -> klepto -> Console,
or `docker exec -it klepto python -m klepto.tiles`) it uses the same
TILE_EMAIL / TILE_PASSWORD the service uses.

Prints a table (name, UUID, kind, last seen, where) and then a block of
TRACKER_n lines you can paste into Portainer's environment variables
(Advanced mode). Matching by UUID survives someone renaming a Tile in the app;
matching by name is easier to read. Either works.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, ClientTimeout

from .config import load_settings
from .geo import zone_for
from .sources import DemoSource, TileSource


def _ago(sec: float) -> str:
    if sec < 3600:
        return f"{int(sec // 60)} min ago"
    if sec < 86400:
        return f"{sec / 3600:.1f} h ago"
    return f"{sec / 86400:.1f} d ago"


async def main() -> int:
    s = load_settings()
    if not s.demo and not (s.tile_email and s.tile_password):
        print("TILE_EMAIL and TILE_PASSWORD must be set (they already are inside the klepto container).")
        return 2

    # Reuse the service's stable Tile client id when /data is writable.
    data_dir = s.data_dir if os.access(s.data_dir, os.W_OK) else Path(tempfile.mkdtemp())
    async with ClientSession(timeout=ClientTimeout(total=30)) as session:
        source = DemoSource() if s.demo else TileSource(s.tile_email, s.tile_password, session, data_dir)
        tiles = await source.fetch()

    tz = ZoneInfo(s.timezone)
    now = time.time()
    configured = {h.match.lower(): h for h in s.trackers}
    tiles.sort(key=lambda t: str(t["name"]).lower())

    rows = []
    for t in tiles:
        h = configured.get(str(t["uuid"]).lower()) or configured.get(str(t["name"]).strip().lower())
        if t.get("ts"):
            seen = datetime.fromtimestamp(t["ts"], timezone.utc).astimezone(tz).strftime("%b %d %H:%M")
            seen = f"{seen} ({_ago(now - t['ts'])})"
        else:
            seen = "never"
        where = ""
        if t.get("lat") is not None:
            z = zone_for(t["lat"], t["lon"], s.zones)
            where = z.name if z else f"{t['lat']:.5f},{t['lon']:.5f}"
        rows.append((t["name"], t["uuid"], t.get("kind") or "", seen, where, h.label if h else ""))

    headers = ("NAME", "UUID", "KIND", "LAST SEEN", "WHERE", "USED AS")
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    line = "  ".join("{:<%d}" % w for w in widths)
    print(f"\n{len(rows)} Tile(s) on {s.tile_email or 'the demo account'}:\n")
    print(line.format(*headers))
    print(line.format(*("-" * w for w in widths)))
    for r in rows:
        print(line.format(*r))

    candidates = [t for t in tiles if (t.get("kind") or "TILE").upper() != "PHONE"]
    print("\nPaste into Portainer -> Stack -> Environment variables -> Advanced mode")
    print("(keep only the Tiles you want on the map; renumber if you like):\n")
    for n, t in enumerate(candidates, start=1):
        print(f"TRACKER_{n}={t['uuid']}")
        print(f"TRACKER_{n}_NAME={t['name']}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
