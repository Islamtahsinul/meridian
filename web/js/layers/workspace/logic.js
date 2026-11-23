// Workspaces: pure helpers with no DOM and no ctx, so Node can test them
// (tests/fixtures/workspace/logic_test.mjs). Three parts:
// 1. workspace states: capture the page (snapshot) and plan how to apply a saved state;
// 2. which MapLibre layers draw a registered layer (for "Export what's on screen");
// 3. the file formats: GeoJSON (RFC 7946), CSV (RFC 4180, UTF-8 with BOM) and KML 2.2.

export const BASEMAPS = ['dark', 'relief', 'light', 'streets', 'satellite', 'sentinel'];
export const PROJECTIONS = ['mercator', 'globe'];
export const PLACE_TYPES = [
  { id: 'home', label: 'Home' }, { id: 'work', label: 'Work' }, { id: 'family', label: 'Family' },
  { id: 'travel', label: 'Travel' }, { id: 'other', label: 'Other' },
];
export const PLACE_RADII = [5, 10, 25, 50, 100, 250, 500];
export const MAX_FEATURES = 50000;
export const MAX_BYTES = 25 * 1024 * 1024;

const LAYER_RE = /^[A-Za-z0-9_.:-]{1,80}$/;
const OPTION_RE = /^[A-Za-z0-9_.-]{1,60}$/;
const round = (v, d) => Math.round(v * 10 ** d) / 10 ** d;
const finite = v => typeof v === 'number' && Number.isFinite(v);

// ------------------------------------------------------------------ 1. workspace states
/** A declared option's value, checked against its spec: {ok, value}. */
export function optionValue(spec, value) {
  if (!spec || typeof spec !== 'object') return { ok: false };
  if (spec.type === 'toggle') return typeof value === 'boolean' ? { ok: true, value } : { ok: false };
  if (spec.type === 'select') {
    const choice = (spec.choices || []).find(c => String(c.value) === String(value));
    return choice ? { ok: true, value: choice.value } : { ok: false };
  }
  if (spec.type === 'range') {
    const n = typeof value === 'string' && value.trim() !== '' ? Number(value) : value;
    if (!finite(n)) return { ok: false };
    if ((finite(spec.min) && n < spec.min) || (finite(spec.max) && n > spec.max)) return { ok: false };
    return { ok: true, value: n };
  }
  return { ok: false };
}

/**
 * The page as a workspace state (version 1).
 * page: {view: {center: {lng, lat} | [lon, lat], zoom, bearing, pitch}, display: {basemap, projection, terrain},
 *        layers: [{id, on, options: {current values}, specOptions: [declared options]}]}
 * Only the layers that are on are kept, each with its declared options.
 */
export function snapshot({ view, display = {}, layers = [] }) {
  const state = { v: 1 };
  const c = Array.isArray(view?.center) ? { lng: view.center[0], lat: view.center[1] } : view?.center;
  if (c && finite(+c.lat) && finite(+c.lng)) {
    const lon = ((((+c.lng + 180) % 360) + 360) % 360) - 180;
    state.view = { center: [round(lon, 5), round(+c.lat, 5)], zoom: round(+view.zoom || 0, 2),
      bearing: round(+view.bearing || 0, 1), pitch: round(+view.pitch || 0, 1) };
  }
  if (BASEMAPS.includes(display.basemap)) state.basemap = display.basemap;
  if (PROJECTIONS.includes(display.projection)) state.projection = display.projection;
  if (typeof display.terrain === 'boolean') state.terrain = display.terrain;
  state.layers = {};
  for (const l of layers) {
    if (!l || !l.on || !LAYER_RE.test(l.id || '')) continue;
    const entry = { on: true };
    const opts = {};
    for (const spec of l.specOptions || []) {
      if (!spec || !OPTION_RE.test(spec.id || '') || !l.options || !(spec.id in l.options)) continue;
      const checked = optionValue(spec, l.options[spec.id]);
      if (checked.ok) opts[spec.id] = checked.value;
    }
    if (Object.keys(opts).length) entry.options = opts;
    state.layers[l.id] = entry;
  }
  return state;
}

