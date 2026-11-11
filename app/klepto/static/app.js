/*
 * Klepto Cartographer frontend.
 *
 * Basemap strategy (the fix for "This page can't load Google Maps correctly"):
 *   - If the server provides a Google Maps JS key, Google Maps is rendered
 *     inside Leaflet via GoogleMutant (official Maps JS API underneath, so it
 *     is Google-ToS compliant and shows Google's own logo/attribution).
 *   - If Google rejects the key (billing off, referrer not allowed, API
 *     disabled), gm_authFailure fires or the error overlay appears; we then
 *     swap to a free OpenStreetMap-based layer automatically and say so.
 *   - Without a key, free layers are used from the start.
 * Markers, trails and zones are Leaflet objects, so they work identically
 * on every basemap.
 *
 * Data: GET /api/config once, then GET /api/state every refresh_seconds.
 */
(() => {
  "use strict";

  // ------------------------------------------------------------ helpers
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const ls = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };
  const isDark = () => window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches;
  const isMobile = () => window.matchMedia("(max-width: 720px)").matches;

  let CFG = null;
  let skew = 0; // server_now - client_now, seconds
  const nowS = () => Date.now() / 1000 + skew;

  function ago(sec) {
    if (sec == null) return "";
    if (sec < 60) return "just now";
    if (sec < 3600) return `${Math.round(sec / 60)} min ago`;
    if (sec < 86400) {
      const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
      return h < 6 && m ? `${h} h ${m} min ago` : `${h} h ago`;
    }
    const d = Math.floor(sec / 86400), h = Math.round((sec % 86400) / 3600);
    return h ? `${d} d ${h} h ago` : `${d} d ago`;
  }
  const fmtShort = new Intl.DateTimeFormat(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" });
  const fmtLong = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const fmtClock = (ts) => (nowS() - ts < 6 * 86400 ? fmtShort : fmtLong).format(new Date(ts * 1000));

  const STATUS_TEXT = {
    fresh: "Fresh", aging: "Hours old", stale: "Stale", nofix: "No fix yet", missing: "Not on account",
  };
  const gmapsUrl = (it) => `https://www.google.com/maps/search/?api=1&query=${it.lat},${it.lon}`;

  // ------------------------------------------------------------ map setup
  let map, layerCtl, zonesLayer, accLayer, leadersLayer, trailLayer, markerLayer;
  const free = {};
  const gLayers = {};  // Google basemaps (named to avoid shadowing window.google)
  let googleDead = false;
  const LABELS = {
    google_roadmap: "Google Maps", google_hybrid: "Google Satellite",
    streets: "Streets", dark: "Dark", satellite: "Satellite (Esri)", osm: "OpenStreetMap",
  };

  function makeFreeLayers() {
    const osmAttr = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';
    free.streets = L.tileLayer("https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
      { subdomains: "abcd", maxZoom: 20, attribution: `${osmAttr} &copy; <a href="https://carto.com/attributions">CARTO</a>` });
    free.dark = L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
      { subdomains: "abcd", maxZoom: 20, attribution: `${osmAttr} &copy; <a href="https://carto.com/attributions">CARTO</a>` });
    free.satellite = L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 19, attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics" });
    free.osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: osmAttr });
  }

  function fallbackId() { return isDark() ? "dark" : "streets"; }

  function initMap() {
    map = L.map("map", {
      center: CFG.map.center, zoom: CFG.map.zoom, zoomControl: false,
      maxZoom: 20, worldCopyJump: true, attributionControl: true,
    });
    L.control.zoom({ position: "topright" }).addTo(map);
    makeFreeLayers();

    zonesLayer = L.layerGroup().addTo(map);
    accLayer = L.layerGroup().addTo(map);
    trailLayer = L.layerGroup().addTo(map);
    leadersLayer = L.layerGroup().addTo(map);
    markerLayer = L.layerGroup().addTo(map);

    const bases = {};
    for (const id of ["streets", "dark", "satellite", "osm"]) bases[LABELS[id]] = free[id];
    layerCtl = L.control.layers(bases, { "Zones": zonesLayer, "Accuracy circles": accLayer },
      { position: "topright", collapsed: true }).addTo(map);

    // "Fit all" control
    const Fit = L.Control.extend({
      options: { position: "topright" },
      onAdd() {
        const div = L.DomUtil.create("div", "leaflet-bar k-ctl");
        div.innerHTML = '<a href="#" role="button" title="Show all trackers">Fit</a>';
        L.DomEvent.on(div.firstChild, "click", (e) => { L.DomEvent.preventDefault(e); fitAll(true); });
        L.DomEvent.disableClickPropagation(div);
        return div;
      },
    });
    new Fit().addTo(map);

    map.on("baselayerchange", (e) => {
      const id = Object.keys(LABELS).find((k) => LABELS[k] === e.name);
      if (id) ls.set("klepto.layer", id);
    });
    map.on("zoomend", layoutMarkers);
    map.on("contextmenu", (e) => {
      L.popup().setLatLng(e.latlng).setContent(
        `<div class="coord">${e.latlng.lat.toFixed(5)}, ${e.latlng.lng.toFixed(5)}</div>
         <small>Zone centre for ZONES</small>`).openOn(map);
    });

    drawZones();
    chooseBasemap();
  }

  function chooseBasemap() {
    const key = CFG.map.google_maps_js_key;
    let want = ls.get("klepto.layer") || CFG.map.default_layer;
    if (!LABELS[want]) want = CFG.map.default_layer;
    if (want.startsWith("google") && !key) want = fallbackId();

    if (!key) { free[want].addTo(map); return; }

    // Start Google loading. Free layer only appears if Google fails or if the
    // user preferred a free layer.
    if (!want.startsWith("google")) free[want].addTo(map);
    loadGoogle(key).then(() => {
      if (googleDead) return;
      gLayers.google_roadmap = L.gridLayer.googleMutant({ type: "roadmap", maxZoom: 21 });
      gLayers.google_hybrid = L.gridLayer.googleMutant({ type: "hybrid", maxZoom: 21 });
      layerCtl.addBaseLayer(gLayers.google_roadmap, LABELS.google_roadmap);
      layerCtl.addBaseLayer(gLayers.google_hybrid, LABELS.google_hybrid);
      if (want.startsWith("google")) {
        const layer = gLayers[want];
        layer.addTo(map);
        layer.on("spawned", () => setTimeout(checkGoogleOverlay, 3500));
      }
    }).catch((err) => googleFailed(`Google Maps did not load (${err.message}).`));
  }

  function loadGoogle(key) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("timed out")), 12000);
      // Google calls this global on key/billing/referrer failures.
      window.gm_authFailure = () => googleFailed(
        "Google rejected the Maps API key (billing disabled, referrer not allowed, or API not enabled).");
      window.__kleptoGmReady = async () => {
        clearTimeout(timer);
        try {
          if (window.google?.maps?.importLibrary) await window.google.maps.importLibrary("maps");
          resolve();
        } catch (e) { reject(e); }
      };
      const s = document.createElement("script");
      s.src = "https://maps.googleapis.com/maps/api/js?key=" + encodeURIComponent(key) +
        "&v=weekly&loading=async&callback=__kleptoGmReady";
      s.async = true;
      s.onerror = () => { clearTimeout(timer); reject(new Error("script blocked or offline")); };
      document.head.appendChild(s);
    });
  }

  // Belt and braces: some failures only show Google's error overlay.
  function checkGoogleOverlay() {
    if (document.querySelector(".gm-err-container, .gm-err-message, .dismissButton")) {
      googleFailed("Google Maps showed its 'can't load correctly' error.");
    }
  }

  function googleFailed(reason) {
    if (googleDead) return;
    googleDead = true;
    console.warn("[klepto] Google basemap disabled:", reason);
    let wasActive = false;
    for (const layer of Object.values(gLayers)) {
      if (map.hasLayer(layer)) { wasActive = true; map.removeLayer(layer); }
      layerCtl.removeLayer(layer);
    }
    const anyFree = Object.values(free).some((l) => map.hasLayer(l));
    if (wasActive || !anyFree) free[fallbackId()].addTo(map);
    banners.google = { cls: "warn", html: `<b>Google Maps unavailable.</b> ${esc(reason)} Showing OpenStreetMap instead; positions are unaffected.` };
    renderBanners();
  }

  function drawZones() {
    zonesLayer.clearLayers();
    for (const z of CFG.zones) {
      L.circle([z.lat, z.lon], {
        radius: z.radius_m, color: z.home ? "#4b5563" : "#9ca3af", weight: 1.5,
        dashArray: "6 6", fillOpacity: 0.035, interactive: false,
      }).addTo(zonesLayer);
      // Label on the southern edge: pins rise upward, so the bottom stays clear.
      const bottom = [z.lat - z.radius_m / 111320, z.lon];
      L.marker(bottom, {
        interactive: false, keyboard: false,
        icon: L.divIcon({ className: "", html: `<div class="zone-label">${esc(z.name)}</div>`, iconSize: [0, 0] }),
      }).addTo(zonesLayer);
    }
  }

  // ------------------------------------------------------------ state
  let items = [];
  const markers = new Map();   // key -> L.marker
  const trails = new Map();    // key -> L.layerGroup
  let selected = null;
  let firstFit = true;
  const banners = {};

  async function refresh() {
    let data;
    try {
      const r = await fetch("/api/state", { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      data = await r.json();
    } catch (err) {
      setPoll("err", `Cannot reach the Klepto server (${err.message}). Retrying…`);
      return;
    }
    skew = data.now - Date.now() / 1000;
    items = data.items;
    renderPoll(data.poll);
    renderSetupBanners(data);
    renderSummary();
    renderList();
    renderMarkers();
    renderEvents(data.events);
    if (firstFit) { firstFit = false; fitAll(false); }
  }

  function setPoll(cls, text, title) {
    const bar = $("#pollbar");
    bar.className = `pollbar ${cls}`;
    $("#polltext").textContent = text;
    bar.title = title || "";
  }

  function renderPoll(p) {
    const n = nowS();
    // Persistent failures get a readable banner with the real error text
    // (bad password, Tile API change, DNS...). One transient blip does not.
    delete banners.poll;
    if (p.last_error && (p.consecutive_failures >= 2 || !p.last_ok)) {
      const wait = p.next_poll ? p.next_poll - n : null;
      const next = wait == null ? "" : wait <= 60 ? " Retrying within a minute." : ` Next try in ${Math.round(wait / 60)} min.`;
      banners.poll = {
        cls: p.consecutive_failures >= 3 ? "err" : "warn",
        html: `<b>Tile polling is failing</b> (${p.consecutive_failures}×). <code>${esc(p.last_error)}</code>` +
          `${p.last_ok ? " Showing last known positions." : ""}${next}`,
      };
    }
    const every = Math.round(p.interval / 60);
    if (p.last_error && p.consecutive_failures > 0) {
      const since = p.last_error_at ? ago(n - p.last_error_at) : "";
      const lastOk = p.last_ok ? `last good poll ${ago(n - p.last_ok)}` : "no successful poll yet";
      setPoll(p.consecutive_failures >= 3 ? "err" : "warn",
        `Tile API error ${since} · ${lastOk}`, p.last_error);
    } else if (p.last_ok) {
      setPoll("ok", `Tile checked ${ago(n - p.last_ok)} · every ${every} min`);
    } else {
      setPoll("", "Waiting for first Tile poll…");
    }
  }

  function renderSetupBanners(data) {
    delete banners.demo; delete banners.disc; delete banners.missing;
    if (CFG.demo) banners.demo = { cls: "", html: "<b>Demo mode.</b> Simulated trackers; set <code>KLEPTO_DEMO=0</code> for real data." };
    if (CFG.discovery) banners.disc = { cls: "warn", html: "<b>Discovery mode.</b> Showing every Tile on the account. Set <code>TRACKER_1</code>, <code>TRACKER_2</code>… to the Tiles you want on the map (names and UUIDs at <a href=\"/api/tiles\" target=\"_blank\">/api/tiles</a>)." };
    if (data.missing_trackers?.length) {
      const names = data.missing_trackers.map((m) => `<code>${esc(m)}</code>`).join(", ");
      const avail = (data.unmatched_tiles || []).map((t) => `<code>${esc(t.name)}</code>`).join(", ");
      banners.missing = { cls: "err", html: `<b>Not found on the Tile account:</b> ${names}.${avail ? ` Unassigned Tiles: ${avail}.` : ""}` };
    }
    renderBanners();
  }

  function renderBanners() {
    $("#banners").innerHTML = Object.values(banners).map((b) => `<div class="banner ${b.cls}">${b.html}</div>`).join("");
  }

  function renderSummary() {
    const c = { fresh: 0, aging: 0, stale: 0, away: 0, nofix: 0 };
    for (const it of items) {
      if (it.status === "missing" || it.status === "nofix") c.nofix++;
      else c[it.status]++;
      if (it.home === false) c.away++;
    }
    const chips = [`<span class="chip">${items.length} tracked</span>`];
    if (c.fresh) chips.push(`<span class="chip fresh">${c.fresh} fresh</span>`);
    if (c.aging) chips.push(`<span class="chip aging">${c.aging} seen hours ago</span>`);
    if (c.stale) chips.push(`<span class="chip stale">${c.stale} stale</span>`);
    if (c.away) chips.push(`<span class="chip away">${c.away} away</span>`);
    if (c.nofix) chips.push(`<span class="chip">${c.nofix} no fix</span>`);
    $("#summary").innerHTML = chips.join("");
  }

  function placeHtml(it) {
    if (it.status === "missing") return `No Tile named <code>${esc(it.key)}</code>; see <a href="/api/tiles" target="_blank">/api/tiles</a>`;
    if (it.lat == null) return "Waiting for a phone to hear it";
    const zone = it.zone ? `<b>${esc(it.zone)}</b>` : (it.home === false ? "<b>Away</b>" : "");
    const addr = it.address ? esc(it.address) : `${it.lat.toFixed(5)}, ${it.lon.toFixed(5)}`;
    return zone ? `${zone} · ${addr}` : addr;
  }

  function timeText(it) {
    if (it.ts == null) return "";
    const acc = it.accuracy != null ? ` · ±${Math.round(it.accuracy)} m` : "";
    return `Seen ${ago(nowS() - it.ts)} · ${fmtClock(it.ts)}${acc}`;
  }

  function renderList() {
    const ul = $("#list");
    if (!items.length) {
      ul.innerHTML = '<li class="empty">No trackers yet. They appear after the first successful Tile poll.</li>';
      return;
    }
    ul.innerHTML = items.map((it) => {
      const away = it.home === false ? '<span class="pill away">Away</span>' : "";
      const lost = it.lost && it.lat != null ? '<span class="pill">Lost mode</span>' : "";
      const hasFix = it.lat != null;
      return `
      <li class="card st-${it.status} ${selected === it.key ? "selected" : ""}" data-key="${esc(it.key)}" style="--c:${esc(it.color)}">
        <button class="card-main" type="button" ${hasFix ? "" : "disabled"}>
          <span class="badge">${esc(it.label)}</span>
          <span class="card-body">
            <span class="card-title">${esc(it.name)} <span class="pill st-${it.status}">${STATUS_TEXT[it.status]}</span>${away}${lost}</span>
            <span class="card-place">${placeHtml(it)}</span>
            <span class="card-time" data-ts="${it.ts ?? ""}">${timeText(it)}</span>
            ${it.note ? `<span class="card-note">${esc(it.note)}</span>` : ""}
          </span>
        </button>
        ${hasFix ? `<div class="card-actions">
          <button type="button" data-act="trail" aria-pressed="${trails.has(it.key)}">Trail 24 h</button>
          <a href="${gmapsUrl(it)}" target="_blank" rel="noopener">Google Maps ↗</a>
        </div>` : ""}
      </li>`;
    }).join("");
  }

  // Click handling for the list (event delegation survives re-renders).
  function onListClick(e) {
    const li = e.target.closest(".card");
    if (!li) return;
    const it = items.find((x) => x.key === li.dataset.key);
    if (!it || it.lat == null) return;
    if (e.target.closest('[data-act="trail"]')) { toggleTrail(it); return; }
    if (e.target.closest(".card-main")) select(it.key, true);
  }

  function tickRelative() {
    document.querySelectorAll(".card-time[data-ts]").forEach((el) => {
      const it = items.find((x) => String(x.ts) === el.dataset.ts);
      if (it) el.textContent = timeText(it);
    });
  }

  function renderEvents(events) {
    $("#events").innerHTML = (events || []).map((e) =>
      `<li class="${e.kind.endsWith("_on") ? "on" : ""}"><time>${fmtClock(e.ts)}</time>${esc(e.message)}</li>`
    ).join("") || "<li>No events yet.</li>";
  }

  // ------------------------------------------------------------ markers
  function pinIcon(it) {
    const cls = ["k-pin", `st-${it.status}`, it.home === false ? "away" : "", selected === it.key ? "is-selected" : ""].join(" ");
    return L.divIcon({
      className: "k-pin-wrap", iconSize: [0, 0], iconAnchor: [0, 0], popupAnchor: [0, -38],
      html: `<div class="${cls}" style="--c:${esc(it.color)}">${esc(it.label)}</div>`,
    });
  }

  function popupHtml(it) {
    return `<div class="pop">
      <div class="pop-h" style="--c:${esc(it.color)}"><span class="sw"></span><b>${esc(it.label)}</b> ${esc(it.name)}</div>
      <div class="pop-meta">${placeHtml(it)}</div>
      <div class="pop-meta">${esc(timeText(it))}</div>
      <a href="${gmapsUrl(it)}" target="_blank" rel="noopener">Open in Google Maps ↗</a>
    </div>`;
  }

  function renderMarkers() {
    accLayer.clearLayers();
    const live = new Set();
    for (const it of items) {
      if (it.lat == null) continue;
      live.add(it.key);
      const ll = [it.lat, it.lon];
      let m = markers.get(it.key);
      if (!m) {
        m = L.marker(ll, { icon: pinIcon(it), riseOnHover: true, keyboard: true, title: it.name });
        m.on("click", () => select(it.key, false));
        m.addTo(markerLayer);
        markers.set(it.key, m);
      }
      m._true = L.latLng(ll);
      m._item = it;
      m.setIcon(pinIcon(it));
      m.setZIndexOffset(selected === it.key ? 1000 : { fresh: 300, aging: 200, stale: 100 }[it.status] || 0);
      if (m.isPopupOpen()) m.setPopupContent(popupHtml(it));
      if (it.accuracy) {
        L.circle(ll, { radius: it.accuracy, color: it.color, weight: 1, opacity: 0.5, fillOpacity: 0.07, interactive: false })
          .addTo(accLayer);
      }
    }
    for (const [k, m] of markers) if (!live.has(k)) { markerLayer.removeLayer(m); markers.delete(k); }
    layoutMarkers();
  }

  /*
   * Overlap fan-out. Inside one building, several trackers often share the
   * same fix (the old map hid four pins under one). Pins closer than ~34 px
   * on screen are fanned out around their centroid, with a hairline back to
   * the true position. Recomputed on every zoom.
   */
  function layoutMarkers() {
    leadersLayer.clearLayers();
    const entries = [...markers.values()].map((m) => ({ m, p: map.latLngToLayerPoint(m._true) }));
    const groups = [];
    for (const e of entries) {
      const g = groups.find((g) => g.c.distanceTo(e.p) < 34);
      if (g) {
        g.list.push(e);
        g.c = g.list.reduce((a, x) => a.add(x.p), L.point(0, 0)).divideBy(g.list.length);
      } else groups.push({ c: e.p, list: [e] });
    }
    for (const g of groups) {
      if (g.list.length === 1) { g.list[0].m.setLatLng(g.list[0].m._true); continue; }
      // Pins extend upward from their anchor, so fan them along an upper arc
      // (a full ring for big groups) and size the radius so pins never touch.
      const n = g.list.length;
      const arc = n <= 6 ? (160 * Math.PI) / 180 : 2 * Math.PI;
      const step = n <= 6 ? arc / (n - 1) : arc / n;
      const r = Math.max(36, 52 / (2 * Math.sin(Math.min(step, Math.PI) / 2)));
      const start = n <= 6 ? -Math.PI / 2 - arc / 2 : -Math.PI / 2;
      g.list.sort((a, b) => String(a.m._item.label).localeCompare(String(b.m._item.label), undefined, { numeric: true }));
      g.list.forEach((e, i) => {
        const ang = start + step * i;
        const pos = map.layerPointToLatLng(g.c.add(L.point(Math.cos(ang) * r, Math.sin(ang) * r)));
        e.m.setLatLng(pos);
        L.polyline([e.m._true, pos], { color: e.m._item.color, weight: 1.5, opacity: 0.8, interactive: false }).addTo(leadersLayer);
        L.circleMarker(e.m._true, { radius: 3.5, color: "#fff", weight: 1.5, fillColor: e.m._item.color, fillOpacity: 1, interactive: false })
          .addTo(leadersLayer);
      });
    }
  }

  function select(key, fly) {
    selected = key;
    const it = items.find((x) => x.key === key);
    const m = markers.get(key);
    renderList();
    renderMarkers();
    if (!it || !m) return;
    const target = L.latLng(it.lat, it.lon);
    const open = () => m.bindPopup(popupHtml(it), { autoPanPaddingTopLeft: panelPad() }).openPopup();
    if (fly) {
      map.once("moveend", open);
      map.flyTo(target, Math.max(map.getZoom(), 17), { duration: 0.6 });
    } else open();
    document.querySelector(`.card[data-key="${CSS.escape(key)}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function panelPad() {
    const panel = $("#panel");
    if (isMobile() || panel.classList.contains("collapsed")) return L.point(20, 20);
    return L.point(panel.getBoundingClientRect().right + 16, 20);
  }

  function fitAll(animate) {
    const pts = items.filter((it) => it.lat != null).map((it) => [it.lat, it.lon]);
    if (!pts.length) return;
    const opts = { paddingTopLeft: panelPad(), paddingBottomRight: isMobile() ? L.point(64, window.innerHeight * 0.5 + 20) : L.point(64, 40), maxZoom: 17, animate };
    if (pts.length === 1) map.setView(pts[0], 17, { animate });
    else map.fitBounds(L.latLngBounds(pts), opts);
  }

  async function toggleTrail(it) {
    if (trails.has(it.key)) {
      trailLayer.removeLayer(trails.get(it.key));
      trails.delete(it.key);
      renderList();
      return;
    }
    const r = await fetch(`/api/history?uuid=${encodeURIComponent(it.uuid)}&hours=24`, { cache: "no-store" });
    if (!r.ok) return;
    const pts = await r.json();
    const grp = L.layerGroup();
    if (pts.length > 1) {
      L.polyline(pts.map((p) => [p.lat, p.lon]), { color: it.color, weight: 3, opacity: 0.75, dashArray: "2 7", lineCap: "round" }).addTo(grp);
    }
    for (const p of pts) {
      L.circleMarker([p.lat, p.lon], { radius: 4, color: "#fff", weight: 1.5, fillColor: it.color, fillOpacity: 0.95 })
        .bindTooltip(`${esc(it.label)} · ${fmtClock(p.ts)}${p.zone ? " · " + esc(p.zone) : ""}`, { direction: "top" })
        .addTo(grp);
    }
    grp.addTo(trailLayer);
    trails.set(it.key, grp);
    renderList();
    if (pts.length > 1) map.fitBounds(L.latLngBounds(pts.map((p) => [p.lat, p.lon])), { paddingTopLeft: panelPad(), maxZoom: 17 });
  }

  // ------------------------------------------------------------ boot
  async function boot() {
    try {
      CFG = await (await fetch("/api/config", { cache: "no-store" })).json();
    } catch (err) {
      setPoll("err", "Cannot reach the Klepto server.");
      setTimeout(boot, 5000);
      return;
    }
    document.title = CFG.title;
    $("#title").textContent = CFG.title;
    $("#subtitle").textContent = CFG.subtitle || "";
    $("#foot").innerHTML =
      "Each pin is where a phone running Tile or Life360 last heard that tracker; expect building-level accuracy. " +
      "Right-click (or long-press) the map for coordinates." +
      (CFG.viewer ? `<br>Signed in as ${esc(CFG.viewer)}` : "");

    if (isMobile() && ls.get("klepto.collapsed") === "1") $("#panel").classList.add("collapsed");
    $("#collapse").addEventListener("click", () => {
      const p = $("#panel");
      p.classList.toggle("collapsed");
      ls.set("klepto.collapsed", p.classList.contains("collapsed") ? "1" : "0");
    });
    $("#list").addEventListener("click", onListClick);
    $("#refresh").addEventListener("click", async () => {
      const btn = $("#refresh");
      btn.disabled = true;
      try { await fetch("/api/poll", { method: "POST" }); } catch { /* ignore */ }
      setTimeout(async () => { await refresh(); btn.disabled = false; }, 4000);
    });

    initMap();
    await refresh();
    setInterval(refresh, CFG.refresh_seconds * 1000);
    setInterval(tickRelative, 15000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  }

  boot();
})();
