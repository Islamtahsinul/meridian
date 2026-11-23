// Space module: NASA GIBS true-colour imagery, night lights, the imagery products of the extension pack
// (daily night lights, flood maps, Sentinel-1 radar, Meteosat) and the before/after compare tool.

import { COLOR, addRaster, gibsCatalogue, gibsMeta, gibsUrl, productTileUrl, isoDate, addDays, validDate, dateLabel, clamp, pickDate,
  watchTileErrors, resetTileErrors } from './common.js';
import { registerProductLayer, shownLayers, compositeTimes } from './gibs.js';

const SOURCES = [
  { value: 'terra', label: 'MODIS Terra (morning)' }, { value: 'aqua', label: 'MODIS Aqua (afternoon)' },
  { value: 'noaa20', label: 'VIIRS NOAA-20 (afternoon)' }, { value: 'noaa21', label: 'VIIRS NOAA-21 (afternoon)' },
  { value: 'snpp', label: 'VIIRS Suomi NPP' },
];
const RANK = { daily: 10, nightlights: 12, coast: 89, ref: 90 };
// Imagery-group products of the catalogue (imagery.py IMAGERY), registered through gibs.js
const PRODUCT_LAYERS = ['nightdaily', 'flood', 'sar', 'meteosat'];
const PRODUCT_FALLBACK = {
  nightdaily: { title: 'Night lights, daily', subtitle: 'VIIRS, useful for spotting blackouts', group: 'Imagery', period: 'P1D' },
  flood: { title: 'Flood map', group: 'Imagery', period: 'P1D' }, sar: { title: 'Radar, Sentinel-1', group: 'Imagery', period: 'P1D' },
  meteosat: { title: 'Meteosat, Europe and Africa', group: 'Imagery', period: 'PT10M' },
};
// The before/after tool: true colour plus the products where a change between two days means something
const TC_IDS = new Set(['terra', 'aqua', 'noaa20', 'noaa21', 'snpp']);
const COMPARE = [
  { value: 'terra', label: 'MODIS Terra (morning)', group: 'True colour', min: '2000-02-24' },
  { value: 'aqua', label: 'MODIS Aqua (afternoon)', group: 'True colour', min: '2002-07-03' },
  { value: 'noaa20', label: 'VIIRS NOAA-20 (afternoon)', group: 'True colour', min: '2018-01-01' },
  { value: 'noaa21', label: 'VIIRS NOAA-21 (afternoon)', group: 'True colour', min: '2023-02-10' },
  { value: 'snpp', label: 'VIIRS Suomi NPP', group: 'True colour', min: '2012-01-19' },
  { value: 'nightdaily', label: 'Night lights (blackouts)', group: 'Other views', min: '2024-02-05' },
  { value: 'flood', label: 'Flood map (VIIRS, 2 days)', group: 'Other views', min: '2025-06-24', overlay: true },
  { value: 'sar', label: 'Radar, Sentinel-1 (6 days to each date)', group: 'Other views', min: '2025-01-10', overlay: true },
  { value: 'no2', label: 'Nitrogen dioxide (Sentinel-5P)', group: 'Other views', min: '2018-05-06', overlay: true },
];
const COMPARE_ALIASES = { nightlights: 'nightdaily', night: 'nightdaily', blackout: 'nightdaily', radar: 'sar', floods: 'flood' };
const CATALOGUE_IDS = [...TC_IDS, 'nightdaily', 'flood', 'sar', 'no2'];
const COMPARE_NOTE = {
  tc: 'corrected reflectance (true colour). One mosaic per day from polar orbits, so cloud and swath gaps differ between days.',
  nightdaily: 'VIIRS night lights on NOAA-20, moonlight corrected, with cloudy spots filled from earlier nights: a blackout under cloud can stay hidden.',
  flood: 'VIIRS flood product, 2-day composite: red is flooding, yellow land that floods most years, blue normal water, grey cloud.',
  sar: 'OPERA radar from Copernicus Sentinel-1; each side combines the six days up to its date, newest on top.',
  no2: 'Copernicus Sentinel-5P nitrogen dioxide (TROPOMI), published about ten days late; cloudy areas are blank.',
};
const BASE_FILL = /^(background|landcover|landuse|park|water)/;
const FALLBACK_BASE = 'https://gibs.earthdata.nasa.gov/wmts/epsg3857/best';
const TC = { matrix: 'GoogleMapsCompatible_Level9', maxzoom: 9, ext: 'jpg' };
const LAYER_ID = { terra: 'MODIS_Terra_CorrectedReflectance_TrueColor', aqua: 'MODIS_Aqua_CorrectedReflectance_TrueColor',
  noaa20: 'VIIRS_NOAA20_CorrectedReflectance_TrueColor', noaa21: 'VIIRS_NOAA21_CorrectedReflectance_TrueColor',
  snpp: 'VIIRS_SNPP_CorrectedReflectance_TrueColor' };