/**
 * How to apply a state to the page. registry: [{id, on, options, specOptions}].
 * Returns {turnOn, turnOff, options: [{id, name, value, layerOn}], unknown, view, display, touchesLayers}.
 * Layers the state doesn't know are turned off (a workspace is the whole picture); layer ids this page
 * doesn't have are reported as unknown; options are applied only when valid for the declared option.
 */
export function planApply(state, registry) {
  const out = { turnOn: [], turnOff: [], options: [], unknown: [], view: null, display: {}, touchesLayers: false };
  if (!state || typeof state !== 'object') return out;
  const byId = new Map((registry || []).map(l => [l.id, l]));
  if (state.layers && typeof state.layers === 'object') {
    out.touchesLayers = true;
    const wanted = new Set();
    for (const [id, entry] of Object.entries(state.layers)) {
      if (!entry || entry.on !== true) continue;
      if (!byId.has(id)) { if (LAYER_RE.test(id)) out.unknown.push(id); continue; }
      wanted.add(id);
      const l = byId.get(id);
      const specs = new Map((l.specOptions || []).map(o => [o.id, o]));
      for (const [name, value] of Object.entries(entry.options || {})) {
        const checked = optionValue(specs.get(name), value);
        if (checked.ok && (!l.options || l.options[name] !== checked.value)) out.options.push({ id, name, value: checked.value, layerOn: !!l.on });
      }
      if (!l.on) out.turnOn.push(id);
    }
    for (const l of registry || []) if (l.on && !wanted.has(l.id)) out.turnOff.push(l.id);
  }
  const v = state.view;
  if (v && Array.isArray(v.center) && finite(v.center[0]) && finite(v.center[1]) && Math.abs(v.center[1]) <= 90) {
    out.view = { center: [v.center[0], v.center[1]], zoom: finite(v.zoom) ? Math.min(22, Math.max(0, v.zoom)) : 3,
      bearing: finite(v.bearing) ? v.bearing : 0, pitch: finite(v.pitch) ? Math.min(85, Math.max(0, v.pitch)) : 0 };
  }
  if (BASEMAPS.includes(state.basemap)) out.display.basemap = state.basemap;
  if (PROJECTIONS.includes(state.projection)) out.display.projection = state.projection;
  if (typeof state.terrain === 'boolean') out.display.terrain = state.terrain;
  return out;
}

/** "6 layers, dark map, globe" for a state. */
export function describeState(state) {
  if (!state) return '';
  const on = Object.values(state.layers || {}).filter(e => e && e.on).length;
  const parts = [state.layers ? `${on} layer${on === 1 ? '' : 's'}` : 'layers unchanged'];
  if (state.basemap) parts.push(`${state.basemap} map`);
  if (state.projection === 'globe') parts.push('globe');
  if (state.terrain) parts.push('3D terrain');
  return parts.join(', ');
}

/** Reads a workspaces export (the GET /api/workspace/export document, or a bare list). */
/** Reads the text of a workspaces file; broken JSON gets a plain message, not the parser's. */
export function parseExport(text) {
  let doc;
  try { doc = JSON.parse(text); } catch { throw new Error("it isn't a Meridian workspaces file (the JSON is broken)."); }
  return readExport(doc);
}

export function readExport(doc) {
  const list = Array.isArray(doc) ? doc : (doc && typeof doc === 'object' ? doc.workspaces : null);
  if (!Array.isArray(list)) throw new Error('This file has no workspaces in it.');
  if (doc && !Array.isArray(doc) && doc.kind && doc.kind !== 'meridian-workspaces') throw new Error('This is not a Meridian workspaces file.');
  return list.filter(w => w && typeof w === 'object' && typeof w.name === 'string' && w.state && typeof w.state === 'object')
    .map(w => ({ name: w.name, state: w.state }));
}

