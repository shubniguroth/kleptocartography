"""
The poll loop.

Every poll_interval_seconds:
  1. fetch all Tiles on the account (TileSource or DemoSource)
  2. keep only configured trackers (or everything, in discovery mode)
  3. store each *new* fix with its zone and reverse-geocoded address
  4. run the alert state machines
  5. ping Healthchecks (success or /fail)

Failure handling is the main difference from the old script, which recursed
into itself forever with no delay and died on the first exception:
  * any error is caught, logged, recorded for the UI banner and /healthz
  * the Tile session is dropped so the next attempt re-logs in cleanly
  * retries back off exponentially (interval, 2x, 4x ... capped at 30 min)
    with jitter, so a Tile outage or a bad password does not hammer their API
  * the map keeps serving the last known positions from SQLite meanwhile
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

from aiohttp import ClientSession, ClientTimeout

from .alerts import Alerter
from .config import Tracker, Settings
from .geo import Geocoder, zone_for
from .store import Store

log = logging.getLogger("klepto.poller")

MAX_BACKOFF_S = 1800
FETCH_TIMEOUT_S = 90


class Poller:
    def __init__(
        self, settings: Settings, store: Store, source: Any, geocoder: Geocoder,
        alerter: Alerter, session: ClientSession,
    ) -> None:
        self.s = settings
        self.store = store
        self.source = source
        self.geocoder = geocoder
        self.alerter = alerter
        self.session = session
        self._wake = asyncio.Event()
        self._unmatched_logged: set[str] = set()
        self.status: dict[str, Any] = store.kv_get(
            "poll_status",
            {"last_attempt": None, "last_ok": None, "last_error": None,
             "last_error_at": None, "consecutive_failures": 0, "next_poll": None,
             "other_tiles": 0, "missing_trackers": []},
        )
        self.status.pop("unmatched_tiles", None)
        # Index trackers by lower-cased Tile name and by UUID.
        self._by_key: dict[str, Tracker] = {}
        for h in settings.trackers:
            self._by_key[h.match.lower()] = h

    # ------------------------------------------------------------- matching
    def match(self, tile: dict[str, Any]) -> Tracker | None:
        return self._by_key.get(str(tile["uuid"]).lower()) or self._by_key.get(
            str(tile["name"]).strip().lower()
        )

    def poke(self) -> None:
        """Ask the loop to poll now (used by POST /api/poll)."""
        self._wake.set()

    # ----------------------------------------------------------------- loop
    async def run(self) -> None:
        delay = 0.0
        last_prune = 0.0
        while True:
            if delay:
                self.status["next_poll"] = int(time.time() + delay)
                self.store.kv_set("poll_status", self.status)
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()

            ok = await self.poll_once()
            if ok:
                delay = float(self.s.poll_interval_seconds)
            else:
                n = self.status["consecutive_failures"]
                delay = min(MAX_BACKOFF_S, self.s.poll_interval_seconds * (2 ** min(n - 1, 6)))
                delay *= random.uniform(0.85, 1.15)
                log.warning("Next attempt in %.0f s (failure #%d)", delay, n)

            if time.time() - last_prune > 86400:
                cutoff = int(time.time()) - self.s.history_days * 86400
                n_pruned = self.store.prune(cutoff)
                if n_pruned:
                    log.info("Pruned %d observations older than %d days", n_pruned, self.s.history_days)
                last_prune = time.time()

    async def poll_once(self) -> bool:
        now = int(time.time())
        self.status["last_attempt"] = now
        try:
            tiles = await asyncio.wait_for(self.source.fetch(), timeout=FETCH_TIMEOUT_S)
            await self._process(tiles, now)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            msg = f"{type(err).__name__}: {err}"
            if "InvalidAuth" in msg:
                msg += "  (check TILE_EMAIL / TILE_PASSWORD)"
            log.error("Poll failed: %s", msg)
            self.status.update(
                last_error=msg[:500], last_error_at=now,
                consecutive_failures=self.status.get("consecutive_failures", 0) + 1,
            )
            self.store.kv_set("poll_status", self.status)
            await self._ping_healthchecks(ok=False, body=msg)
            return False

        self.status.update(last_ok=now, consecutive_failures=0, last_error=None)
        self.store.kv_set("poll_status", self.status)
        await self._ping_healthchecks(ok=True)
        return True

    async def _process(self, tiles: list[dict[str, Any]], now: int) -> None:
        seen_trackers: set[str] = set()
        other_tiles = 0
        problems: list[str] = []

        for t in tiles:
            h = self.match(t)
            if h is None and not self.s.discovery_mode:
                # Privacy: Tiles that are not configured trackers are never
                # stored, logged by name, or exposed; only counted.
                other_tiles += 1
                continue
            self.store.upsert_tile(
                {"uuid": t["uuid"], "name": t["name"], "kind": t.get("kind"),
                 "archetype": t.get("archetype"), "firmware": t.get("firmware"),
                 "hardware": t.get("hardware"), "lost": t.get("lost"), "dead": t.get("dead")}
            )
            if h is not None:
                seen_trackers.add(h.match.lower())
            # Human-readable name for logs and alert messages.
            label = (h.name or t["name"]) if h else t["name"]

            if t.get("lat") is None or t.get("lon") is None or t.get("ts") is None:
                log.info("%s has no location fix yet", label)
                continue

            lat, lon = float(t["lat"]), float(t["lon"])
            z = zone_for(lat, lon, self.s.zones)
            obs = {
                "uuid": t["uuid"], "ts": int(t["ts"]), "lat": lat, "lon": lon,
                "accuracy": t.get("accuracy"), "altitude": t.get("altitude"),
                "zone": z.name if z else None, "address": None, "polled_at": now,
            }
            prev = self.store.latest(t["uuid"])
            is_new_fix = prev is None or prev["ts"] != obs["ts"]
            if is_new_fix:
                # Only geocode genuinely new fixes; repeats are free.
                obs["address"] = t.get("demo_address") or await self.geocoder.reverse(lat, lon)
                if self.store.insert_observation(obs):
                    log.info(
                        "%s: new fix %.5f,%.5f ±%sm zone=%s age=%ds",
                        label, lat, lon, obs["accuracy"], obs["zone"], now - obs["ts"],
                    )
            latest = self.store.latest(t["uuid"])
            problems += await self.alerter.evaluate(t["uuid"], label, dict(latest) if latest else None, now)

        missing = [h.match for h in self.s.trackers if h.match.lower() not in seen_trackers]
        if missing:
            log.warning("Configured trackers not found on the Tile account: %s", ", ".join(missing))
        if other_tiles and not self._unmatched_logged:
            log.info("Ignoring %d other Tile(s) on the account (not configured as trackers)", other_tiles)
            self._unmatched_logged.add("logged")
        self.status["other_tiles"] = other_tiles
        self.status.pop("unmatched_tiles", None)
        self.status["missing_trackers"] = missing
        self.status["active_problems"] = problems
        await self._ping_alert_check(problems)

    async def _ping_alert_check(self, problems: list[str]) -> None:
        """Tracker-status check for Healthchecks: /fail with the details while
        anything is away or stale, a plain success ping when all is well.
        Healthchecks notifies on the up/down transitions, and the check also
        goes down by itself if Klepto stops polling."""
        url = self.s.alert_healthchecks_url
        if not url:
            return
        body = "\n".join(problems) if problems else "All trackers home and recently heard."
        target = url.rstrip("/") + "/fail" if problems else url
        try:
            async with self.session.post(
                target, data=body.encode()[:10000], timeout=ClientTimeout(total=10)
            ):
                pass
        except Exception as err:
            log.warning("Alert Healthchecks ping failed: %s", err)

    async def _ping_healthchecks(self, ok: bool, body: str = "") -> None:
        """Optional dead-man's switch for the self-hosted Healthchecks instance."""
        url = self.s.healthchecks_url
        if not url:
            return
        try:
            target = url if ok else url.rstrip("/") + "/fail"
            async with self.session.post(
                target, data=body.encode()[:10000], timeout=ClientTimeout(total=10)
            ):
                pass
        except Exception as err:
            log.debug("Healthchecks ping failed: %s", err)