// GIBS Reference_Features/Labels currently fail (HTTP 500), so the main map and the compare maps copy the
// basemap's own vector borders and place names above the imagery; without a vector basemap the main map
// falls back to GIBS Coastlines_15m.
const COAST = ['Coastlines_15m', 'GoogleMapsCompatible_Level13', 13];
const BASE_REF = /^(boundary_(state|country_outline|country_inner)|place_(country_1|country_2|state|continent|city_r5|city_r6|city_dot_r7|city_dot_r4|city_dot_r2|city_dot_z7|capital_dot_z7|town)|watername_(ocean|sea))$/;

const I = { ctx: null, cat: null, daily: null, night: null, compare: null };

function base() { return I.cat?.base || FALLBACK_BASE; }

function yesterday() { return addDays(isoDate(Date.now()), -1); }

/** The catalogue layer a source shows (the first variant of products that have variants). */
function sourceLayer(source) {
  const p = I.cat?.products?.[source];
  return p ? (TC_IDS.has(source) ? p.layers?.[0] : shownLayers(p)[0]) : null;
}

/** Latest complete day for a source (from the GIBS domain), or yesterday. */
function latestDay(source) {
  const le = sourceLayer(source);
  return le?.default || I.cat?.products?.[source]?.default_time || yesterday();
}

function availableDays(source) {
  return sourceLayer(source)?.times || [];
}

function tcUrl(source, date) {
  return gibsUrl(base(), LAYER_ID[source] || LAYER_ID.terra, TC.matrix, TC.ext, date);
}

function coastUrl() {
  return `${base()}/${COAST[0]}/default/${COAST[1]}/{z}/{y}/{x}.png`;
}

async function refreshCatalogue() {
  try { I.cat = await gibsCatalogue(I.ctx.api, { products: CATALOGUE_IDS, maxAgeMs: 30 * 60000 }); } catch (err) { if (!I.cat) throw err; }
  return I.cat;
}

// ------------------------------------------------------------------ daily true colour layer
function dailyDate(options) {
  const src = options.source || 'terra';
  const latest = latestDay(src);
  const when = options.when || 'latest';
  if (when === 'custom') {
    const d = I.ctx.settings.get('space.daily.date', null);
    return validDate(d) ? d : latest;
  }
  const m = /^d(\d+)$/.exec(when);
  return m ? addDays(latest, -Number(m[1])) : latest;
}

function applyDaily(options) {
  const { map, map2 } = I.ctx;
  const src = options.source || 'terra';
  const date = dailyDate(options);
  const url = tcUrl(src, date);
  const sid = 'ov-space-daily';
  const s = map.getSource(sid);
  // a new day or satellite gets a new source rather than setTiles() (see the note in weather.js apply)
  if (s && s._spaceUrl !== url) { map2.removeSource(sid); resetTileErrors('ov-space-daily'); }
  if (!map.getSource(sid)) {
    map2.addSource(sid, { type: 'raster', tiles: [url], tileSize: 256, maxzoom: TC.maxzoom,
      attribution: '<a href="https://www.earthdata.nasa.gov/gibs">NASA GIBS</a> true colour' });
    map.getSource(sid)._spaceUrl = url;
  }
  if (!map.getLayer(sid)) addRaster(map, map2, { id: sid, type: 'raster', source: sid, paint: { 'raster-opacity': (options.opacity ?? 100) / 100, 'raster-fade-duration': 250 } }, RANK.daily);
  else map.setPaintProperty(sid, 'raster-opacity', (options.opacity ?? 100) / 100);
  setReference(options.reference !== false, 'daily');
  const days = availableDays(src);
  const missing = days.length && !days.includes(date) && date > days[days.length - 1];
  I.daily.setCount(dateLabel(date, false));
  if (missing) I.daily.setStatus('warn', `No ${SOURCES.find(x => x.value === src)?.label || src} imagery for ${dateLabel(date)} yet. Pick an earlier day.`);
  else if (src === 'snpp' && days.length && days[days.length - 1] < addDays(isoDate(Date.now()), -5)) I.daily.setStatus('warn', `Suomi NPP imagery currently ends on ${dateLabel(days[days.length - 1])}.`);
  else I.daily.setStatus('ok');
}