// ------------------------------------------------------------------ 2. which MapLibre layers draw a layer
// Contract: overlay ids start with ov-<module>- (CONTRACT.md 10.5). Within a module the next word
// usually names the layer (ov-hazards-quakes-label is hazards.quakes); a few are abbreviations
// (ov-infra-ap-large, ov-infra-dc-c, ov-space-sats) and a few are shared, such as the one aircraft
// source behind flights, flights.military and flights.alerts. Helpers for cards and hover (trails,
// selections, hit areas) are never exported.
const HELPER_RE = /-(trail|sel|selection|hover|hit|highlight|draft|preview)(-|$)/;
const ALIASES = { infra: { landings: 'cables' } };
export const RASTER_TYPES = new Set(['raster', 'hillshade', 'color-relief', 'background', 'sky']);

function isSubsequence(short, long) {
  let i = 0;
  for (const ch of long) if (ch === short[i]) i++;
  return i === short.length;
}

/** Which registered sub-layer an ov- id belongs to: the sub id, or null when the name doesn't say. */
export function ownerOf(styleId, module, subs) {
  const prefix = `ov-${module}-`;
  if (!styleId.startsWith(prefix)) return null;
  const token = styleId.slice(prefix.length).split('-')[0].toLowerCase();
  if (!token) return null;
  const alias = ALIASES[module]?.[token];
  if (alias && subs.includes(alias)) return alias;
  let best = null;
  let score = 0;
  for (const sub of subs) {
    const s = sub.toLowerCase().replace(/[^a-z0-9]/g, '');
    if (!s) continue;
    let k = 0;
    if (s === token) k = 3;
    else if (token.length >= 2 && (s.startsWith(token) || token.startsWith(s))) k = 2;
    else if (token.length >= 2 && token[0] === s[0] && isSubsequence(token, s)) k = 1;
    if (k > score) { score = k; best = sub; }
  }
  return best;
}

/**
 * The style layers that draw a registered layer. styleIds: every layer id in the map style;
 * registered: every registered layer id. Returns {ids, shared}: shared is true when the layer's
 * features come from map layers it shares with other layers of its module (they are all included).
 */
export function styleLayersFor(layerId, styleIds, registered) {
  const module = layerId.split('.')[0];
  const sub = layerId.includes('.') ? layerId.slice(module.length + 1) : '';
  const subs = (registered || []).filter(id => id.startsWith(`${module}.`)).map(id => id.slice(module.length + 1));
  const own = [];
  const unclaimed = [];
  for (const id of styleIds || []) {
    if (!(id === `ov-${module}` || id.startsWith(`ov-${module}-`)) || HELPER_RE.test(id)) continue;
    const owner = ownerOf(id, module, subs);
    if (owner === null) unclaimed.push(id);
    else if (owner === sub) own.push(id);
  }
  if (own.length) return { ids: own, shared: false };
  return { ids: unclaimed, shared: unclaimed.length > 0 && subs.length > 0 };
}

// ------------------------------------------------------------------ features
/** The scalar properties as a stable string: how a drawn feature is matched to its source feature
 * (MapLibre keeps strings, numbers and booleans, drops nulls and turns nested values into JSON text). */
export function signature(props) {
  if (!props || typeof props !== 'object') return '[]';
  const parts = [];
  for (const key of Object.keys(props).sort()) {
    const v = props[key];
    if (typeof v === 'string') { if (!v.startsWith('{') && !v.startsWith('[')) parts.push([key, v]); }
    else if (typeof v === 'number') { if (Number.isFinite(v)) parts.push([key, Object.is(v, -0) ? 0 : v]); }
    else if (typeof v === 'boolean') parts.push([key, v]);
  }
  return JSON.stringify(parts);
}

function firstPosition(geometry) {
  let c = geometry?.coordinates;
  if (geometry?.type === 'GeometryCollection') return firstPosition(geometry.geometries?.[0]);
  while (Array.isArray(c) && Array.isArray(c[0])) c = c[0];
  return Array.isArray(c) && finite(c[0]) && finite(c[1]) ? c : null;
}

/** Index source features by signature (for matching drawn features back to them). */
export function indexFeatures(data) {
  const list = data?.type === 'FeatureCollection' ? data.features : (data?.type === 'Feature' ? [data] : []);
  const index = new Map();
  for (const f of list || []) {
    if (!f || f.type !== 'Feature' || !f.geometry) continue;
    const key = signature(f.properties);
    const hit = index.get(key);
    if (hit) hit.push(f); else index.set(key, [f]);
  }
  return index;
}

