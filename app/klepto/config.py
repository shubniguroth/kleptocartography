"""
Configuration for Klepto Cartographer.

Everything is configurable through ENVIRONMENT VARIABLES, so the container can
be deployed as a Portainer stack (or plain `docker run`) with no files to
mount. Precedence, lowest to highest:

    built-in defaults  <  /config/config.toml (optional)  <  environment variables

Environment variables (all optional except the Tile login):

  Tile login
    TILE_EMAIL, TILE_PASSWORD

  Which Tiles to show (one numbered variable per tracker)
    TRACKER_1=<Tile name as shown in the app, or Tile UUID>
    TRACKER_1_LABEL=1           short text on the map pin (default: the number)
    TRACKER_1_NAME=Camera bag   name in the side panel (default: the Tile's own name)
    TRACKER_1_COLOR=#0072B2     (default: colour-blind-safe palette)
    TRACKER_1_NOTE=Serial 123   (optional, shown on the card)
    ...TRACKER_2, TRACKER_3, ... any number, ordered by n.
    No TRACKER_n at all = discovery mode (every Tile on the account is shown).

  Zones (optional; one line, ';' between zones)
    ZONES=Office:40.7549,-73.9840,150;Warehouse:40.7440,-73.9360,250
        each zone is  Name:lat,lon,radius_m[,away]
        Zones are "home" by default: where trackers belong. Leaving every
        home zone marks a tracker "Away" (and can alert). Add ",away" for a
        named zone that is NOT home (e.g. a repair shop).
        Unset or "none" = no zones; the map simply shows positions.

  Map and lookups
    GOOGLE_MAPS_JS_KEY, GOOGLE_GEOCODING_KEY
    MAP_DEFAULT_LAYER   google_roadmap | google_hybrid | streets | dark | satellite | osm
    MAP_CENTER=lat,lon  MAP_ZOOM   (view before the first fix; afterwards the
                                    map fits all trackers automatically)
    GEOCODER            nominatim | google | none      GEOCODER_CONTACT=<email>

  Behaviour
    TITLE, SUBTITLE, TIMEZONE (IANA name, used for times in alert messages)
    POLL_INTERVAL_SECONDS (180), REFRESH_SECONDS (30), HISTORY_DAYS (120)
    FRESH_MINUTES (120), STALE_HOURS (24)

  Alerts and monitoring
    ALERTS_ENABLED (false), ALERT_ON_LEAVE, ALERT_ON_RETURN, ALERT_ON_STALE (true)
    APPRISE_URLS (space/comma separated), HEALTHCHECKS_URL

  Runtime
    KLEPTO_DEMO, KLEPTO_LOG_LEVEL, KLEPTO_PORT, KLEPTO_DATA_DIR, KLEPTO_CONFIG
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("klepto.config")

# A colour-blind-friendly categorical palette (Okabe-Ito plus extras).
DEFAULT_COLORS = [
    "#0072B2",  # blue
    "#D55E00",  # vermilion
    "#009E73",  # bluish green
    "#CC79A7",  # reddish purple
    "#E69F00",  # orange
    "#56B4E9",  # sky blue
    "#7A5195",  # violet
    "#4D4D4D",  # graphite
]

LAYER_IDS = {"google_roadmap", "google_hybrid", "streets", "dark", "satellite", "osm"}


@dataclass
class Zone:
    """A named circle on the map. `home = True` zones are where trackers
    belong; being outside all of them is what "Away" means."""

    name: str
    lat: float
    lon: float
    radius_m: float = 400.0
    home: bool = True


# Only used in demo mode when ZONES is unset, to show zone behaviour.
DEMO_ZONES = [
    Zone("Office", 40.75490, -73.98400, 250, True),
    Zone("Warehouse", 40.74400, -73.93600, 300, True),
]


@dataclass
class Tracker:
    match: str   # Tile name (case-insensitive) or Tile UUID
    label: str   # short pin text
    name: str    # friendly panel name ("" = use the Tile's own name)
    color: str
    note: str = ""


@dataclass
class Settings:
    title: str = "Klepto Cartographer"
    subtitle: str = "Last known locations"
    timezone: str = "UTC"
    poll_interval_seconds: int = 180
    history_days: int = 120
    refresh_seconds: int = 30

    fresh_minutes: int = 120
    stale_hours: int = 24

    default_layer: str = "google_roadmap"
    center: tuple[float, float] = (20.0, 0.0)
    zoom: int = 3

    geocoder: str = "nominatim"
    geocoder_contact: str = ""

    alerts_enabled: bool = False
    alert_on_leave: bool = True
    alert_on_return: bool = True
    alert_on_stale: bool = True

    zones: list[Zone] = field(default_factory=list)
    trackers: list[Tracker] = field(default_factory=list)

    tile_email: str = ""
    tile_password: str = ""
    google_maps_js_key: str = ""
    google_geocoding_key: str = ""
    apprise_urls: list[str] = field(default_factory=list)
    healthchecks_url: str = ""
    demo: bool = False
    data_dir: Path = Path("/data")
    port: int = 8787
    config_path: Path = Path("/config/config.toml")

    @property
    def discovery_mode(self) -> bool:
        return not self.trackers


# --------------------------------------------------------------------- helpers
def _env(name: str) -> str | None:
    """Env value, or None if unset/blank (Portainer often saves blanks)."""
    v = os.environ.get(name)
    return v.strip() if v is not None and v.strip() != "" else None


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _split_urls(raw: str) -> list[str]:
    return [p for p in raw.replace(",", " ").split() if p]


def parse_zones(raw: str) -> list[Zone]:
    """'Office:40.75,-73.98,150;Repair shop:40.7,-73.8,100,away' -> [Zone, Zone]."""
    if raw.strip().lower() in {"none", "off", "-"}:
        return []
    zones: list[Zone] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            name, _, rest = chunk.rpartition(":")
            parts = [p.strip() for p in rest.split(",")]
            lat, lon = float(parts[0]), float(parts[1])
            radius = float(parts[2]) if len(parts) > 2 and parts[2] else 400.0
            home = not (len(parts) > 3 and parts[3].lower() in {"away", "visit", "nothome", "false"})
            if not name:
                raise ValueError("missing zone name")
            zones.append(Zone(name.strip(), lat, lon, radius, home))
        except (ValueError, IndexError) as err:
            log.error("Ignoring malformed zone %r in ZONES (%s). Format: Name:lat,lon,radius", chunk, err)
    return zones


def _trackers_from_env() -> list[Tracker]:
    nums = sorted(
        int(m.group(1))
        for k in os.environ
        if (m := re.fullmatch(r"TRACKER_(\d+)", k)) and _env(k)
    )
    out: list[Tracker] = []
    for i, n in enumerate(nums):
        out.append(
            Tracker(
                match=_env(f"TRACKER_{n}") or "",
                label=(_env(f"TRACKER_{n}_LABEL") or str(n))[:6],
                # Blank = use the Tile's own name from the account (resolved in web.py).
                name=_env(f"TRACKER_{n}_NAME") or "",
                color=_env(f"TRACKER_{n}_COLOR") or DEFAULT_COLORS[i % len(DEFAULT_COLORS)],
                note=_env(f"TRACKER_{n}_NOTE") or "",
            )
        )
    return out


def _apply_toml(s: Settings, raw: dict) -> None:
    """Optional file-based config with the same meaning as the env vars."""
    app = raw.get("app", {})
    s.title = app.get("title", s.title)
    s.subtitle = app.get("subtitle", s.subtitle)
    s.timezone = app.get("timezone", s.timezone)
    s.poll_interval_seconds = int(app.get("poll_interval_seconds", s.poll_interval_seconds))
    s.history_days = int(app.get("history_days", s.history_days))
    s.refresh_seconds = int(app.get("refresh_seconds", s.refresh_seconds))

    fr = raw.get("freshness", {})
    s.fresh_minutes = int(fr.get("fresh_minutes", s.fresh_minutes))
    s.stale_hours = int(fr.get("stale_hours", s.stale_hours))

    mp = raw.get("map", {})
    s.default_layer = mp.get("default_layer", s.default_layer)
    if "center" in mp:
        s.center = (float(mp["center"][0]), float(mp["center"][1]))
    s.zoom = int(mp.get("zoom", s.zoom))

    geo = raw.get("geocoding", {})
    s.geocoder = str(geo.get("provider", s.geocoder)).lower()
    s.geocoder_contact = geo.get("contact_email", s.geocoder_contact)

    al = raw.get("alerts", {})
    s.alerts_enabled = bool(al.get("enabled", s.alerts_enabled))
    s.alert_on_leave = bool(al.get("on_leave_home", s.alert_on_leave))
    s.alert_on_return = bool(al.get("on_return_home", s.alert_on_return))
    s.alert_on_stale = bool(al.get("on_stale", s.alert_on_stale))

    for z in raw.get("zones", []):
        s.zones.append(Zone(str(z["name"]), float(z["lat"]), float(z["lon"]),
                            float(z.get("radius_m", 400)), bool(z.get("home", True))))

    for i, t in enumerate(raw.get("trackers", [])):
        s.trackers.append(
            Tracker(
                match=str(t["match"]).strip(),
                label=str(t.get("label") or i + 1)[:6],
                name=str(t.get("name") or ""),
                color=str(t.get("color") or DEFAULT_COLORS[i % len(DEFAULT_COLORS)]),
                note=str(t.get("note", "")),
            )
        )


def load_settings() -> Settings:
    s = Settings()

    # ---- runtime + secrets ----
    s.config_path = Path(os.environ.get("KLEPTO_CONFIG", "/config/config.toml"))
    s.data_dir = Path(os.environ.get("KLEPTO_DATA_DIR", "/data"))
    s.port = int(os.environ.get("KLEPTO_PORT", "8787"))
    s.demo = _as_bool(os.environ.get("KLEPTO_DEMO", "0"))
    s.tile_email = _env("TILE_EMAIL") or ""
    s.tile_password = os.environ.get("TILE_PASSWORD", "")
    s.google_maps_js_key = _env("GOOGLE_MAPS_JS_KEY") or ""
    s.google_geocoding_key = _env("GOOGLE_GEOCODING_KEY") or ""
    s.apprise_urls = _split_urls(os.environ.get("APPRISE_URLS", ""))
    s.healthchecks_url = _env("HEALTHCHECKS_URL") or ""

    # ---- optional TOML file ----
    if s.config_path.exists():
        with s.config_path.open("rb") as fh:
            _apply_toml(s, tomllib.load(fh))
        log.info("Loaded %s (environment variables override it)", s.config_path)

    # ---- environment overrides ----
    str_vars = {"TITLE": "title", "SUBTITLE": "subtitle", "TIMEZONE": "timezone",
                "MAP_DEFAULT_LAYER": "default_layer", "GEOCODER": "geocoder",
                "GEOCODER_CONTACT": "geocoder_contact"}
    int_vars = {"POLL_INTERVAL_SECONDS": "poll_interval_seconds", "REFRESH_SECONDS": "refresh_seconds",
                "HISTORY_DAYS": "history_days", "FRESH_MINUTES": "fresh_minutes",
                "STALE_HOURS": "stale_hours", "MAP_ZOOM": "zoom"}
    bool_vars = {"ALERTS_ENABLED": "alerts_enabled", "ALERT_ON_LEAVE": "alert_on_leave",
                 "ALERT_ON_RETURN": "alert_on_return", "ALERT_ON_STALE": "alert_on_stale"}
    for env, attr in str_vars.items():
        if (v := _env(env)) is not None:
            setattr(s, attr, v)
    for env, attr in int_vars.items():
        if (v := _env(env)) is not None:
            try:
                setattr(s, attr, int(v))
            except ValueError:
                log.error("%s must be a whole number, got %r; keeping %s", env, v, getattr(s, attr))
    for env, attr in bool_vars.items():
        if (v := _env(env)) is not None:
            setattr(s, attr, _as_bool(v))
    if (v := _env("MAP_CENTER")) is not None:
        try:
            lat, lon = (float(x) for x in v.split(","))
            s.center = (lat, lon)
        except ValueError:
            log.error("MAP_CENTER must be 'lat,lon', got %r", v)
    if (v := _env("ZONES")) is not None:
        s.zones = parse_zones(v)
    elif s.demo and not s.zones:
        s.zones = list(DEMO_ZONES)
    env_trackers = _trackers_from_env()
    if env_trackers:
        s.trackers = env_trackers

    # ---- sanity ----
    # Floor the poll interval: Tile only changes when a phone passes the
    # tracker, so polling faster buys nothing and risks the account.
    s.poll_interval_seconds = max(60, s.poll_interval_seconds)
    s.refresh_seconds = max(10, s.refresh_seconds)
    s.geocoder = s.geocoder.lower()
    if s.default_layer not in LAYER_IDS:
        log.warning("Unknown MAP_DEFAULT_LAYER %r; using 'streets'", s.default_layer)
        s.default_layer = "streets"
    if s.geocoder == "google" and not s.google_geocoding_key:
        log.warning("GEOCODER=google but GOOGLE_GEOCODING_KEY is empty; using nominatim")
        s.geocoder = "nominatim"
    if s.geocoder not in {"nominatim", "google", "none"}:
        log.warning("Unknown GEOCODER %r; using nominatim", s.geocoder)
        s.geocoder = "nominatim"
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(s.timezone)
    except Exception:
        log.error("Unknown TIMEZONE %r; using UTC", s.timezone)
        s.timezone = "UTC"
    return s