/** Restyled copies of the basemap's vector borders and place names, for use above opaque imagery.
 *  Returns null when the vector basemap is not available (the raster fallback style). */
function refSpecs(style, prefix) {
  const base = (style.layers || []).filter(l => BASE_REF.test(l.id) && style.sources[l.source] && style.sources[l.source].type === 'vector');
  if (!base.length) return null;
  const layers = base.map((l) => {
    const spec = { id: `${prefix}${l.id}`, type: l.type, source: l.source, 'source-layer': l['source-layer'], layout: { ...(l.layout || {}), visibility: 'visible' } };
    if (l.filter) spec.filter = l.filter;
    if (l.minzoom != null) spec.minzoom = l.minzoom;
    if (l.maxzoom != null) spec.maxzoom = l.maxzoom;
    const width = (l.paint || {})['line-width'] ?? 0.8;
    if (l.type === 'line') {
      // the basemap draws country borders as a wide dark casing plus a thin inner line
      spec.paint = /outline/.test(l.id)
        ? { 'line-color': COLOR.ink, 'line-opacity': 0.35, 'line-width': width }
        : { 'line-color': COLOR.paper, 'line-opacity': /country/.test(l.id) ? 0.75 : 0.35, 'line-width': /country/.test(l.id) ? 1 : 0.6 };
    } else {
      spec.paint = { 'text-color': /watername/.test(l.id) ? '#bcd3e6' : COLOR.paper, 'text-halo-color': COLOR.ink, 'text-halo-width': 1.4, 'icon-opacity': 0.85 };
    }
    return spec;
  });
  const sources = {};
  for (const l of base) sources[l.source] = style.sources[l.source];
  return { layers, sources, glyphs: style.glyphs, sprite: style.sprite };
}

const refUsers = new Set();
let refIds = [];
/** Borders and place names above opaque imagery on the main map, or GIBS coastlines without a vector basemap. */
function setReference(on, who) {
  const { map, map2 } = I.ctx;
  if (on) refUsers.add(who); else refUsers.delete(who);
  if (refUsers.size && !refIds.length) {
    const ref = refSpecs(map.getStyle(), 'ov-space-ref-');
    for (const spec of ref?.layers || []) {
      try { addRaster(map, map2, spec, RANK.ref); refIds.push(spec.id); } catch (err) { console.warn('space: label copy failed', spec.id, err.message); }
    }
    if (!refIds.length) {
      map2.addSource('ov-space-ref-coast', { type: 'raster', tiles: [coastUrl()], tileSize: 256, maxzoom: COAST[2] });
      addRaster(map, map2, { id: 'ov-space-ref-coast', type: 'raster', source: 'ov-space-ref-coast', paint: { 'raster-opacity': 0.85 } }, RANK.coast);
      refIds.push('ov-space-ref-coast');
    }
  } else if (!refUsers.size && refIds.length) {
    for (const id of refIds) { if (id === 'ov-space-ref-coast') map2.removeSource(id); else map2.removeLayer(id); }
    refIds = [];
  }
}