// One thing can be drawn from two sources: the cameras layer draws its points from one source and their
// street-zoom names from another (ov-cameras-names), so the same camera comes back twice.
const STABLE_KEYS = ['id', 'hex', 'mmsi'];
const SAME_SPOT_DEG = 1e-4;   // about 11 m

/** 'id:tfl:00001.07458': a feature's stable id (property id, hex or mmsi), or null. */
export function stableId(props) {
  if (!props || typeof props !== 'object') return null;
  for (const k of STABLE_KEYS) {
    const v = props[k];
    if ((typeof v === 'string' && v.trim()) || finite(v)) return `${k}:${v}`;
  }
  return null;
}

/**
 * A check for points drawn twice from different sources. duplicate(feature, sourceId) is true when a
 * point with the same stable id came from another source within about 11 m; otherwise it remembers this
 * one. Shapes, lines and features without a stable id are never duplicates here, and within one source
 * the usual matching applies, so the parts of one thing that share an id are all kept.
 */
export function crossSourceDuplicates() {
  const seen = new Map();   // stable id -> [[lon, lat, sourceId]]
  return (feature, sourceId) => {
    const key = stableId(feature?.properties);
    if (!key) return false;
    const g = feature.geometry;
    const c = g?.type === 'Point' ? g.coordinates : null;
    if (!Array.isArray(c) || !finite(c[0]) || !finite(c[1])) return false;
    const list = seen.get(key);
    if (list?.some(([x, y, s]) => s !== sourceId && Math.abs(x - c[0]) <= SAME_SPOT_DEG && Math.abs(y - c[1]) <= SAME_SPOT_DEG)) return true;
    if (list) list.push([c[0], c[1], sourceId]); else seen.set(key, [[c[0], c[1], sourceId]]);
    return false;
  };
}

