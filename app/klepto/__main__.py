"""
Entry point:  python -m klepto

Wires together settings -> store -> source -> geocoder/alerter -> poller,
then serves the web app on KLEPTO_PORT (default 8787) and runs the poller as
a background task on the same event loop. SIGTERM (docker stop) shuts both
down cleanly so SQLite is never left mid-write.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

from .alerts import Alerter
from .config import load_settings
from .geo import Geocoder
from .poller import Poller
from .sources import DemoSource, TileSource
from .store import Store
from .web import WebApp


_RELEVANT_PREFIXES = ("TILE_", "TRACKER_", "ZONES", "GOOGLE_", "MAP_", "GEOCODER", "ALERT", "APPRISE", "KLEPTO_")


class _MissingCredentials:
    """Stand-in source when the Tile login is absent: every poll reports a
    clear, actionable error (shown in the page banner) instead of the
    container exiting and restarting forever."""

    def __init__(self, seen: list[str]) -> None:
        self.seen = seen

    async def fetch(self):
        raise RuntimeError(
            "TILE_EMAIL / TILE_PASSWORD are not set inside the container "
            f"(variables it can see: {', '.join(self.seen) or 'none'}). "
            "Pass them via environment: in the compose file, then Update the stack."
        )


def setup_logging() -> None:
    level = os.environ.get("KLEPTO_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # pytile logs every raw API payload at DEBUG; keep it quiet unless asked.
    logging.getLogger("pytile").setLevel(logging.WARNING if level != "DEBUG" else logging.DEBUG)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def main() -> None:
    setup_logging()
    log = logging.getLogger("klepto")
    s = load_settings()

    missing_creds = not s.demo and not (s.tile_email and s.tile_password)
    if missing_creds:
        # Do not crash-loop: keep serving the page, which shows this error in a
        # banner, and log which relevant variable NAMES did reach the container
        # (never values) so a mis-wired stack is obvious.
        seen = sorted(k for k in os.environ if k.startswith(_RELEVANT_PREFIXES))
        log.error(
            "TILE_EMAIL and TILE_PASSWORD are not set inside the container. "
            "Relevant variables it can see: %s. If you set them in Portainer, pass them "
            "through `environment:` in the compose file and redeploy (Update the stack).",
            ", ".join(seen) or "none",
        )

    try:
        s.data_dir.mkdir(parents=True, exist_ok=True)
        store = Store(s.data_dir / "klepto.sqlite3")
    except (PermissionError, OSError) as err:
        log.error(
            "Cannot write to %s (%s). Run `mkdir -p data` before first start and make sure "
            "PUID/PGID in .env match the owner of the data folder (container uid %s).",
            s.data_dir, err, os.getuid(),
        )
        sys.exit(2)
    if not s.discovery_mode:
        # Privacy: forget any Tile that is not a configured tracker (e.g. the
        # whole account, seen during a discovery-mode run).
        keys = {t.match.lower() for t in s.trackers}
        keep = {r["uuid"] for r in store.tiles()
                if r["uuid"].lower() in keys or r["tile_name"].strip().lower() in keys}
        purged = store.purge_except(keep)
        if any(purged.values()):
            log.info("Privacy purge of non-tracker data: %s", purged)

    session = ClientSession(timeout=ClientTimeout(total=30), connector=TCPConnector(limit=10))

    if s.demo:
        source = DemoSource()
    elif missing_creds:
        source = _MissingCredentials(sorted(k for k in os.environ if k.startswith(_RELEVANT_PREFIXES)))
    else:
        source = TileSource(s.tile_email, s.tile_password, session, s.data_dir)
    geocoder = Geocoder(s, store, session)
    alerter = Alerter(s, store)
    poller = Poller(s, store, source, geocoder, alerter, session)

    mode = "DEMO" if s.demo else ("discovery (no trackers configured)" if s.discovery_mode else
                                  f"{len(s.trackers)} trackers")
    log.info(
        "Klepto Cartographer starting: %s, %d zones, poll every %ds, geocoder=%s, map=%s, alerts=%s",
        mode, len(s.zones), s.poll_interval_seconds, s.geocoder,
        "google+fallback" if s.google_maps_js_key else "free tiles",
        "on" if s.alerts_enabled and s.apprise_urls else "off",
    )

    runner = web.AppRunner(WebApp(s, store, poller).build(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=s.port)
    await site.start()
    log.info("Serving on http://0.0.0.0:%d", s.port)

    poll_task = asyncio.create_task(poller.run(), name="poller")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover (Windows)
            pass

    # Exit if the poller task dies unexpectedly, so Docker restarts us.
    done, _ = await asyncio.wait(
        {poll_task, asyncio.create_task(stop.wait())}, return_when=asyncio.FIRST_COMPLETED
    )
    if poll_task in done and poll_task.exception():
        log.error("Poller crashed: %r", poll_task.exception())

    log.info("Shutting down")
    poll_task.cancel()
    await asyncio.gather(poll_task, return_exceptions=True)
    await runner.cleanup()
    await session.close()
    store.close()
    if poll_task in done and not poll_task.cancelled() and poll_task.exception():
        sys.exit(1)  # non-zero so the restart policy treats it as a crash


if __name__ == "__main__":
    asyncio.run(main())