function registerDaily(ctx) {
  watchTileErrors(ctx.map, 'ov-space-daily', () => I.daily?.isOn() && I.daily.setStatus('warn', 'Some imagery tiles did not load from NASA GIBS. That day may still be processing.'));
  watchTileErrors(ctx.map, 'ov-space-ref-coast', () => {});
  watchTileErrors(ctx.map, 'ov-space-night', () => I.night?.isOn() && I.night.setStatus('warn', 'Some night-light tiles did not load.'));
  I.daily = ctx.layers.register({
    id: 'space.daily', group: 'Imagery', title: 'Daily true colour', subtitle: 'MODIS and VIIRS, one picture of the planet per day',
    description: 'How the Earth looked on a chosen day: clouds, smoke, dust, floods, snow and ice. Each satellite passes once a day, so dark wedges between swaths are normal near the equator.',
    source: 'NASA GIBS, MODIS and VIIRS corrected reflectance', color: COLOR.imagery, symbol: 'area', refreshMs: 3 * 3600000,
    options: [
      { id: 'source', label: 'Satellite', type: 'select', choices: SOURCES, default: 'terra', refresh: false },
      { id: 'when', label: 'Day', type: 'select', default: 'latest', refresh: false, choices: [
        { value: 'latest', label: 'Latest complete day' }, { value: 'd1', label: '1 day earlier' }, { value: 'd2', label: '2 days earlier' },
        { value: 'd3', label: '3 days earlier' }, { value: 'd7', label: 'A week earlier' }, { value: 'custom', label: 'A day I choose' }] },
      { id: 'opacity', label: 'Opacity', type: 'range', min: 20, max: 100, step: 5, default: 100, format: v => `${v}%`, refresh: false },
      { id: 'reference', label: 'Borders and place names', type: 'toggle', default: true, refresh: false },
    ],
    async refresh({ options, signal }) {
      await refreshCatalogue();
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      applyDaily(options);
    },
    async disable() {
      ctx.map2.removeSource('ov-space-daily');
      setReference(false, 'daily');
    },
    async onOption(id, value, all) {
      if (id === 'when' && value === 'custom') {
        const d = await pickDate(ctx, ctx.settings.get('space.daily.date', null) || latestDay(all.source || 'terra'),
          { note: 'MODIS Terra goes back to February 2000, Aqua to July 2002, VIIRS to 2012.' });
        if (d) ctx.settings.set('space.daily.date', d);
      }
      if (I.daily.isOn()) applyDaily(I.daily.options());
    },
  });

  I.night = ctx.layers.register({
    id: 'space.nightlights', group: 'Imagery', title: 'Night lights', subtitle: 'VIIRS Black Marble, 2016',
    description: 'Cloud-free composite of the Earth at night. Bright areas are cities, industry, gas flares and fishing fleets.',
    source: 'NASA Black Marble via GIBS', color: COLOR.imagery, symbol: 'area',
    options: [{ id: 'opacity', label: 'Opacity', type: 'range', min: 20, max: 100, step: 5, default: 90, format: v => `${v}%`, refresh: false }],
    async enable({ options }) {
      const { map, map2 } = ctx;
      map2.addSource('ov-space-night', { type: 'raster', tiles: [gibsUrl(base(), 'VIIRS_Black_Marble', 'GoogleMapsCompatible_Level8', 'png', '2016-01-01')],
        tileSize: 256, maxzoom: 8, attribution: '<a href="https://earthobservatory.nasa.gov/features/NightLights">NASA Black Marble</a>' });
      addRaster(map, map2, { id: 'ov-space-night', type: 'raster', source: 'ov-space-night', paint: { 'raster-opacity': (options.opacity ?? 90) / 100 } }, RANK.nightlights);
      setReference(true, 'night');
      I.night.setCount('2016');
    },
    async disable() { ctx.map2.removeSource('ov-space-night'); setReference(false, 'night'); },
    onOption(id, value) { if (I.night.isOn() && ctx.map.getLayer('ov-space-night')) ctx.map.setPaintProperty('ov-space-night', 'raster-opacity', value / 100); },
  });
}

// ------------------------------------------------------------------ compare tool
const gibsSource = (url, maxzoom = TC.maxzoom) => ({ type: 'raster', tiles: [url], tileSize: 256, maxzoom, attribution: 'NASA GIBS' });
const gibsLayer = (id = 'gibs') => ({ id, type: 'raster', source: id, paint: { 'raster-fade-duration': 150 } });
const isGibsId = (id) => /^gibs(-[0-9]+)?$/.test(id);

