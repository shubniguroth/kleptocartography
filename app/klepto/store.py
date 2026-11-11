"""
SQLite persistence.

Why a database at all, when the old script only drew the latest point?
  * History: "where has tracker 3 been since Monday?" becomes answerable, and the
    map can draw a trail.
  * Survives restarts: after a container restart or a Tile outage the page
    still shows the last known position instead of an empty map.
  * Alert state: we only alert on transitions (left home zones / came back), so
    we need to remember the previous state.

One file at /data/klepto.sqlite3, WAL mode so the web handlers can read while
the poller writes. All calls are tiny and synchronous; they run on the event
loop thread, which is fine at this scale (5 trackers, one write per poll).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- One row per Tile ever seen on the account (tracker or not).
CREATE TABLE IF NOT EXISTS tiles (
    uuid        TEXT PRIMARY KEY,
    tile_name   TEXT NOT NULL,
    kind        TEXT,
    archetype   TEXT,
    firmware    TEXT,
    hardware    TEXT,
    lost        INTEGER,
    dead        INTEGER,
    first_seen  INTEGER NOT NULL,
    last_polled INTEGER NOT NULL
);

-- One row per distinct Tile fix. The Tile cloud only produces a new fix when
-- a phone passes the tracker, so (uuid, ts) de-duplicates repeat polls of the
-- same fix and the table stays small.
CREATE TABLE IF NOT EXISTS observations (
    uuid      TEXT    NOT NULL,
    ts        INTEGER NOT NULL,   -- when the Tile was seen (epoch s, UTC)
    lat       REAL    NOT NULL,
    lon       REAL    NOT NULL,
    accuracy  REAL,               -- metres, as reported by the finding phone
    altitude  REAL,
    zone      TEXT,               -- matched zone name, if inside one
    address   TEXT,               -- reverse-geocoded street address
    polled_at INTEGER NOT NULL,   -- when we fetched it
    PRIMARY KEY (uuid, ts)
);
CREATE INDEX IF NOT EXISTS obs_uuid_ts ON observations(uuid, ts DESC);

-- Reverse geocode cache keyed by coordinates rounded to ~11 m.
CREATE TABLE IF NOT EXISTS geocache (
    key        TEXT PRIMARY KEY,
    address    TEXT,
    provider   TEXT,
    fetched_at INTEGER NOT NULL
);

-- Alert state machine: one row per (tile, kind) remembering whether the
-- condition is currently active, so we only notify on edges.
CREATE TABLE IF NOT EXISTS alert_state (
    uuid   TEXT NOT NULL,
    kind   TEXT NOT NULL,
    active INTEGER NOT NULL,
    since  INTEGER NOT NULL,
    PRIMARY KEY (uuid, kind)
);

-- Human-readable event log (also shown in the UI).
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid       TEXT,
    kind       TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    delivered  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_created ON events(created_at DESC);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    # ------------------------------------------------------------------ tiles
    def upsert_tile(self, t: dict[str, Any]) -> None:
        now = int(time.time())
        with self._lock:
            self._db.execute(
                """
                INSERT INTO tiles (uuid, tile_name, kind, archetype, firmware, hardware,
                                   lost, dead, first_seen, last_polled)
                VALUES (:uuid, :name, :kind, :archetype, :firmware, :hardware,
                        :lost, :dead, :now, :now)
                ON CONFLICT(uuid) DO UPDATE SET
                    tile_name = excluded.tile_name,
                    kind = excluded.kind,
                    archetype = excluded.archetype,
                    firmware = excluded.firmware,
                    hardware = excluded.hardware,
                    lost = excluded.lost,
                    dead = excluded.dead,
                    last_polled = excluded.last_polled
                """,
                {**t, "now": now, "lost": int(bool(t.get("lost"))), "dead": int(bool(t.get("dead")))},
            )

    def tiles(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute("SELECT * FROM tiles ORDER BY tile_name"))

    # ----------------------------------------------------------- observations
    def insert_observation(self, o: dict[str, Any]) -> bool:
        """Insert a fix. Returns True if it was new (not a repeat poll)."""
        with self._lock:
            cur = self._db.execute(
                """
                INSERT OR IGNORE INTO observations
                    (uuid, ts, lat, lon, accuracy, altitude, zone, address, polled_at)
                VALUES (:uuid, :ts, :lat, :lon, :accuracy, :altitude, :zone, :address, :polled_at)
                """,
                o,
            )
            return cur.rowcount == 1

    def latest(self, uuid: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM observations WHERE uuid = ? ORDER BY ts DESC LIMIT 1", (uuid,)
            ).fetchone()

    def history(self, uuid: str, since_ts: int, limit: int = 2000) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._db.execute(
                    """SELECT ts, lat, lon, accuracy, zone, address FROM observations
                       WHERE uuid = ? AND ts >= ? ORDER BY ts ASC LIMIT ?""",
                    (uuid, since_ts, limit),
                )
            )

    def prune(self, older_than_ts: int) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM observations WHERE ts < ?", (older_than_ts,))
            self._db.execute("DELETE FROM events WHERE created_at < ?", (older_than_ts,))
            return cur.rowcount

    # --------------------------------------------------------------- geocache
    def geocache_get(self, key: str, max_age_s: int) -> str | None | bool:
        """Returns the cached address, None if cached-as-unknown, or False on miss."""
        with self._lock:
            row = self._db.execute(
                "SELECT address, fetched_at FROM geocache WHERE key = ?", (key,)
            ).fetchone()
        if row is None or time.time() - row["fetched_at"] > max_age_s:
            return False
        return row["address"]

    def geocache_put(self, key: str, address: str | None, provider: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO geocache (key, address, provider, fetched_at) VALUES (?,?,?,?)",
                (key, address, provider, int(time.time())),
            )

    # ------------------------------------------------------------ alert state
    def alert_get(self, uuid: str, kind: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM alert_state WHERE uuid = ? AND kind = ?", (uuid, kind)
            ).fetchone()

    def alert_set(self, uuid: str, kind: str, active: bool, since: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO alert_state (uuid, kind, active, since) VALUES (?,?,?,?)",
                (uuid, kind, int(active), since),
            )

    # ----------------------------------------------------------------- events
    def add_event(self, uuid: str | None, kind: str, message: str) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events (uuid, kind, message, created_at) VALUES (?,?,?,?)",
                (uuid, kind, message, int(time.time())),
            )
            return int(cur.lastrowid)

    def mark_delivered(self, event_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE events SET delivered = 1 WHERE id = ?", (event_id,))

    def recent_events(self, limit: int = 30) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._db.execute(
                    "SELECT * FROM events ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
                )
            )

    # --------------------------------------------------------------------- kv
    def kv_get(self, k: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
        return json.loads(row["v"]) if row else default

    def kv_set(self, k: str, v: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, json.dumps(v))
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()