/** The source feature a drawn feature came from (nearest when several share its properties), or null. */
export function matchFeature(index, drawn) {
  const hits = index?.get(signature(drawn?.properties));
  if (!hits || !hits.length) return null;
  if (hits.length === 1) return hits[0];
  const p = firstPosition(drawn.geometry);
  if (!p) return null;
  let best = null;
  let bestD = Infinity;
  for (const f of hits) {
    const q = firstPosition(f.geometry);
    if (!q) continue;
    const d = (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2;
    if (d < bestD) { bestD = d; best = f; }
  }
  return best;
}

function roundCoords(c, d = 5) {
  if (!Array.isArray(c)) return c;
  if (typeof c[0] === 'number') return c.map(v => round(v, d));
  return c.map(x => roundCoords(x, d));
}

export function roundGeometry(g) {
  if (!g || typeof g !== 'object') return null;
  if (g.type === 'GeometryCollection') return { type: g.type, geometries: (g.geometries || []).map(roundGeometry).filter(Boolean) };
  if (!Array.isArray(g.coordinates)) return null;
  return { type: g.type, coordinates: roundCoords(g.coordinates) };
}

function eachPosition(g, fn) {
  if (!g) return;
  if (g.type === 'GeometryCollection') { for (const x of g.geometries || []) eachPosition(x, fn); return; }
  const walk = (c) => { if (!Array.isArray(c)) return; if (typeof c[0] === 'number') fn(c); else c.forEach(walk); };
  walk(g.coordinates);
}

/** [w, s, e, n] of the features, or null. */
export function bboxOf(features) {
  let w = Infinity; let s = Infinity; let e = -Infinity; let n = -Infinity;
  for (const f of features) eachPosition(f.geometry, ([x, y]) => { w = Math.min(w, x); e = Math.max(e, x); s = Math.min(s, y); n = Math.max(n, y); });
  return Number.isFinite(w) ? [round(w, 5), round(s, 5), round(e, 5), round(n, 5)] : null;
}

/** A point for a row: the point itself, or the centre of a shape's extent. */
export function representativePoint(g) {
  if (!g) return null;
  if (g.type === 'Point' && finite(g.coordinates?.[0]) && finite(g.coordinates?.[1])) return [round(g.coordinates[0], 5), round(g.coordinates[1], 5)];
  const b = bboxOf([{ geometry: g }]);
  return b ? [round((b[0] + b[2]) / 2, 5), round((b[1] + b[3]) / 2, 5)] : null;
}

// Owner fields are never exported (CONTRACT.md rule 2); Windy webcams never leave the map (their terms).
const OWNER_KEY = /(^|_)(owner|registrant)(_|$)|^owners?$/i;
const WINDY = /^windy:/i;

/** Drop what must never be exported. aircraft: {checked, allowed: Set of hex} or null (unknown: drop all). */
export function restrict(features, { aircraft = undefined } = {}) {
  const out = [];
  const dropped = { windy: 0, privacy: 0 };
  for (const f of features) {
    const p = f.properties || {};
    if ([p.id, p.ref, p.camera].some(v => typeof v === 'string' && WINDY.test(v))) { dropped.windy++; continue; }
    if (aircraft !== undefined && typeof p.hex === 'string') {
      if (!aircraft || !aircraft.allowed.has(p.hex)) { dropped.privacy++; continue; }
    }
    const props = {};
    for (const [k, v] of Object.entries(p)) if (!OWNER_KEY.test(k)) props[k] = v;
    out.push({ ...f, properties: props });
  }
  return { features: out, dropped };
}

/** Decode the flights module's compact answers ({fields, ac}) into hex -> row. */
export function aircraftRows(payloads) {
  const rows = new Map();
  for (const payload of payloads || []) {
    const fields = Array.isArray(payload?.fields) ? payload.fields : [];
    const ix = Object.fromEntries(fields.map((name, i) => [name, i]));
    if (ix.hex == null) continue;
    for (const r of payload.ac || []) {
      if (!Array.isArray(r) || typeof r[ix.hex] !== 'string' || !/^~?[0-9a-f]{6}$/.test(r[ix.hex])) continue;
      if (ix.lat != null && (!finite(r[ix.lat]) || !finite(r[ix.lon]))) continue;
      rows.set(r[ix.hex], { hex: r[ix.hex], flags: (r[ix.flags] | 0), cs: r[ix.cs], reg: r[ix.reg], typ: r[ix.typ],
        alt: r[ix.alt], gs: r[ix.gs], trk: r[ix.trk] });
    }
  }
  return rows;
}

export const PRIVACY_FLAG = 64;   // LADD or PIA (meridian/modules/flights/const.py FLAG_PRIV)

// ------------------------------------------------------------------ 3. file formats
const ID_KEYS = ['id', 'hex', 'mmsi', 'key', 'uid', 'code', 'ident', 'ref'];
const TITLE_KEYS = ['title', 'name', 'n', 'label', 'lbl', 'place', 'subject', 'callsign'];
const TIME_KEYS = ['time', 't', 'updated', 'when', 'start', 'issued', 'last'];
const XML_BAD = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\ufffe\uffff]/g;

// textOnly: titles must be words. Several layers keep a name in `n` (infra, cameras, satellites) but
// others keep a count there (ships, fires), and a count is not a title.
function firstOf(props, keys, textOnly = false) {
  for (const k of keys) {
    const v = props[k];
    if ((typeof v === 'string' && v.trim()) || (!textOnly && finite(v))) return { key: k, value: v };
  }
  return null;
}

/** ISO 8601 UTC for epoch seconds, epoch ms or a date string between 1990 and 2100; else ''. */
export function isoTime(value) {
  let ms = NaN;
  if (finite(value)) ms = value < 1e11 ? value * 1000 : value;
  else if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}/.test(value)) ms = Date.parse(value);
  if (!Number.isFinite(ms) || ms < Date.UTC(1990, 0, 1) || ms > Date.UTC(2100, 0, 1)) return '';
  return new Date(ms).toISOString().replace(/\.000Z$/, 'Z');
}