/** The raster sources one side of the compare tool shows for a day: one, or a composite (radar). */
function compareLayers(source, date) {
  const product = I.cat?.products?.[source];
  if (TC_IDS.has(source) || !product) return [{ id: 'gibs', url: tcUrl(source, date), maxzoom: TC.maxzoom }];
  const le = shownLayers(product)[0];
  if (!le) return [];
  const days = product.composite ? compositeTimes(product, le, date) : [date];
  return days.map((d, k) => ({ id: k ? `gibs-${k}` : 'gibs', url: productTileUrl(base(), product, le, d), maxzoom: le.maxzoom || product.maxzoom || 9 }));
}

/** Dark land and water from the basemap, under overlay products (floods, radar, NO2) in the compare maps. */
function baseSpecs(style, prefix) {
  const src = (style.layers || []).filter(l => BASE_FILL.test(l.id)
    && (l.type === 'background' || (l.type === 'fill' && style.sources[l.source] && style.sources[l.source].type === 'vector')));
  if (!src.some(l => l.type === 'fill')) return null;
  const layers = src.map((l) => {
    const spec = { id: `${prefix}${l.id}`, type: l.type, paint: { ...(l.paint || {}) }, layout: { ...(l.layout || {}), visibility: 'visible' } };
    if (l.type !== 'background') { spec.source = l.source; spec['source-layer'] = l['source-layer']; }
    if (l.filter) spec.filter = l.filter;
    if (l.minzoom != null) spec.minzoom = l.minzoom;
    if (l.maxzoom != null) spec.maxzoom = l.maxzoom;
    return spec;
  });
  const sources = {};
  for (const l of src) if (l.source) sources[l.source] = style.sources[l.source];
  return { layers, sources };
}

function compareStyle(source, date, withRef) {
  const main = I.ctx.map.getStyle();
  const ref = refSpecs(main, 'ref-');
  const style = { version: 8, sources: {}, layers: [{ id: 'bg', type: 'background', paint: { 'background-color': '#05070c' } }] };
  if (COMPARE.find(c => c.value === source)?.overlay) {
    const b = baseSpecs(main, 'base-');
    if (b) { Object.assign(style.sources, b.sources); style.layers = b.layers; }
  }
  for (const l of compareLayers(source, date)) { style.sources[l.id] = gibsSource(l.url, l.maxzoom); style.layers.push(gibsLayer(l.id)); }
  if (ref) {
    Object.assign(style.sources, ref.sources);
    if (ref.glyphs) style.glyphs = ref.glyphs;
    if (ref.sprite) style.sprite = ref.sprite;
    if (withRef) style.layers.push(...ref.layers);
  }
  return style;
}

function setMapRef(map, on) {
  const ref = refSpecs(I.ctx.map.getStyle(), 'ref-');
  if (!ref) return;
  for (const spec of ref.layers) {
    if (on && !map.getLayer(spec.id) && map.getSource(spec.source)) map.addLayer(spec);
    else if (!on && map.getLayer(spec.id)) map.removeLayer(spec.id);
  }
}

function clampDay(source, date, { complete = false } = {}) {
  const days = availableDays(source);
  // GIBS lists today as soon as the first swaths arrive, so a tool opened from a card (complete) starts
  // from the latest complete day instead of a mostly black "today". Dates picked by hand may still use today.
  const latest = complete ? latestDay(source) : days.length ? days[days.length - 1] : latestDay(source);
  if (date > latest) return { date: latest, clamped: true };
  return { date, clamped: false };
}

/**
 * Both compare dates after a change of source: each is clamped to the source's latest day, and when that
 * leaves Before on or after After (a source that ends earlier, like NO2), Before moves a week before After,
 * as on opening. Returns the new dates and the note to show ('' for none).
 */
export function pairForSource(before, after, clampFn, label) {
  const b = clampFn(before);
  const a = clampFn(after);
  let nb = b.date;
  const na = a.date;
  if (!b.clamped && !a.clamped) return { before: nb, after: na, note: '' };
  const latest = a.clamped ? a.date : b.date;
  let moved = false;
  if (nb >= na) { nb = addDays(na, -7); moved = true; }
  return { before: nb, after: na, note: `No ${label} imagery after ${dateLabel(latest)} yet${moved ? ', so both sides moved back' : ''}.` };
}

