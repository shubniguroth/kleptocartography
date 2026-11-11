"""
Edge-triggered alerts.

Two conditions per tracker, each a tiny state machine stored in SQLite:

  away   the latest fix is outside every zone marked as home
         -> "Laptop bag left all home zones" once, then "Laptop bag is back at Office" once.
  stale  no phone has reported the tracker for [freshness].stale_hours
         -> one alert when it goes stale, one when it is heard again.

The first time a tracker is evaluated (fresh install, new tracker) the
state is recorded silently, so a restart never produces an alert storm.

Every transition is written to the events table (shown in the UI's activity
list) whether or not notifications are enabled. Delivery uses Apprise, which
speaks ~100 services from one URL string: ntfy, email (e.g. a Teams channel's
email address), Slack, Discord, Pushover, Power Automate workflows, etc.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import Settings
from .store import Store

log = logging.getLogger("klepto.alerts")


class Alerter:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.s = settings
        self.store = store
        self.tz = ZoneInfo(settings.timezone)
        self._apprise = None
        if settings.alerts_enabled and settings.apprise_urls:
            try:
                import apprise

                ap = apprise.Apprise()
                for url in settings.apprise_urls:
                    if not ap.add(url):
                        log.error("Apprise rejected a notification URL (check APPRISE_URLS syntax)")
                self._apprise = ap if len(ap) else None
            except ImportError:
                log.error("alerts.enabled but apprise is not installed")
        if settings.alerts_enabled and not self._apprise:
            log.warning("alerts.enabled = true but no usable APPRISE_URLS; events are logged only")

    def fmt_time(self, ts: int) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).astimezone(self.tz).strftime("%a %b %-d, %-I:%M %p")

    async def evaluate(self, uuid: str, label: str, latest: dict | None, now: int) -> list[str]:
        """Run the edge-triggered alerts for one tracker and return the
        conditions that are active RIGHT NOW (level, not edge), used for the
        ALERT_HEALTHCHECKS_URL status ping."""
        active: list[str] = []
        if latest is None:
            return active
        home_zones = [z for z in self.s.zones if z.home]

        # ---- away / back -------------------------------------------------
        if home_zones:
            away = latest["zone"] not in {z.name for z in home_zones}
            where = latest["address"] or f"{latest['lat']:.5f}, {latest['lon']:.5f}"
            if latest["zone"]:
                where = f"{latest['zone']} ({where})" if latest["address"] else latest["zone"]
            away_msg = (f"{label} left all home zones: last seen near {where} at "
                        f"{self.fmt_time(latest['ts'])} (±{int(latest['accuracy'] or 0)} m)")
            if away:
                active.append(away_msg)
            await self._edge(
                uuid, "away", away, now,
                on_msg=away_msg,
                off_msg=f"{label} is back at {latest['zone']} ({self.fmt_time(latest['ts'])})",
                notify_on=self.s.alert_on_leave,
                notify_off=self.s.alert_on_return,
            )

        # ---- stale / heard again -----------------------------------------
        age_h = (now - latest["ts"]) / 3600
        stale = age_h > self.s.stale_hours
        place = latest["zone"] or latest["address"] or "unknown location"
        stale_msg = (f"{label} has not been heard by any phone for {int(age_h)} h. "
                     f"Last seen at {place}, {self.fmt_time(latest['ts'])}")
        if stale:
            active.append(stale_msg)
        await self._edge(
            uuid, "stale", stale, now,
            on_msg=stale_msg,
            off_msg=f"{label} was heard again at {place} ({self.fmt_time(latest['ts'])})",
            notify_on=self.s.alert_on_stale,
            notify_off=self.s.alert_on_stale,
        )
        return active

    async def _edge(
        self, uuid: str, kind: str, active: bool, now: int, *,
        on_msg: str, off_msg: str, notify_on: bool, notify_off: bool,
    ) -> None:
        prev = self.store.alert_get(uuid, kind)
        if prev is None:
            # First sighting: remember the state, do not alert.
            self.store.alert_set(uuid, kind, active, now)
            return
        if bool(prev["active"]) == active:
            return
        self.store.alert_set(uuid, kind, active, now)
        msg = on_msg if active else off_msg
        event_kind = f"{kind}_{'on' if active else 'off'}"
        event_id = self.store.add_event(uuid, event_kind, msg)
        log.info("EVENT %s: %s", event_kind, msg)
        if (notify_on if active else notify_off):
            await self.send(self.s.title, msg, event_id)

    async def send(self, title: str, body: str, event_id: int | None = None) -> bool:
        if not self._apprise:
            return False
        if self.s.public_url:
            body = f"{body}\n\nMap: {self.s.public_url}"
        try:
            ok = await asyncio.to_thread(self._apprise.notify, title=title, body=body)
        except Exception as err:
            log.error("Notification failed: %s", err)
            return False
        if ok and event_id is not None:
            self.store.mark_delivered(event_id)
        if not ok:
            log.error("Apprise reported a delivery failure for: %s", body)
        return bool(ok)
