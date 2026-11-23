// The map (CONTRACT.md section 10.5): basemaps, globe/flat, 3D terrain, overlay slots, icons,
// click and hover dispatch, highlight ring. Modules reach it through ctx.map (the MapLibre map)
// and ctx.map2 (these helpers).

import { bus } from './bus.js';
import { h, fmt, throttle, debounce, svgIcon } from './dom.js';

const maplibregl = window.maplibregl;

const CARTO_DARK = 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json';
const CARTO_ATTR = '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors © <a href="https://carto.com/about-carto/">CARTO</a>';
const sub4 = (tmpl) => ['a', 'b', 'c', 'd'].map(s => tmpl.replace('{s}', s));

export const BASEMAPS = [
  { id: 'dark', title: 'Dark', note: 'Vector, CARTO Dark Matter' },
  { id: 'relief', title: 'Relief', note: 'Dark map with hill shading' },
  { id: 'light', title: 'Light', note: 'CARTO Positron', raster: { tiles: sub4('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png'), tileSize: 256, maxzoom: 20, attribution: CARTO_ATTR }, labels: false },
  { id: 'streets', title: 'Streets', note: 'CARTO Voyager', raster: { tiles: sub4('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}@2x.png'), tileSize: 256, maxzoom: 20, attribution: CARTO_ATTR }, labels: false },
  { id: 'satellite', title: 'Satellite', note: 'Esri World Imagery', raster: { tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'], tileSize: 256, maxzoom: 19, attribution: 'Imagery © Esri, Maxar, Earthstar Geographics and the GIS User Community' }, labels: true },
  { id: 'sentinel', title: 'Sentinel-2 cloudless', note: 'EOX, 2020 mosaic', raster: { tiles: ['https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2020_3857/default/g/{z}/{y}/{x}.jpg'], tileSize: 256, maxzoom: 15, attribution: '<a href="https://s2maps.eu">Sentinel-2 cloudless 2020</a> by EOX IT Services GmbH (contains modified Copernicus Sentinel data 2020), CC BY-NC-SA 4.0' }, labels: true },
];

const DEM = {
  type: 'raster-dem',
  tiles: ['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'],
  encoding: 'terrarium', tileSize: 256, maxzoom: 15,
  attribution: 'Terrain: <a href="https://registry.opendata.aws/terrain-tiles/">Mapzen terrain tiles on AWS</a>',
};

export const SLOTS = ['polygons', 'lines', 'points', 'moving', 'top'];
const SLOT_MARKERS = ['slot-polygons', 'slot-lines', 'slot-points', 'slot-moving', 'slot-top', 'slot-end'];

const FALLBACK_STYLE = {
  version: 8,
  glyphs: 'https://tiles.basemaps.cartocdn.com/fonts/{fontstack}/{range}.pbf',
  sources: { 'carto-dark': { type: 'raster', tiles: sub4('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png'), tileSize: 256, attribution: CARTO_ATTR } },
  layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#0e0e0e' } }, { id: 'carto-dark', type: 'raster', source: 'carto-dark' }],
};

let map = null;
let baseVectorIds = [];
let baseSymbolIds = [];
let keepOnImagery = [];
let state = { basemap: 'dark', projection: 'mercator', terrain: false };
const icons = new Map();
const clickables = [];
const hovers = [];
let tipEl = null;

function settingsGet(key, def) {
  try { const v = localStorage.getItem('meridian.' + key); return v == null ? def : JSON.parse(v); } catch { return def; }
}
function settingsSet(key, value) {
  try { localStorage.setItem('meridian.' + key, JSON.stringify(value)); } catch { /* private mode */ }
}

async function loadBaseStyle() {
  try {
    const res = await fetch(CARTO_DARK, { signal: AbortSignal.timeout(12000) });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return await res.json();
  } catch (err) {
    console.warn('CARTO vector style unavailable, using the raster fallback', err);
    return structuredClone(FALLBACK_STYLE);
  }
}

function prepareStyle(style) {
  style.projection = { type: 'mercator' };
  const layers = style.layers || [];
  baseVectorIds = layers.filter(l => l.type !== 'background').map(l => l.id);
  baseSymbolIds = layers.filter(l => l.type === 'symbol').map(l => l.id);
  keepOnImagery = layers.filter(l => l.type === 'line' && /boundar|admin/i.test(l.id)).map(l => l.id);
  const bgIndex = Math.max(0, layers.findIndex(l => l.type === 'background'));
  const rasterLayers = [];
  for (const b of BASEMAPS) {
    if (!b.raster) continue;
    style.sources['bm-' + b.id] = { type: 'raster', ...b.raster };
    rasterLayers.push({ id: 'bm-' + b.id, type: 'raster', source: 'bm-' + b.id, layout: { visibility: 'none' }, paint: { 'raster-fade-duration': 150 } });
  }
  style.sources['dem'] = { ...DEM };
  style.sources['dem-hs'] = { ...DEM };
  layers.splice(bgIndex + 1, 0, ...rasterLayers);
  const firstSymbol = layers.findIndex(l => l.type === 'symbol');
  const hill = { id: 'bm-hillshade', type: 'hillshade', source: 'dem-hs', layout: { visibility: 'none' },
    paint: { 'hillshade-exaggeration': 0.5, 'hillshade-shadow-color': '#04060c', 'hillshade-highlight-color': '#8e9bb6', 'hillshade-accent-color': '#151d2f' } };
  layers.splice(firstSymbol > 0 ? firstSymbol : layers.length, 0, hill);
  for (const id of SLOT_MARKERS) layers.push({ id, type: 'background', layout: { visibility: 'none' }, paint: { 'background-color': '#000' } });
  style.layers = layers;
  return style;
}

export async function createMap(container) {
  const style = prepareStyle(await loadBaseStyle());
  state = { basemap: settingsGet('basemap', 'dark'), projection: settingsGet('projection', 'mercator'), terrain: settingsGet('terrain', false) };
  map = new maplibregl.Map({
    container,
    style,
    center: [0, 30],
    zoom: 2.2,
    hash: 'map',
    maxPitch: 80,
    attributionControl: { compact: true },
    fadeDuration: 150,
    dragRotate: true,
    pitchWithRotate: true,
    localIdeographFontFamily: "'Noto Sans', 'Microsoft YaHei', sans-serif",
  });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'bottom-right');
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');
  map.on('styleimagemissing', (e) => { if (icons.has(e.id)) addIcon(e.id); });
  await new Promise((resolve) => map.once('load', resolve));
  setBasemap(state.basemap, false);
  setProjection(state.projection, false);
  if (state.terrain) setTerrain(true, false);
  wireDispatch();
  wireReadouts();
  map.on('moveend', debounce(() => bus.emit('map:moveend', { bounds: viewBounds(), zoom: map.getZoom() }), 120));
  bus.emit('map:ready', { map });
  return map;
}

// ------------------------------------------------------------------ basemaps, projection, terrain
export function setBasemap(id, persist = true) {
  const def = BASEMAPS.find(b => b.id === id) || BASEMAPS[0];
  const raster = !!def.raster;
  const vis = (layerId, on) => { if (map.getLayer(layerId)) map.setLayoutProperty(layerId, 'visibility', on ? 'visible' : 'none'); };
  for (const b of BASEMAPS) if (b.raster) vis('bm-' + b.id, b.id === def.id);
  for (const lid of baseVectorIds) {
    if (lid.startsWith('bm-')) continue;
    let on = !raster;
    if (raster && def.labels && (baseSymbolIds.includes(lid) || keepOnImagery.includes(lid))) on = true;
    vis(lid, on);
  }
  vis('bm-hillshade', def.id === 'relief');
  state.basemap = def.id;
  if (persist) settingsSet('basemap', def.id);
  bus.emit('basemap:changed', { id: def.id });
}

export function setProjection(type, persist = true) {
  const t = type === 'globe' ? 'globe' : 'mercator';
  try {
    map.setProjection({ type: t });
    if (t === 'globe' && map.setSky) {
      map.setSky({ 'sky-color': '#0b1224', 'horizon-color': '#1d3157', 'fog-color': '#0b1224', 'sky-horizon-blend': 0.6,
        'horizon-fog-blend': 0.6, 'fog-ground-blend': 0.6, 'atmosphere-blend': ['interpolate', ['linear'], ['zoom'], 0, 1, 5, 1, 7, 0] });
    }
  } catch (err) {
    console.warn('projection change failed', err);
  }
  state.projection = t;
  if (persist) settingsSet('projection', t);
  bus.emit('projection:changed', { type: t });
}

export function setTerrain(on, persist = true) {
  try {
    if (on) {
      map.setTerrain({ source: 'dem', exaggeration: 1.35 });
      if (map.getPitch() < 30) map.easeTo({ pitch: 55, duration: reducedMotion() ? 0 : 800 });
    } else {
      map.setTerrain(null);
    }
  } catch (err) {
    console.warn('terrain change failed', err);
  }
  state.terrain = !!on;
  if (persist) settingsSet('terrain', !!on);
  bus.emit('terrain:changed', { on: !!on });
}

export function viewState() { return { ...state }; }

// ------------------------------------------------------------------ geometry helpers
export function viewBounds() {
  const b = map.getBounds();
  let w = b.getWest();
  let e = b.getEast();
  const s = Math.max(-85, b.getSouth());
  const n = Math.min(85, b.getNorth());
  if (state.projection === 'globe' && map.getZoom() < 2.5) return [-180, -85, 180, 85];
  if (e - w >= 360) return [-180, s, 180, n];
  const wrap = (x) => ((x + 180) % 360 + 360) % 360 - 180;
  w = wrap(w);
  e = wrap(e);
  if (e === -180) e = 180;
  return [+w.toFixed(4), +s.toFixed(4), +e.toFixed(4), +n.toFixed(4)];
}

export function reducedMotion() {
  return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export function flyTo(lat, lon, zoom) {
  const z = zoom ?? Math.max(map.getZoom(), 7);
  if (reducedMotion()) map.jumpTo({ center: [lon, lat], zoom: z });
  else map.flyTo({ center: [lon, lat], zoom: z, speed: 1.6, curve: 1.4, essential: true });
}

export function fitBbox(bbox, opts = {}) {
  const [w, s, e, n] = bbox;
  map.fitBounds([[w, s], [e, n]], { padding: 60, maxZoom: 12, duration: reducedMotion() ? 0 : 900, ...opts });
}

// ------------------------------------------------------------------ overlay helpers
export function addSource(id, spec) {
  const existing = map.getSource(id);
  if (existing) {
    if (spec.type === 'geojson' && spec.data !== undefined && existing.setData) existing.setData(spec.data);
    return existing;
  }
  map.addSource(id, spec);
  return map.getSource(id);
}

export function addLayer(spec, slot = 'points') {
  if (map.getLayer(spec.id)) return map.getLayer(spec.id);
  const i = SLOTS.indexOf(slot);
  const before = SLOT_MARKERS[(i < 0 ? 2 : i) + 1];
  map.addLayer(spec, before);
  return map.getLayer(spec.id);
}

export function removeLayer(id) {
  if (map.getLayer(id)) map.removeLayer(id);
}

export function removeSource(id) {
  if (!map.getSource(id)) return;
  for (const l of map.getStyle().layers) if (l.source === id) map.removeLayer(l.id);
  map.removeSource(id);
}

export function setVisible(id, on) {
  if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none');
}

export function setData(sourceId, data) {
  const src = map.getSource(sourceId);
  if (src && src.setData) src.setData(data);
}

export const emptyFC = () => ({ type: 'FeatureCollection', features: [] });

// ------------------------------------------------------------------ icons
/** Register a canvas-drawn icon. draw(ctx2d, size) paints into a size×size box. sdf icons take icon-color. */
export function icon(name, draw, { size = 32, sdf = true, pixelRatio = 2 } = {}) {
  icons.set(name, { draw, size, sdf, pixelRatio });
  addIcon(name);
}

function addIcon(name) {
  const spec = icons.get(name);
  if (!spec || map.hasImage(name)) return;
  const px = Math.round(spec.size * spec.pixelRatio);
  // SDF icons get a margin so the distance field (and icon halos) can extend past the shape.
  const buf = spec.sdf ? SDF_RADIUS : 0;
  const w = px + 2 * buf;
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = w;
  const g = canvas.getContext('2d', { willReadFrequently: true });
  g.translate(buf, buf);
  g.scale(spec.pixelRatio, spec.pixelRatio);
  spec.draw(g, spec.size);
  const img = g.getImageData(0, 0, w, w);
  const data = spec.sdf ? toSdf(img.data, w, w) : img.data;
  map.addImage(name, { width: w, height: w, data }, { sdf: spec.sdf, pixelRatio: spec.pixelRatio });
}

// ---- signed distance field from a drawn shape (TinySDF method, Felzenszwalb–Huttenlocher EDT) ----
const SDF_RADIUS = 8;      // device pixels of field either side of the edge
const SDF_CUTOFF = 0.25;   // edge lands at 0.75, which is where MapLibre's SDF shader draws it
const INF = 1e20;

function toSdf(rgba, width, height) {
  const n = width * height;
  const outer = new Float64Array(n);
  const inner = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const a = rgba[i * 4 + 3] / 255;
    if (a >= 1) { outer[i] = 0; inner[i] = INF; }
    else if (a <= 0) { outer[i] = INF; inner[i] = 0; }
    else {
      const d = 0.5 - a;
      outer[i] = d > 0 ? d * d : 0;
      inner[i] = d < 0 ? d * d : 0;
    }
  }
  const size = Math.max(width, height);
  const f = new Float64Array(size);
  const z = new Float64Array(size + 1);
  const v = new Uint16Array(size);
  edt(outer, width, height, f, v, z);
  edt(inner, width, height, f, v, z);
  const out = new Uint8ClampedArray(n * 4);
  for (let i = 0; i < n; i++) {
    const d = Math.sqrt(outer[i]) - Math.sqrt(inner[i]);
    out[i * 4] = 255;
    out[i * 4 + 1] = 255;
    out[i * 4 + 2] = 255;
    out[i * 4 + 3] = Math.round(255 - 255 * (d / SDF_RADIUS + SDF_CUTOFF));
  }
  return out;
}

function edt(grid, width, height, f, v, z) {
  for (let x = 0; x < width; x++) edt1d(grid, x, width, height, f, v, z);
  for (let y = 0; y < height; y++) edt1d(grid, y * width, 1, width, f, v, z);
}

function edt1d(grid, offset, stride, length, f, v, z) {
  v[0] = 0;
  z[0] = -INF;
  z[1] = INF;
  f[0] = grid[offset];
  for (let q = 1, k = 0, s = 0; q < length; q++) {
    f[q] = grid[offset + q * stride];
    const q2 = q * q;
    do {
      const r = v[k];
      s = (f[q] - f[r] + q2 - r * r) / (q - r) / 2;
    } while (s <= z[k] && --k > -1);
    k++;
    v[k] = q;
    z[k] = s;
    z[k + 1] = INF;
  }
  for (let q = 0, k = 0; q < length; q++) {
    while (z[k + 1] < q) k++;
    const r = v[k];
    const qr = q - r;
    grid[offset + q * stride] = f[r] + qr * qr;
  }
}

// ------------------------------------------------------------------ click and hover dispatch
/** handler(feature, lngLat, event, allFeatures). Topmost feature wins across all registered layers. */
export function onClick(layerIds, handler) {
  clickables.push({ layers: [].concat(layerIds), handler });
}

/** text(feature) -> string | null, shown as a tooltip; the cursor turns into a pointer either way. */
export function onHover(layerIds, text) {
  hovers.push({ layers: [].concat(layerIds), text });
}

function presentIds(list) {
  const ids = [];
  for (const c of list) for (const id of c.layers) if (map.getLayer(id)) ids.push(id);
  return ids;
}

let dispatchPaused = false;

/** While drawing (ctx.draw) the core click and hover dispatch stand down: no cards, no tooltips,
 *  no pointer cursor and no map:click-empty. */
export function setDispatchPaused(on) {
  dispatchPaused = !!on;
  if (dispatchPaused && tipEl) tipEl.hidden = true;
}

function wireDispatch() {
  map.on('click', (e) => {
    if (dispatchPaused) return;
    const ids = presentIds(clickables);
    if (ids.length) {
      const pad = 6;
      const feats = map.queryRenderedFeatures([[e.point.x - pad, e.point.y - pad], [e.point.x + pad, e.point.y + pad]], { layers: ids });
      if (feats.length) {
        const f = feats[0];
        const c = clickables.find(cl => cl.layers.includes(f.layer.id));
        try { c.handler(f, e.lngLat, e, feats); } catch (err) { console.error('click handler failed', err); }
        return;
      }
    }
    bus.emit('map:click-empty', { lngLat: e.lngLat, point: e.point, originalEvent: e.originalEvent });
  });
  tipEl = h('div', { class: 'map-tip', hidden: true });
  map.getContainer().parentElement.append(tipEl);
  const move = throttle((e) => {
    if (dispatchPaused) { tipEl.hidden = true; return; }
    const ids = [...new Set([...presentIds(clickables), ...presentIds(hovers)])];
    if (!ids.length) { map.getCanvas().style.cursor = ''; tipEl.hidden = true; return; }
    const feats = map.queryRenderedFeatures([[e.point.x - 4, e.point.y - 4], [e.point.x + 4, e.point.y + 4]], { layers: ids });
    if (!feats.length) { map.getCanvas().style.cursor = ''; tipEl.hidden = true; return; }
    const f = feats[0];
    const clickable = clickables.some(c => c.layers.includes(f.layer.id));
    map.getCanvas().style.cursor = clickable ? 'pointer' : '';
    const hv = hovers.find(hh => hh.layers.includes(f.layer.id));
    let text = null;
    if (hv) { try { text = hv.text(f); } catch { text = null; } }
    if (text) {
      tipEl.textContent = text;
      tipEl.hidden = false;
      const x = e.point.x + 14;
      const y = e.point.y + 14;
      tipEl.style.left = `${Math.min(x, map.getContainer().clientWidth - 330)}px`;
      tipEl.style.top = `${y}px`;
    } else {
      tipEl.hidden = true;
    }
  }, 40);
  map.on('mousemove', move);
  map.on('mouseout', () => { tipEl.hidden = true; });
  map.on('movestart', () => { tipEl.hidden = true; });
}

function wireReadouts() {
  const pos = document.getElementById('cursor-pos');
  const zoom = document.getElementById('zoom-level');
  map.on('mousemove', throttle((e) => { if (pos) pos.textContent = fmt.latlon(e.lngLat.lat, e.lngLat.lng, 4); }, 60));
  const z = () => { if (zoom) zoom.textContent = `Zoom ${map.getZoom().toFixed(1)}`; };
  map.on('zoom', throttle(z, 100));
  z();
}

// ------------------------------------------------------------------ highlight ring (selected entity)
export function highlight(lat, lon, color = '#6cb6ff') {
  addSource('ov-core-highlight', { type: 'geojson', data: emptyFC() });
  addLayer({ id: 'ov-core-highlight-ring', type: 'circle', source: 'ov-core-highlight',
    paint: { 'circle-radius': 15, 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-width': 2.5, 'circle-stroke-color': ['get', 'color'], 'circle-pitch-alignment': 'map' } }, 'top');
  setData('ov-core-highlight', { type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'Point', coordinates: [lon, lat] }, properties: { color } }] });
}

export function clearHighlight() {
  setData('ov-core-highlight', emptyFC());
}

// ------------------------------------------------------------------ view controls (top bar)
export function buildViewControls(el, { openMenu }) {
  const seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Map projection' });
  const flat = h('button', { type: 'button', title: 'Flat map (Mercator)', 'aria-pressed': String(state.projection !== 'globe') }, '2D');
  const globe = h('button', { type: 'button', title: 'Globe', 'aria-pressed': String(state.projection === 'globe') }, 'Globe');
  flat.addEventListener('click', () => setProjection('mercator'));
  globe.addEventListener('click', () => setProjection('globe'));
  bus.on('projection:changed', ({ type }) => {
    flat.setAttribute('aria-pressed', String(type !== 'globe'));
    globe.setAttribute('aria-pressed', String(type === 'globe'));
  });
  seg.append(flat, globe);
  const baseBtn = h('button', { class: 'icon-btn', type: 'button', title: 'Basemap and terrain', 'aria-haspopup': 'menu' },
    svgIcon('M10 2.5 2.5 6.5 10 10.5l7.5-4L10 2.5Zm-7.5 7 7.5 4 7.5-4M2.5 13.5l7.5 4 7.5-4'), h('span', { class: 'label' }, 'Basemap'));
  baseBtn.addEventListener('click', () => {
    const items = BASEMAPS.map(b => ({ text: b.title, note: b.note, checked: state.basemap === b.id, onClick: () => setBasemap(b.id) }));
    items.push({ separator: true });
    items.push({ text: '3D terrain', checked: state.terrain, onClick: () => setTerrain(!state.terrain) });
    openMenu(baseBtn, items);
  });
  el.append(seg, baseBtn);
}

export function getMap() { return map; }