/** The common columns of a feature: {kind, id, title, time_utc, lat, lon, geometry}. */
export function rowOf(f) {
  const p = f.properties || {};
  const id = firstOf(p, ID_KEYS);
  const title = firstOf(p, TITLE_KEYS, true);
  const t = firstOf(p, TIME_KEYS);
  const pt = representativePoint(f.geometry);
  return {
    kind: typeof p.kind === 'string' ? p.kind : '',
    id: id ? String(id.value) : (f.id != null ? String(f.id) : ''),
    title: title ? String(title.value).replace(/\s*\n\s*/g, ', ') : (id?.key === 'mmsi' ? `MMSI ${id.value}` : ''),
    time_utc: t ? isoTime(t.value) : '',
    lat: pt ? pt[1] : '',
    lon: pt ? pt[0] : '',
    geometry: f.geometry?.type || '',
  };
}

function plainValue(v) {
  if (v == null) return '';
  if (typeof v === 'object') { try { return JSON.stringify(v); } catch { return ''; } }
  return v;
}

/** One CSV cell (RFC 4180), with text that a spreadsheet would run as a formula made harmless. */
export function csvCell(value) {
  if (value == null || value === '') return '';
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : '';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  let s = String(value);
  if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

const MAX_EXTRA_COLUMNS = 60;

/** CSV text: UTF-8 byte-order mark (for Excel), CRLF lines, common columns, the layer's own fields, credits. */
export function toCSV(features, meta = {}) {
  const base = ['layer', 'kind', 'id', 'title', 'lat', 'lon', 'time_utc', 'geometry'];
  const extra = [];
  const seen = new Set(base);
  for (const f of features) {
    for (const k of Object.keys(f.properties || {})) {
      if (!seen.has(k) && extra.length < MAX_EXTRA_COLUMNS) { seen.add(k); extra.push(k); }
    }
  }
  const header = [...base, ...extra, 'source'];
  const lines = [header.map(csvCell).join(',')];
  for (const f of features) {
    const r = rowOf(f);
    const p = f.properties || {};
    const cells = [meta.layer || '', r.kind, r.id, r.title, r.lat, r.lon, r.time_utc, r.geometry, ...extra.map(k => plainValue(p[k])), meta.source || ''];
    lines.push(cells.map(csvCell).join(','));
  }
  return `\ufeff${lines.join('\r\n')}\r\n`;
}

/** GeoJSON text (RFC 7946) with name, attribution, licence, exported_at, note and bbox at the top. */
export function toGeoJSON(features, meta = {}) {
  const out = features.map((f) => {
    const g = roundGeometry(f.geometry);
    return g ? { type: 'Feature', geometry: g, properties: { ...(f.properties || {}) } } : null;
  }).filter(Boolean);
  const doc = {
    type: 'FeatureCollection',
    name: meta.title || meta.layer || 'Meridian export',
    attribution: meta.attribution || meta.source || '',
    licence: meta.licence || '',
    exported_at: meta.exportedAt || new Date().toISOString(),
    note: meta.note || '',
    layer: meta.layer || '',
    bbox: bboxOf(out) || undefined,
    features: out,
  };
  return JSON.stringify(doc);
}

export function xmlText(value) {
  return String(value ?? '').replace(XML_BAD, '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&apos;');
}

/** '#f0a43a' -> KML's aabbggrr ('ff3aa4f0'). */
export function kmlColor(hex, alpha = 'ff') {
  const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || '').trim());
  const rgb = m ? m[1].toLowerCase() : 'ebe6d6';
  return `${alpha}${rgb.slice(4, 6)}${rgb.slice(2, 4)}${rgb.slice(0, 2)}`;
}

function kmlCoords(list) {
  return list.filter(p => finite(p?.[0]) && finite(p?.[1])).map(p => `${round(p[0], 5)},${round(p[1], 5)}`).join(' ');
}