let openSeq = 0;
export async function openCompare(payload = {}) {
  const ctx = I.ctx;
  const { h, ui } = ctx;
  const my = ++openSeq;
  try { await refreshCatalogue(); } catch { /* fall back to computed dates */ }
  if (my !== openSeq) return null;   // a newer call (a double click) opens the tool instead
  if (I.compare) I.compare.close();
  const main = ctx.map;
  const c = main.getCenter();
  const lat = ctx.dom.validLatLon(payload.lat, payload.lon) ? +payload.lat : c.lat;
  const lon = ctx.dom.validLatLon(payload.lat, payload.lon) ? +payload.lon : c.lng;
  const zoom = clamp(Number.isFinite(+payload.zoom) ? +payload.zoom : main.getZoom(), 2, 9);
  const asked = COMPARE_ALIASES[payload.source] || payload.source;
  const saved = ctx.settings.get('space.compare.source', 'terra');
  let source = COMPARE.some(c => c.value === asked) ? asked : (COMPARE.some(c => c.value === saved) ? saved : 'terra');
  let after = validDate(payload.after) ? payload.after : validDate(payload.date) ? addDays(payload.date, 1) : latestDay(source);
  let before = validDate(payload.before) ? payload.before : validDate(payload.date) ? addDays(payload.date, -3) : addDays(after, -7);
  if (before > after) [before, after] = [after, before];
  const notes = [];
  const ca = clampDay(source, after, { complete: true });
  if (ca.clamped) { notes.push(`There is no complete imagery for ${dateLabel(after)} yet, so the right side shows ${dateLabel(ca.date)}, the latest full day.`); after = ca.date; if (before >= after) before = addDays(after, -7); }
  let withRef = ctx.settings.get('space.compare.reference', true) !== false;
  let mode = ctx.settings.get('space.compare.mode', 'side') === 'swipe' ? 'swipe' : 'side';

  // controls
  const srcSel = h('select', { 'aria-label': 'Imagery' }, ['True colour', 'Other views'].map(g => h('optgroup', { label: g },
    COMPARE.filter(c => c.group === g).map(c => h('option', { value: c.value, selected: c.value === source || null }, c.label)))));
  const today = isoDate(Date.now());
  const minDay = () => COMPARE.find(c => c.value === source)?.min || '2000-02-24';
  const inBefore = h('input', { type: 'date', value: before, min: minDay(), max: today, 'aria-label': 'Before date' });
  const inAfter = h('input', { type: 'date', value: after, min: minDay(), max: today, 'aria-label': 'After date' });
  const swap = h('button', { type: 'button', class: 'btn', title: 'Swap the two dates' }, 'Swap');
  const segSide = h('button', { type: 'button', 'aria-pressed': String(mode === 'side') }, 'Side by side');
  const segSwipe = h('button', { type: 'button', 'aria-pressed': String(mode === 'swipe') }, 'Swipe');
  const refBox = h('input', { type: 'checkbox', id: 'space-cmp-ref', checked: withRef || null });
  const note = h('p', { class: 'space-cmp-note', hidden: !notes.length }, notes.join(' '));
  const chipA = h('div', { class: 'space-cmp-chip' }, '');
  const chipB = h('div', { class: 'space-cmp-chip right' }, '');
  const paneA = h('div', { class: 'space-cmp-map' });
  const paneB = h('div', { class: 'space-cmp-map' });
  const handle = h('div', { class: 'space-cmp-handle', role: 'slider', tabindex: '0', 'aria-label': 'Swipe position', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': '50' }, h('span'));
  const stage = h('div', { class: `space-cmp-stage ${mode}` },
    h('div', { class: 'space-cmp-pane a' }, paneA, chipA), h('div', { class: 'space-cmp-pane b' }, paneB, chipB), handle);
  const controls = h('div', { class: 'space-cmp-controls' },
    h('label', { class: 'space-cmp-field' }, h('span', null, 'Imagery'), srcSel),
    h('label', { class: 'space-cmp-field' }, h('span', null, 'Before'), inBefore),
    h('label', { class: 'space-cmp-field' }, h('span', null, 'After'), inAfter),
    swap,
    h('div', { class: 'seg', role: 'group', 'aria-label': 'Layout' }, segSide, segSwipe),
    h('label', { class: 'space-cmp-check', for: 'space-cmp-ref' }, h('span', { class: 'switch' }, refBox, h('span')), 'Borders and place names'));
  const body = h('div', { class: 'space-cmp' }, controls, note, stage);
  const foot = h('p', { class: 'space-credit space-cmp-foot' });
  const setFoot = () => {
    ctx.dom.clear(foot);
    foot.append('Imagery: ', h('a', { href: 'https://www.earthdata.nasa.gov/gibs' }, 'NASA GIBS'), `, ${COMPARE_NOTE[TC_IDS.has(source) ? 'tc' : source] || ''}`);
  };
  setFoot();

  let mapA = null;
  let mapB = null;
  let closed = false;
  const cleanups = [];
  const m = ui.modal({ title: 'Compare imagery', body, full: true, foot, onClose: () => {
    closed = true;
    for (const fn of cleanups) { try { fn(); } catch { /* ignore */ } }
    try { mapA?.remove(); } catch { /* ignore */ }
    try { mapB?.remove(); } catch { /* ignore */ }
    mapA = null; mapB = null;
    I.compare = null;
  } });
  I.compare = m;

  const chips = () => {
    ctx.dom.clear(chipA); ctx.dom.clear(chipB);
    chipA.append(h('strong', null, 'Before'), ` ${dateLabel(inBefore.value)}`);
    chipB.append(h('strong', null, 'After'), ` ${dateLabel(inAfter.value)}`);
  };
  chips();

  const mk = (container, date) => new ctx.maplibregl.Map({
    container, style: compareStyle(source, date, withRef), center: [lon, lat], zoom, maxZoom: 12, minZoom: 1,
    attributionControl: false, dragRotate: false, pitchWithRotate: false, fadeDuration: 0, renderWorldCopies: true,
  });
  // the stage has its final size only after the modal is laid out
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  if (closed) return null;
  mapA = mk(paneA, inBefore.value);
  mapB = mk(paneB, inAfter.value);
  mapA.addControl(new ctx.maplibregl.NavigationControl({ showCompass: false }), 'bottom-left');
  let syncing = false;
  const sync = (from, to) => () => {
    if (syncing || !to) return;
    syncing = true;
    to.jumpTo({ center: from.getCenter(), zoom: from.getZoom(), bearing: from.getBearing(), pitch: from.getPitch() });
    syncing = false;
  };
  mapA.on('move', sync(mapA, mapB));
  mapB.on('move', sync(mapB, mapA));
  for (const mp of [mapA, mapB]) {
    // a missing tile (a gap day, a server hiccup) is normal here; anything else is worth a warning
    mp.on('error', (e) => { if (!/AJAXError|Failed to fetch|NetworkError|AbortError|(4|5)\d\d/i.test(`${e?.error?.name || ''} ${e?.error?.message || ''}`)) console.warn('compare map', e?.error?.message || e); });
  }

  // a new date gets new sources rather than setTiles() (see the note in gibs.js apply), kept under the labels
  const setTiles = (mp, date) => {
    if (!mp?.getSource('gibs')) return;
    const style = mp.getStyle();
    const labels = style.layers.find(l => l.id.startsWith('ref-'));
    for (const l of style.layers) if (isGibsId(l.id)) mp.removeLayer(l.id);
    for (const id of Object.keys(style.sources)) if (isGibsId(id)) mp.removeSource(id);
    for (const l of compareLayers(source, date)) { mp.addSource(l.id, gibsSource(l.url, l.maxzoom)); mp.addLayer(gibsLayer(l.id), labels?.id); }
  };
  const setDate = (input, mp) => {
    if (!validDate(input.value)) return;
    const res = clampDay(source, input.value);
    if (res.clamped) {
      input.value = res.date;
      note.hidden = false;
      note.textContent = `No ${COMPARE.find(c => c.value === source)?.label || source} imagery after ${dateLabel(res.date)} yet.`;
    } else {
      note.hidden = true;
    }
    setTiles(mp, input.value);
    chips();
  };
  inBefore.addEventListener('change', () => setDate(inBefore, mapA));
  inAfter.addEventListener('change', () => setDate(inAfter, mapB));
  swap.addEventListener('click', () => {
    const a = inBefore.value; inBefore.value = inAfter.value; inAfter.value = a;
    setTiles(mapA, inBefore.value); setTiles(mapB, inAfter.value); chips();
  });
  srcSel.addEventListener('change', () => {
    source = srcSel.value;
    ctx.settings.set('space.compare.source', source);
    inBefore.min = minDay(); inAfter.min = minDay();
    for (const input of [inBefore, inAfter]) if (input.value < input.min) input.value = input.min;
    const pair = pairForSource(inBefore.value, inAfter.value, d => clampDay(source, d), COMPARE.find(c => c.value === source)?.label || source);
    inBefore.value = pair.before; inAfter.value = pair.after;
    for (const [input, mp] of [[inBefore, mapA], [inAfter, mapB]]) {
      mp?.setStyle(compareStyle(source, input.value, withRef));   // overlay products bring their own dark base
    }
    note.hidden = !pair.note;
    note.textContent = pair.note;
    setFoot();
    chips();
  });
  refBox.addEventListener('change', () => {
    withRef = refBox.checked;
    ctx.settings.set('space.compare.reference', withRef);
    for (const mp of [mapA, mapB]) if (mp?.isStyleLoaded()) setMapRef(mp, withRef); else mp?.once('load', () => setMapRef(mp, withRef));
  });

  // layouts
  let split = 0.5;
  const applySplit = () => {
    const w = stage.clientWidth;
    const px = Math.round(w * split);
    stage.style.setProperty('--split', `${px}px`);
    handle.setAttribute('aria-valuenow', String(Math.round(split * 100)));
  };
  const setMode = (next) => {
    mode = next;
    ctx.settings.set('space.compare.mode', mode);
    stage.classList.toggle('side', mode === 'side');
    stage.classList.toggle('swipe', mode === 'swipe');
    segSide.setAttribute('aria-pressed', String(mode === 'side'));
    segSwipe.setAttribute('aria-pressed', String(mode === 'swipe'));
    applySplit();
    requestAnimationFrame(() => { mapA?.resize(); mapB?.resize(); });
  };
  segSide.addEventListener('click', () => setMode('side'));
  segSwipe.addEventListener('click', () => setMode('swipe'));
  const onMove = (e) => {
    const r = stage.getBoundingClientRect();
    split = clamp((e.clientX - r.left) / r.width, 0.02, 0.98);
    applySplit();
  };
  const onUp = () => { window.removeEventListener('pointermove', onMove); window.removeEventListener('pointerup', onUp); stage.classList.remove('dragging'); };
  handle.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    stage.classList.add('dragging');
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  });
  handle.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      split = clamp(split + (e.key === 'ArrowLeft' ? -0.05 : 0.05), 0.02, 0.98);
      applySplit();
    }
  });
  const onResize = () => { applySplit(); mapA?.resize(); mapB?.resize(); };
  window.addEventListener('resize', onResize);
  cleanups.push(onUp, () => window.removeEventListener('resize', onResize));
  setMode(mode);
  return { close: m.close, maps: () => [mapA, mapB], dates: () => [inBefore.value, inAfter.value], source: () => source, mode: () => mode,
    setSource: (v) => { srcSel.value = v; srcSel.dispatchEvent(new Event('change')); } };
}

// ------------------------------------------------------------------ register
export async function registerImagery(ctx) {
  I.ctx = ctx;
  registerDaily(ctx);     // availability (I.cat) is fetched when the layer or the compare tool needs it
  let meta = null;
  try { meta = await gibsMeta(ctx.api); } catch { meta = null; }
  ctx._spaceGibsBase = ctx._spaceGibsBase || meta?.base || FALLBACK_BASE;
  for (const pid of PRODUCT_LAYERS) {
    try {
      registerProductLayer(ctx, pid, meta?.products?.[pid] || PRODUCT_FALLBACK[pid], { reference: (on, who) => setReference(on, who) });
    } catch (err) { console.error(`space: layer ${pid} failed`, err); }
  }
  const open = (p) => openCompare(p || {}).catch((err) => { console.error('space: compare failed', err); ctx.ui.toast('The imagery compare tool could not open.', 'red'); });
  ctx.ui.toolbarButton({ id: 'space-compare', title: 'Compare imagery', label: 'Compare',
    icon: 'M3 4.5h14v11H3zM10 4.5v11M6 8l-1.5 2L6 12M14 8l1.5 2L14 12', onClick: () => open({}), order: 40 });
  ctx.bus.on('imagery:compare', (p) => open(p));
}
