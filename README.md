# Klepto Cartographer

A self-hosted map that shows where each of your [Tile](https://www.tile.com/) Bluetooth trackers was last heard, how long ago, and whether it's inside the zones where it belongs. Use it for shared equipment, tools, bags, keys: anything with a Tile on it that a group of people needs to keep track of.

It runs as one small container. GitHub Actions publishes the image to GHCR, and every setting comes from environment variables, so it deploys as a Portainer stack or with plain `docker compose` and there are no config files to mount.

```
Tile cloud ──(pytile, every 3 min, backoff on errors)──► poller ──► SQLite (volume)
                                                           │            │
                                     zones + geocode cache ┘            ▼
Browser ◄── (optional) Cloudflare Access + tunnel ◄── klepto :8787  (/api/state, /api/history, /api/tiles)
   └─ Leaflet map: Google (GoogleMutant) → auto-fallback → CARTO/OSM/Esri
```

## Features

- **Map of last known positions:** one coloured pin per tracker, an accuracy circle, and pins that fan out when several trackers share a spot.
- **Freshness at a glance:** Fresh / Hours old / Stale states, relative times ("seen 12 min ago") and local timestamps.
- **Zones:** named circles such as "Office" or "Warehouse". Trackers outside every home zone are flagged **Away**.
- **History:** every distinct fix is stored in SQLite, so you get a 24 h trail per tracker and the map survives restarts and Tile outages.
- **Alerts (optional):** one message when a tracker leaves all home zones and one when it returns, plus one when it goes stale and one when it's heard again. Delivered through [Apprise](https://github.com/caronc/apprise/wiki) (ntfy, email, Slack, Discord, Pushover, Teams…).
- **Basemap that degrades gracefully:** Google Maps if you set a key, with an automatic switch to OpenStreetMap if Google rejects the key.
- **Robust polling:** fixed interval, exponential backoff with jitter on errors, automatic re-login, a `/healthz` endpoint and an optional Healthchecks ping.
- **Private by default:** once `TRACKER_n` is set, other Tiles on the account are never stored, named or shown, and leftovers from discovery mode are purged at startup. That makes it safe to use a personal Tile account.
- **Mobile-friendly UI** with a light/dark theme.

## Quick start (demo data, no Tile account)

```bash
docker run --rm -p 8787:8787 -e KLEPTO_DEMO=1 ghcr.io/shubniguroth/kleptocartography:latest
```

Open `http://localhost:8787`. Six simulated trackers appear around two demo zones; on the second poll the "Laptop bag" leaves and is flagged Away.

## Deploy

### 1. Let your Docker host pull the image

If the GHCR package is private, give Docker/Portainer a token:

1. GitHub → Settings → Developer settings → Personal access tokens → **Tokens (classic)** → scope **`read:packages`** only.
2. Portainer → Registries → Add registry → **Custom registry**: URL `ghcr.io`, Authentication on, your GitHub username, password = that token. For the CLI, use `docker login ghcr.io` instead.

If the package is public, skip this step.

### 2. Create the stack

**Portainer:** Stacks → Add stack → **Web editor** → paste `docker-compose.yml` → Environment variables → add them one by one, or **Load variables from .env file** with a filled-in copy of `stack.env.example` → Deploy. The compose file passes each variable explicitly (`TILE_EMAIL: ${TILE_EMAIL:-}` …), so it works in every Portainer environment type. After changing variables, use **Update the stack** so the container is recreated with them.

**Plain Docker:**

```bash
cp stack.env.example stack.env
docker compose --env-file stack.env up -d
```

Edit `stack.env` before the second command. To **update** later in Portainer: Stacks → klepto → Update the stack with **Re-pull image** on. With plain Docker:

```bash
docker compose --env-file stack.env pull
docker compose --env-file stack.env up -d
```

### 3. Pick your Tiles

The **name** is what each Tile is called in the Tile app (or the Life360 app). The **UUID** is Tile's internal ID; the apps don't show it, but Klepto can:

- **Discovery mode:** start with only `TILE_EMAIL` / `TILE_PASSWORD` set. The page shows every Tile on the account, and each card shows its UUID. `/api/tiles` lists the same data as JSON. Because this shows *everything* on the account, do it before you publish the page, then set `TRACKER_n`: the next start purges the rest.
- **Command line:** Portainer → Containers → `klepto` → Console, then run:

```bash
python -m klepto.tiles
```

From the Docker host itself:

```bash
docker exec -it klepto python -m klepto.tiles
```

Both print a table (name, UUID, kind, last seen, zone) followed by ready-to-paste `TRACKER_n=` lines. Then set `TRACKER_1`, `TRACKER_2`, … and update the stack.

Matching by **UUID** survives someone renaming a Tile in the app. Matching by **name** is easier to read. Either works, in any mix.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `TILE_EMAIL`, `TILE_PASSWORD` | required | Tile account that owns (or was shared) the Tiles |
| `TRACKER_1` … `TRACKER_n` | discovery mode | Tile name or UUID; one per tracker, any count, ordered by n |
| `TRACKER_n_LABEL` / `_NAME` / `_COLOR` / `_NOTE` | n / Tile's name / palette / blank | Pin text, panel name, colour, note |
| `ZONES` | none | `Name:lat,lon,radius_m[,away];…`. Zones are home unless `,away` is added. Right-click the map for coordinates |
| `GOOGLE_MAPS_JS_KEY` | blank (free maps) | Browser key for Google basemaps |
| `MAP_DEFAULT_LAYER` | `google_roadmap` | `google_roadmap`, `google_hybrid`, `streets`, `dark`, `satellite`, `osm` |
| `MAP_CENTER`, `MAP_ZOOM` | world view | View before the first fix (afterwards the map fits all trackers) |
| `GEOCODER`, `GOOGLE_GEOCODING_KEY`, `GEOCODER_CONTACT` | `nominatim` | Street addresses: `nominatim`, `google`, `none` |
| `POLL_INTERVAL_SECONDS` | 180 (min 60) | How often to ask the Tile cloud |
| `FRESH_MINUTES`, `STALE_HOURS` | 120, 24 | Fresh / Hours old / Stale thresholds |
| `ALERTS_ENABLED`, `APPRISE_URLS` | false, blank | Push alerts |
| `ALERT_ON_LEAVE`, `ALERT_ON_RETURN`, `ALERT_ON_STALE` | true | Which transitions notify |
| `HEALTHCHECKS_URL` | blank | Pinged after every poll; `/fail` with the error on failures |
| `CLOUDFLARE_TUNNEL_TOKEN` | blank | For the optional cloudflared service |
| `TITLE`, `SUBTITLE`, `TIMEZONE`, `TZ` | generic, UTC | Page text; timezone for alert messages |
| `REFRESH_SECONDS`, `HISTORY_DAYS` | 30, 120 | Browser refresh, history retention |
| `KLEPTO_DEMO` | 0 | `1` = simulated trackers, no Tile login needed |
| `KLEPTO_HOST_PORT` | 8787 | Host port |

An optional TOML file at `/config/config.toml` is also read (same settings), but environment variables override it.

## Publishing it (optional)

**Cloudflare Tunnel:** Zero Trust → Networks → Tunnels → Create tunnel. Put the token in `CLOUDFLARE_TUNNEL_TOKEN` and add a public hostname, e.g. `map.example.com` → `HTTP` → `klepto:8787`.

**Cloudflare Access** (recommended, since this is a map of where your stuff is): Zero Trust → Access → Applications → Self-hosted → your hostname.

- Policy: Allow, Include → *Emails ending in* `@yourcompany.com` (or a list of emails).
- Login method: One-time PIN, with a long session (e.g. 1 month).
- The page footer then shows "Signed in as …".

Any other reverse proxy works too. The app is plain HTTP on port 8787.

## Google Maps key (optional)

Without a key the map uses free OpenStreetMap-based layers. To use Google:

1. Google Cloud console → **Billing**: link a billing account to the project. Google requires this even inside the free monthly allowance; one page open counts as one map load.
2. APIs & Services → enable **Maps JavaScript API**.
3. Credentials → Create API key. Restrict Websites to `https://map.example.com/*`. Restrict APIs to Maps JavaScript API only.
4. Optionally lower the daily map-load quota and add a budget alert so it can never bill.
5. Set `GOOGLE_MAPS_JS_KEY`.

If Google ever rejects the key (billing lapsed, wrong referrer), the page switches to OpenStreetMap by itself and shows a banner instead of going dark.

## How accurate is this?

A Tile has no GPS. The pin is the GPS fix of whichever phone running Tile or Life360 last heard the tracker over Bluetooth, and the timestamp is when that happened. Expect building-level answers: "Office, seen 12 min ago". A Tile that sits where no phone passes stays stale; a spare phone running the Tile app, plugged in nearby, keeps it fresh.

## Local development

```bash
cd app
pip install -r ../requirements.txt
KLEPTO_DEMO=1 KLEPTO_DATA_DIR=/tmp/klepto python3 -m klepto
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `denied` / `unauthorized` pulling the image | Private package: add the GHCR registry with a `read:packages` token |
| "TILE_EMAIL / TILE_PASSWORD are not set inside the container" | The variables did not reach the container. Use the repo's `docker-compose.yml` (explicit `environment:`), then **Update the stack**. The log lists which variable names it can see |
| A password containing `$` fails to log in | Compose treats `$` as a variable; write it as `$$` in the stack variables |
| Red banner "Not found on the Tile account" | A `TRACKER_n` value doesn't match; the banner and `/api/tiles` list what exists |
| "Tile polling is failing … InvalidAuthError" | Wrong `TILE_EMAIL` / `TILE_PASSWORD`; Klepto backs off up to 30 min between attempts |
| Amber "Google Maps unavailable" | Key, billing or referrer issue; the map keeps working on OpenStreetMap |
| Actions run fails at `secret-guard` | A secret was committed: remove it, rotate it, push again |

## How it ships

Every push to `main` runs `.github/workflows/publish.yml`. The `secret-guard` job runs first and fails the run if a committed file contains a Google API key, a filled-in secret variable, or a token. If it passes, the workflow builds `linux/amd64` + `linux/arm64` and pushes `ghcr.io/shubniguroth/kleptocartography:latest` and `:sha-xxxxxxx`, plus `:X.Y.Z` for `vX.Y.Z` tags. `stack.env` and `.env` are git-ignored.

## Layout

```
.github/workflows/publish.yml   secret guard + multi-arch build → GHCR
docker-compose.yml              stack (klepto + optional cloudflared, named volume, explicit env)
stack.env.example               every variable, blank
Dockerfile, requirements.txt
app/klepto/__main__.py          wiring + graceful shutdown
app/klepto/config.py            env vars (optional TOML underneath)
app/klepto/tiles.py             `python -m klepto.tiles` lister
app/klepto/sources.py           TileSource (pytile) and DemoSource
app/klepto/poller.py            poll loop, backoff, Healthchecks ping
app/klepto/geo.py               zones, Nominatim/Google reverse geocoding + cache
app/klepto/alerts.py            edge-triggered alerts via Apprise
app/klepto/store.py             SQLite schema and queries
app/klepto/web.py               HTTP API + static files
app/klepto/static/              map UI, vendored Leaflet 1.9.4 + GoogleMutant 0.16
```

Klepto uses [pytile](https://github.com/bachya/pytile), which talks to an unofficial Tile API. If Tile changes that API, only `sources.py` should need updating. Not affiliated with Tile or Life360.
