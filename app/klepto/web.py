"""
HTTP layer (aiohttp, same event loop as the poller).

  GET  /                 the map (static/index.html)
  GET  /api/config       static settings the page needs (title, zones, map key)
  GET  /api/state        current position + freshness of every tracker
  GET  /api/history      ?uuid=...&hours=24  -> fixes for drawing a trail
  POST /api/poll         ask for an immediate poll (rate-limited)
  GET  /healthz          liveness for Docker; ?strict=1 also requires fresh data

The page polls /api/state every [app].refresh_seconds; the map itself is
loaded once, so Google bills one map load per page open, not per refresh.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from .config import DEFAULT_COLORS, Settings
from .poller import Poller
from .store import Store

log = logging.getLogger("klepto.web")

STATIC_DIR = Path(__file__).parent / "static"
STARTED = int(time.time())


def _asset_version() -> str:
    """Cache-buster for app.js/app.css: hash of their contents."""
    h = hashlib.sha1()
    for name in ("app.js", "app.css"):
        p = STATIC_DIR / name
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:10]


@web.middleware
async def headers_mw(request: web.Request, handler):
    resp = await handler(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    # Origin-only referrer still satisfies a Google key restricted to
    # https://your.domain/* while not leaking paths to third parties.
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.path.startswith("/api/") or request.path == "/healthz":
        resp.headers["Cache-Control"] = "no-store"
    elif request.path.startswith("/static/vendor/"):
        resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


class WebApp:
    def __init__(self, settings: Settings, store: Store, poller: Poller) -> None:
        self.s = settings
        self.store = store
        self.poller = poller
        self.version = _asset_version()
        self._index = (STATIC_DIR / "index.html").read_text().replace("{{VERSION}}", self.version)

    def build(self) -> web.Application:
        app = web.Application(middlewares=[headers_mw])
        app.router.add_get("/", self.index)
        app.router.add_get("/api/config", self.config)
        app.router.add_get("/api/state", self.state)
        app.router.add_get("/api/history", self.history)
        app.router.add_get("/api/tiles", self.tiles)
        app.router.add_post("/api/poll", self.poll_now)
        app.router.add_get("/healthz", self.healthz)
        app.router.add_static("/static/", STATIC_DIR, show_index=False)
        return app

    # ------------------------------------------------------------------ pages
    async def index(self, request: web.Request) -> web.Response:
        return web.Response(
            text=self._index, content_type="text/html", headers={"Cache-Control": "no-cache"}
        )

    async def config(self, request: web.Request) -> web.Response:
        s = self.s
        return web.json_response(
            {
                "title": s.title,
                "subtitle": s.subtitle,
                "demo": s.demo,
                "discovery": s.discovery_mode,
                "map": {
                    "default_layer": s.default_layer if (s.google_maps_js_key or not s.default_layer.startswith("google")) else "streets",
                    "center": list(s.center),
                    "zoom": s.zoom,
                    # The Maps JS key is public by design (it is in every page
                    # that uses Google Maps). Protect it with an HTTP-referrer
                    # restriction in Google Cloud, not by hiding it.
                    "google_maps_js_key": s.google_maps_js_key or None,
                },
                "zones": [
                    {"name": z.name, "lat": z.lat, "lon": z.lon, "radius_m": z.radius_m, "home": z.home}
                    for z in s.zones
                ],
                "freshness": {"fresh_minutes": s.fresh_minutes, "stale_hours": s.stale_hours},
                "refresh_seconds": s.refresh_seconds,
                "poll_interval_seconds": s.poll_interval_seconds,
                # Set by Cloudflare Access when the app sits behind it.
                "viewer": request.headers.get("Cf-Access-Authenticated-User-Email"),
            }
        )

    # ------------------------------------------------------------------- data
    def _status(self, ts: int | None, now: int) -> str:
        if ts is None:
            return "nofix"
        age = now - ts
        if age <= self.s.fresh_minutes * 60:
            return "fresh"
        if age <= self.s.stale_hours * 3600:
            return "aging"
        return "stale"

    def _items(self, now: int) -> list[dict[str, Any]]:
        tiles = self.store.tiles()
        by_uuid = {t["uuid"].lower(): t for t in tiles}
        by_name = {t["tile_name"].strip().lower(): t for t in tiles}
        home_names = {z.name for z in self.s.zones if z.home}

        # Configured trackers in config order, or every Tile in discovery mode.
        if self.s.discovery_mode:
            specs = [
                {"match": t["uuid"], "label": str(i + 1), "name": t["tile_name"],
                 "color": DEFAULT_COLORS[i % len(DEFAULT_COLORS)],
                 "note": f"TRACKER_n={t['uuid']}", "tile": t}
                for i, t in enumerate(tiles)
            ]
        else:
            specs = []
            for h in self.s.trackers:
                key = h.match.lower()
                tile = by_uuid.get(key) or by_name.get(key)
                specs.append(
                    {"match": h.match, "label": h.label,
                     # Explicit name wins; else the Tile's own name; else what was configured.
                     "name": h.name or (tile["tile_name"] if tile else h.match),
                     "color": h.color, "note": h.note, "tile": tile}
                )

        items = []
        for sp in specs:
            t = sp["tile"]
            item: dict[str, Any] = {
                "key": sp["match"], "label": sp["label"], "name": sp["name"],
                "color": sp["color"], "note": sp["note"], "found": t is not None,
                "uuid": t["uuid"] if t else None, "tile_name": t["tile_name"] if t else None,
                "lost": bool(t["lost"]) if t else False, "dead": bool(t["dead"]) if t else False,
                "lat": None, "lon": None, "accuracy": None, "ts": None, "age_s": None,
                "zone": None, "address": None, "home": None, "status": "missing" if t is None else "nofix",
            }
            if t is not None:
                o = self.store.latest(t["uuid"])
                if o is not None:
                    item.update(
                        lat=o["lat"], lon=o["lon"], accuracy=o["accuracy"], ts=o["ts"],
                        age_s=now - o["ts"], zone=o["zone"], address=o["address"],
                        home=(o["zone"] in home_names) if home_names else None,
                        status=self._status(o["ts"], now),
                    )
            items.append(item)
        return items

    async def state(self, request: web.Request) -> web.Response:
        now = int(time.time())
        st = dict(self.poller.status)
        events = [
            {"ts": e["created_at"], "kind": e["kind"], "message": e["message"], "uuid": e["uuid"]}
            for e in self.store.recent_events(25)
        ]
        return web.json_response(
            {
                "now": now,
                "poll": {
                    "last_ok": st.get("last_ok"),
                    "last_attempt": st.get("last_attempt"),
                    "last_error": st.get("last_error"),
                    "last_error_at": st.get("last_error_at"),
                    "consecutive_failures": st.get("consecutive_failures", 0),
                    "next_poll": st.get("next_poll"),
                    "interval": self.s.poll_interval_seconds,
                },
                "items": self._items(now),
                "events": events,
                "unmatched_tiles": st.get("unmatched_tiles", []),
                "missing_trackers": st.get("missing_trackers", []),
            }
        )

    async def history(self, request: web.Request) -> web.Response:
        uuid = request.query.get("uuid", "")
        try:
            hours = max(1, min(int(request.query.get("hours", "24")), self.s.history_days * 24))
        except ValueError:
            raise web.HTTPBadRequest(text="hours must be an integer")
        if not uuid:
            raise web.HTTPBadRequest(text="uuid required")
        since = int(time.time()) - hours * 3600
        rows = self.store.history(uuid, since)
        return web.json_response(
            [{"ts": r["ts"], "lat": r["lat"], "lon": r["lon"], "accuracy": r["accuracy"],
              "zone": r["zone"], "address": r["address"]} for r in rows]
        )

    async def tiles(self, request: web.Request) -> web.Response:
        """Every Tile ever seen on the account, configured or not: the quick way
        to read off names and UUIDs for TRACKER_n without a console."""
        now = int(time.time())
        configured = {h.match.lower(): h.label for h in self.s.trackers}
        out = []
        for t in self.store.tiles():
            o = self.store.latest(t["uuid"])
            out.append({
                "name": t["tile_name"], "uuid": t["uuid"], "kind": t["kind"],
                "used_as": configured.get(t["uuid"].lower()) or configured.get(t["tile_name"].strip().lower()),
                "last_seen": o["ts"] if o else None,
                "age_s": now - o["ts"] if o else None,
                "zone": o["zone"] if o else None,
            })
        return web.json_response(out)

    async def poll_now(self, request: web.Request) -> web.Response:
        last = self.poller.status.get("last_attempt") or 0
        if time.time() - last < 30:
            return web.json_response({"queued": False, "reason": "polled < 30 s ago"}, status=429)
        self.poller.poke()
        return web.json_response({"queued": True})

    async def healthz(self, request: web.Request) -> web.Response:
        now = int(time.time())
        last_ok = self.poller.status.get("last_ok")
        max_age = max(3 * self.s.poll_interval_seconds, 900)
        fresh = bool(last_ok and now - last_ok <= max_age)
        body = {"alive": True, "data_fresh": fresh, "last_ok": last_ok, "uptime_s": now - STARTED}
        strict = request.query.get("strict") in {"1", "true", "yes"}
        starting = last_ok is None and now - STARTED < 300
        status = 503 if (strict and not fresh and not starting) else 200
        return web.json_response(body, status=status)