function kmlGeometry(g) {
  if (!g) return '';
  const c = g.coordinates;
  switch (g.type) {
    case 'Point': return finite(c?.[0]) && finite(c?.[1]) ? `<Point><coordinates>${kmlCoords([c])}</coordinates></Point>` : '';
    case 'LineString': return `<LineString><tessellate>1</tessellate><coordinates>${kmlCoords(c || [])}</coordinates></LineString>`;
    case 'Polygon': {
      const [outer, ...holes] = c || [];
      if (!outer) return '';
      return `<Polygon><tessellate>1</tessellate><outerBoundaryIs><LinearRing><coordinates>${kmlCoords(outer)}</coordinates></LinearRing></outerBoundaryIs>${
        holes.map(h => `<innerBoundaryIs><LinearRing><coordinates>${kmlCoords(h)}</coordinates></LinearRing></innerBoundaryIs>`).join('')}</Polygon>`;
    }
    case 'MultiPoint': return `<MultiGeometry>${(c || []).map(p => kmlGeometry({ type: 'Point', coordinates: p })).join('')}</MultiGeometry>`;
    case 'MultiLineString': return `<MultiGeometry>${(c || []).map(l => kmlGeometry({ type: 'LineString', coordinates: l })).join('')}</MultiGeometry>`;
    case 'MultiPolygon': return `<MultiGeometry>${(c || []).map(p => kmlGeometry({ type: 'Polygon', coordinates: p })).join('')}</MultiGeometry>`;
    case 'GeometryCollection': return `<MultiGeometry>${(g.geometries || []).map(kmlGeometry).join('')}</MultiGeometry>`;
    default: return '';
  }
}

/** KML 2.2 text: credits in the document description, one style in the layer colour, no remote icons. */
export function toKML(features, meta = {}) {
  const colour = meta.color || '#ebe6d6';
  const marks = [];
  for (const f of features) {
    const geom = kmlGeometry(f.geometry);
    if (!geom) continue;
    const r = rowOf(f);
    const data = Object.entries(f.properties || {}).slice(0, 80)
      .map(([k, v]) => `<Data name="${xmlText(k)}"><value>${xmlText(plainValue(v))}</value></Data>`).join('');
    marks.push(`<Placemark><name>${xmlText(r.title || r.id || meta.title || 'Feature')}</name>${
      r.time_utc ? `<TimeStamp><when>${r.time_utc}</when></TimeStamp>` : ''}<styleUrl>#layer</styleUrl>${
      data ? `<ExtendedData>${data}</ExtendedData>` : ''}${geom}</Placemark>`);
  }
  const description = [meta.note, meta.attribution ? `Source: ${meta.attribution}` : '', meta.licence ? `Licence: ${meta.licence}` : '',
    `Exported from Meridian at ${meta.exportedAt || new Date().toISOString()}.`].filter(Boolean).join(' ');
  return '<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
    + `<name>${xmlText(meta.title || meta.layer || 'Meridian export')}</name><description>${xmlText(description)}</description>`
    + `<Style id="layer"><IconStyle><color>${kmlColor(colour)}</color><scale>0.8</scale></IconStyle>`
    + `<LineStyle><color>${kmlColor(colour)}</color><width>2</width></LineStyle><PolyStyle><color>${kmlColor(colour, '59')}</color></PolyStyle></Style>`
    + `${marks.join('\n')}</Document></kml>\n`;
}

export const FORMATS = {
  geojson: { label: 'GeoJSON', ext: 'geojson', type: 'application/geo+json', write: toGeoJSON },
  csv: { label: 'CSV', ext: 'csv', type: 'text/csv;charset=utf-8', write: toCSV },
  kml: { label: 'KML', ext: 'kml', type: 'application/vnd.google-earth.kml+xml', write: toKML },
};

/** meridian-hazards-quakes-on-screen-20261123-1702.csv (UTC). */
export function exportFilename(layerId, format, date = new Date()) {
  const p = (n) => String(n).padStart(2, '0');
  const stamp = `${date.getUTCFullYear()}${p(date.getUTCMonth() + 1)}${p(date.getUTCDate())}-${p(date.getUTCHours())}${p(date.getUTCMinutes())}`;
  const safe = String(layerId || 'layer').replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || 'layer';
  return `meridian-${safe}-on-screen-${stamp}.${FORMATS[format]?.ext || 'txt'}`;
}
