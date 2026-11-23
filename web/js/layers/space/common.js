// Space module: shared helpers for the browser code (colours, geodesy, time, raster ordering).

export const COLOR = {
  space: '#a08ff5', weather: '#86d6f7', imagery: '#9cc29a', ink: '#111a2c', paper: '#ebe6d6',
  red: '#ff5d52', amber: '#ffb547', green: '#5fd38d', blue: '#6cb6ff',
};

// Satellite families: three categorical hues validated all-pairs for colour-vision deficiency on the
// dark map (dataviz validator, dark mode, surface #111a2c), plus chart-paper for crewed stations
// (which are also labelled, so colour never carries identity alone).
export const FAMILY = {
  station: { color: '#ebe6d6', label: 'Crewed and space stations', rank: 3 },
  nav: { color: '#d95926', label: 'Navigation', rank: 2 },
  earth: { color: '#199e70', label: 'Earth observation and science', rank: 1 },
  general: { color: '#9085e9', label: 'Other satellites', rank: 0 },
};
export const FAMILY_IDS = ['station', 'nav', 'earth', 'general'];

export const R_EARTH = 6371.0088;
const D2R = Math.PI / 180;
const R2D = 180 / Math.PI;

export function clamp(x, lo, hi) { return Math.max(lo, Math.min(hi, x)); }

export function destination(lat, lon, bearingDeg, distKm) {
  const d = distKm / R_EARTH;
  const b = bearingDeg * D2R;
  const p1 = lat * D2R;
  const l1 = lon * D2R;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [((l2 * R2D + 540) % 360) - 180, p2 * R2D];
}

/** Make a run of longitudes continuous (no jumps over 180°), keeping index `anchor` where it is. */
export function unwrapLons(coords, anchor = 0) {
  const out = coords.map(c => [c[0], c[1]]);
  for (let i = anchor + 1; i < out.length; i++) {
    let l = out[i][0];
    while (l - out[i - 1][0] > 180) l -= 360;
    while (l - out[i - 1][0] < -180) l += 360;
    out[i][0] = l;
  }
  for (let i = anchor - 1; i >= 0; i--) {
    let l = out[i][0];
    while (l - out[i + 1][0] > 180) l -= 360;
    while (l - out[i + 1][0] < -180) l += 360;
    out[i][0] = l;
  }
  return out;
}

/** Visibility circle on the ground as a GeoJSON polygon that renders on Mercator and the globe,
 *  including circles that cross the antimeridian or contain a pole. */
export function footprintPolygon(lat, lon, radiusKm, n = 120) {
  if (!(radiusKm > 0)) return null;
  const ring = [];
  for (let i = 0; i < n; i++) ring.push(destination(lat, lon, (360 * i) / n, radiusKm));
  const capDeg = (radiusKm / R_EARTH) * R2D;
  const north = 90 - lat < capDeg;
  const south = lat + 90 < capDeg;
  if (north || south) {
    const pole = north ? 89.99 : -89.99;
    const pts = ring.map(p => [p[0], p[1]]).sort((a, b) => a[0] - b[0]);
    const edgeLat = (pts[0][1] + pts[pts.length - 1][1]) / 2;
    const coords = north
      ? [[-180, edgeLat], ...pts, [180, edgeLat], [180, pole], [-180, pole], [-180, edgeLat]]
      : [[-180, edgeLat], [-180, pole], [180, pole], [180, edgeLat], ...pts.reverse(), [-180, edgeLat]];
    return { type: 'Polygon', coordinates: [coords] };
  }
  // pick the anchor nearest the centre longitude so the unwrapped ring straddles it
  const unwrapped = unwrapLons(ring, 0);
  const meanLon = unwrapped.reduce((s, p) => s + p[0], 0) / unwrapped.length;
  const shift = Math.round((lon - meanLon) / 360) * 360;
  const coords = unwrapped.map(p => [p[0] + shift, p[1]]);
  coords.push([coords[0][0], coords[0][1]]);
  return { type: 'Polygon', coordinates: [coords] };
}

export function footprintRadiusKm(altKm, minElDeg = 0) {
  if (!(altKm > 0)) return 0;
  const el = minElDeg * D2R;
  return Math.max(0, (Math.acos((R_EARTH * Math.cos(el)) / (R_EARTH + altKm)) - el) * R_EARTH);
}

// ------------------------------------------------------------------ time
const pad = n => String(n).padStart(2, '0');
export const pad2 = pad;

export function isoDate(d) {
  const x = d instanceof Date ? d : new Date(d);
  return `${x.getUTCFullYear()}-${pad(x.getUTCMonth() + 1)}-${pad(x.getUTCDate())}`;
}

export function addDays(dateStr, n) {
  const t = Date.parse(`${dateStr}T00:00:00Z`);
  return isoDate(t + n * 86400000);
}

export function validDate(s) {
  return typeof s === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(s) && !Number.isNaN(Date.parse(`${s}T00:00:00Z`));
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/** "Mon 23 Nov" (UTC) */
export function dayLabel(t, { weekday = true, year = false } = {}) {
  const d = new Date(typeof t === 'number' && t < 1e11 ? t * 1000 : t);
  const s = `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}${year ? ` ${d.getUTCFullYear()}` : ''}`;
  return weekday ? `${DAYS[d.getUTCDay()]} ${s}` : s;
}

/** "23 Nov 2026" from "2026-11-23" */
export function dateLabel(dateStr, year = true) {
  if (!validDate(dateStr)) return dateStr || '';
  return dayLabel(Date.parse(`${dateStr}T00:00:00Z`), { weekday: false, year });
}

/** "17:37" UTC */
export function hm(t) {
  const d = new Date(typeof t === 'number' && t < 1e11 ? t * 1000 : t);
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

/** Short duration: "2 d 4 h", "3 h 12 min", "45 min", "30 s" */
export function span(sec) {
  const s = Math.abs(Math.round(sec));
  if (s >= 86400 * 2) return `${Math.floor(s / 86400)} d ${Math.floor((s % 86400) / 3600)} h`;
  if (s >= 3600) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
  if (s >= 60) return `${Math.floor(s / 60)} min`;
  return `${s} s`;
}

/** Launch countdown "T−2 d 04:12:33" / "T+00:03:10" */
export function countdown(ms) {
  const neg = ms < 0;
  let s = Math.floor(Math.abs(ms) / 1000);
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  const clock = `${pad(h)}:${pad(m)}:${pad(s)}`;
  return `T${neg ? '+' : '−'}${d ? `${d} d ` : ''}${clock}`;
}

/** Block-sized countdown: "T−2d 4h", "T−3h 12m", "T−45m", "T+2h" */
export function shortCountdown(ms) {
  const neg = ms < 0;
  const s = Math.floor(Math.abs(ms) / 1000);
  const sign = neg ? 'T+' : 'T−';
  if (s >= 86400) return `${sign}${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
  if (s >= 3600) return `${sign}${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${sign}${Math.floor(s / 60)}m`;
}

export function compass(deg) {
  const pts = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  return pts[Math.round(((deg % 360) + 360) % 360 / 22.5) % 16];
}

// ------------------------------------------------------------------ satellite.js (loaded once)
let satPromise = null;
export function loadSat() {
  if (!satPromise) satPromise = import('/vendor/satellite.js/dist/index.js');
  return satPromise;
}

/** Row from /api/space/satellites -> OMM object for satellite.js json2satrec */
export function rowToOmm(fields, row) {
  const r = {};
  fields.forEach((f, i) => { r[f] = row[i]; });
  return {
    OBJECT_NAME: r.name, OBJECT_ID: r.cospar, EPOCH: r.epoch, MEAN_MOTION: r.mm, ECCENTRICITY: r.ecc, INCLINATION: r.inc,
    RA_OF_ASC_NODE: r.raan, ARG_OF_PERICENTER: r.argp, MEAN_ANOMALY: r.ma, NORAD_CAT_ID: r.id, BSTAR: r.bstar,
    MEAN_MOTION_DOT: r.mmdot, MEAN_MOTION_DDOT: r.mmddot,
  };
}

/** One SGP4 evaluation on the main thread: {lat, lon, alt, speed} or null */
export function propagateOne(sat, satrec, date) {
  try {
    const pv = sat.propagate(satrec, date);
    if (!pv || !pv.position) return null;
    const g = sat.eciToGeodetic(pv.position, sat.gstime(date));
    const v = pv.velocity;
    return { lat: g.latitude * R2D, lon: g.longitude * R2D, alt: g.height, speed: Math.hypot(v.x, v.y, v.z), eci: pv.position };
  } catch {
    return null;
  }
}

export function orbitInfo(satrec) {
  const re = 6378.135;
  const period = (2 * Math.PI) / satrec.no;          // minutes
  const perigee = satrec.altp * re;
  const apogee = satrec.alta * re;
  const e = satrec.ecco;
  let cls = 'MEO';
  if (e >= 0.25) cls = 'HEO';
  else if (apogee < 2000) cls = 'LEO';
  else if (Math.abs(period - 1436.07) <= 30 && e < 0.05) cls = 'GEO';
  else if (period >= 1406) cls = 'HEO';
  return { period, perigee, apogee, e, inc: satrec.inclo * R2D, cls };
}

export const ORBIT_TEXT = {
  LEO: 'Low Earth orbit', MEO: 'Medium Earth orbit', GEO: 'Geosynchronous orbit', HEO: 'Highly elliptical or high orbit',
};

/** Ground track samples (unwrapped around now) for one satellite: {past: [[lon,lat]...], future: [...]} */
export function groundTrack(sat, satrec, nowMs, periodMin, steps = 180) {
  const half = periodMin * 60000;
  const pts = [];
  let nowIndex = 0;
  for (let i = -steps; i <= steps; i++) {
    const t = nowMs + (half * i) / steps;
    const p = propagateOne(sat, satrec, new Date(t));
    if (!p) continue;
    if (i === 0) nowIndex = pts.length;
    pts.push([p.lon, p.lat]);
  }
  if (pts.length < 2) return null;
  const un = unwrapLons(pts, nowIndex);
  return { past: un.slice(0, nowIndex + 1), future: un.slice(nowIndex) };
}

// ------------------------------------------------------------------ raster ordering inside the polygons slot
// Space rasters sit at the bottom of the polygons slot (so every other module's areas, lines and
// points draw on top), ordered among themselves by rank: true colour lowest, radar highest.
const RASTER_RANK = new Map();

export function addRaster(map, map2, spec, rank) {
  if (map.getLayer(spec.id)) return;
  RASTER_RANK.set(spec.id, rank);
  const ids = map.getStyle().layers.map(l => l.id);
  const start = ids.indexOf('slot-polygons');
  const end = ids.indexOf('slot-lines');
  if (start < 0 || end < 0) { map2.addLayer(spec, 'polygons'); return; }
  let before = 'slot-lines';
  for (let i = start + 1; i < end; i++) {
    const r = RASTER_RANK.get(ids[i]);
    if (r === undefined || r > rank) { before = ids[i]; break; }
  }
  map.addLayer(spec, before);
}

export function removeRasterSource(map2, sourceId) {
  map2.removeSource(sourceId);
}

/** Cached fetch of the imagery catalogue (the server caches GIBS DescribeDomains and the EUMETView
 *  capabilities). Each product is cached on its own, so a layer only waits for the products it shows.
 *  Requests are shared between callers and are not aborted by one of them (they are small). */
export const GIBS_FALLBACK_BASE = 'https://gibs.earthdata.nasa.gov/wmts/epsg3857/best';
const gibsCache = { products: new Map(), base: null, meteosatCredit: null, inflight: new Map() };
export async function gibsCatalogue(api, { products = null, maxAgeMs = 5 * 60000 } = {}) {
  const ids = products && products.length ? [...new Set(products)] : null;
  const now = Date.now();
  const fresh = (pid) => { const e = gibsCache.products.get(pid); return e && now - e.at < maxAgeMs; };
  const missing = ids ? ids.filter(pid => !fresh(pid)).sort() : null;
  if (!ids || missing.length) {
    const key = missing ? missing.join(',') : '*';
    let p = gibsCache.inflight.get(key);
    if (!p) {
      p = api.get('/api/space/gibs', missing ? { products: key } : null, { timeoutMs: 30000 })
        .then((d) => {
          const at = Date.now();
          gibsCache.base = d.base || gibsCache.base;
          gibsCache.meteosatCredit = d.meteosat_credit || gibsCache.meteosatCredit;
          for (const [pid, prod] of Object.entries(d.products || {})) gibsCache.products.set(pid, { at, data: prod });
          return d;
        })
        .finally(() => gibsCache.inflight.delete(key));
      gibsCache.inflight.set(key, p);
    }
    try { await p; } catch (err) { if (!ids || !ids.every(pid => gibsCache.products.has(pid))) throw err; }
  }
  const out = {};
  for (const pid of ids || gibsCache.products.keys()) { const e = gibsCache.products.get(pid); if (e) out[pid] = e.data; }
  return { base: gibsCache.base || GIBS_FALLBACK_BASE, products: out, meteosat_credit: gibsCache.meteosatCredit };
}

/** Static product descriptions (titles, variants, legends; no times), fetched once and shared. */
let metaPromise = null;
export function gibsMeta(api) {
  if (!metaPromise) {
    metaPromise = api.get('/api/space/gibs', { meta: 1 }, { timeoutMs: 4000 }).catch((err) => { metaPromise = null; throw err; });
  }
  return metaPromise;
}

export function gibsUrl(base, layer, matrix, ext, time) {
  return `${base}/${layer}/default/${time || 'default'}/${matrix}/{z}/{y}/{x}.${ext}`;
}

/** A WMS 1.3.0 GetMap template for MapLibre ({bbox-epsg-3857} is filled per tile). spec comes from the
 *  catalogue: {url, layers, styles, format}. EUMETView sends CORS headers and caches fixed times for a week. */
export function wmsUrl(spec, time) {
  const q = [['service', 'WMS'], ['request', 'GetMap'], ['version', '1.3.0'], ['layers', spec.layers], ['styles', spec.styles || ''],
    ['crs', 'EPSG:3857'], ['width', '256'], ['height', '256'], ['format', spec.format || 'image/png'], ['transparent', 'true']];
  if (time) q.push(['time', time]);
  return `${spec.url}?${q.map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&')}&bbox={bbox-epsg-3857}`;
}

/** Tile URL template for one catalogue layer entry (GIBS WMTS or EUMETView WMS) at a time. */
export function productTileUrl(base, product, le, time) {
  if (le.provider === 'eumetsat' && le.wms) return wmsUrl(le.wms, time);
  return gibsUrl(base, le.layer, product.matrix, product.ext, time);
}

/** Modal date picker; resolves to 'YYYY-MM-DD' or null. */
export function pickDate(ctx, initial, { title = 'Choose a day', note = '', min = '2000-02-24', button = 'Show this day' } = {}) {
  const { h, ui } = ctx;
  return new Promise((resolve) => {
    let done = false;
    const input = h('input', { type: 'date', value: initial, min, max: isoDate(Date.now()) });
    const ok = h('button', { type: 'button', class: 'btn primary' }, button);
    const m = ui.modal({ title, body: h('div', { class: 'field' }, h('label', null, 'Date (UTC)'), input,
      note ? h('p', { class: 'faint space-note' }, note) : null), foot: ok,
    onClose: () => { if (!done) resolve(null); } });
    ok.addEventListener('click', () => { done = true; m.close(); resolve(validDate(input.value) ? input.value : null); });
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') ok.click(); });
    input.focus();
  });
}

/** SVG element helper (ctx.h makes HTML elements only). Attribute values are numbers or fixed strings. */
export function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null && v !== false) el.setAttribute(k, String(v));
  for (const c of children.flat()) if (c != null) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}

/** What's here sections resolve in any order; the ISS section waits briefly for weather so weather comes first. */
export const whatsHereOrder = { weather: null };

/** Tile failures from the space module's own raster sources (GIBS, RainViewer) are expected now and then
 *  (a frame still being produced, a gap day). They go to the layer status instead of the console.
 *  Errors from anyone else's sources are logged exactly as MapLibre would without a listener. */
const tileFailures = new Map();   // source prefix -> {count, onWarn}
export function watchTileErrors(map, prefix, onWarn) {
  tileFailures.set(prefix, { count: 0, onWarn });
  if (map._spaceErrorWatch) return;
  map._spaceErrorWatch = true;
  map.on('error', (e) => {
    const sid = e?.sourceId || e?.source?.id || '';
    const entry = [...tileFailures.entries()].find(([p]) => sid.startsWith(p));
    if (entry && e?.error && /AJAXError|Failed to fetch|404|500|503/.test(`${e.error.name || ''} ${e.error.message || ''} ${e.error.status || ''}`)) {
      const st = entry[1];
      st.count += 1;
      if (st.count === 6) st.onWarn?.();
      return;
    }
    console.error(e?.error || e);
  });
}
export function resetTileErrors(prefix) { const st = tileFailures.get(prefix); if (st) st.count = 0; }

/** Sentence case for upstream Title Case strings ("Go for Launch" -> "Go for launch"), keeping acronyms
 *  (LEO, NASA, SSO) and a few proper nouns. */
const KEEP = new Set(['Earth', 'Moon', 'Sun', 'Mars', 'Venus', 'Jupiter', 'Lunar', 'Martian', 'International', 'Space', 'Station']);
export function sentence(text) {
  if (!text) return text;
  return String(text).split(/(\s+|\/|-)/).map((w, i) => {
    if (i === 0 || !/[a-z]/.test(w) || /^[A-Z0-9]{2,}$/.test(w) || KEEP.has(w)) return w;
    return w.charAt(0).toLowerCase() + w.slice(1);
  }).join('');
}
