// One map source for every aircraft the module draws. Three layers feed it (the view, military
// worldwide, emergencies worldwide) so an aircraft is drawn once even when two layers include it.

import { CLASSES, FLAGS, INK, RED, altText, classColor, rowEmergencyShort, rowEmergencyText, titleOf } from './style.js';

const SRC = 'ov-flights-ac';
export const L_EMG = 'ov-flights-emg';
export const L_SANC = 'ov-flights-sanc';
export const L_ICON = 'ov-flights-icon';
export const L_LABEL = 'ov-flights-label';

let ctx = null;

// Icon scale per zoom stop. The SDF halo must stay below 6 x icon-size (MapLibre's SDF range), so
// halo widths follow the same curve: about 1.8 x size for the ink outline, 2.3 x for hollow icons.
const ZOOM_SCALE = [[1, 0.32], [4, 0.38], [7, 0.5], [10, 0.66], [13, 0.82], [16, 0.94]];
const bySize = (k) => ['interpolate', ['linear'], ['zoom'],
  ...ZOOM_SCALE.flatMap(([z, s]) => [z, ['*', s, k, ['get', 'sz']]])];
const EST = ['==', ['get', 'est'], 1];
const sets = { view: new Map(), mil: new Map(), alerts: new Map() };
const active = { view: false, mil: false, alerts: false };
const drawn = { view: 0, mil: 0, alerts: 0 };
let opts = { military: false, hideGround: false, vehicles: false, minAlt: 0, kind: 'all', labels: true, estimated: true };
let lastFeatures = [];

export function init(c) { ctx = c; }

export function setOptions(o) {
  opts = { ...opts, ...o };
  render();
}

export function isActive(which) { return active[which]; }

function ensureLayers() {
  if (ctx.map.getSource(SRC)) return;
  ctx.map2.addSource(SRC, { type: 'geojson', data: ctx.map2.emptyFC() });
  ctx.map2.addLayer({
    id: L_EMG, type: 'circle', source: SRC, filter: ['==', ['get', 'emg'], 1],
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 1, 7, 5, 10, 9, 15, 13, 20],
      'circle-color': 'rgba(255,93,82,0.20)', 'circle-stroke-color': RED, 'circle-stroke-width': 1.6,
      'circle-pitch-alignment': 'map',
    },
  }, 'moving');
  ctx.map2.addLayer({
    id: L_SANC, type: 'circle', source: SRC, filter: ['==', ['get', 'sanc'], 1],
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 1, 6, 5, 9, 9, 13, 13, 17],
      'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': RED, 'circle-stroke-width': 2,
      'circle-pitch-alignment': 'map',
    },
  }, 'moving');
  ctx.map2.addLayer({
    id: L_ICON, type: 'symbol', source: SRC,
    layout: {
      'icon-image': ['get', 'icon'],
      'icon-size': bySize(1),
      'icon-rotate': ['get', 'rot'],
      'icon-rotation-alignment': 'map',
      'icon-pitch-alignment': 'map',
      'icon-allow-overlap': true,
      'icon-ignore-placement': true,
      'symbol-sort-key': ['get', 'sk'],
    },
    paint: {
      'icon-color': ['case', EST, 'rgba(0,0,0,0)', ['get', 'col']],
      'icon-halo-color': ['case', EST, ['get', 'col'], 'rgba(17,26,44,0.92)'],
      'icon-halo-width': bySize(['case', EST, 4.6, 3.6]),
      'icon-opacity': ['case', EST, 0.95, 1],
    },
  }, 'moving');
  ctx.map2.addLayer({
    id: L_LABEL, type: 'symbol', source: SRC, minzoom: 6.5,
    layout: {
      'text-field': ['step', ['zoom'], ['get', 'lbl'], 9, ['get', 'lbl2']],
      'text-font': ['Open Sans Regular'],
      'text-size': ['interpolate', ['linear'], ['zoom'], 6.5, 10.5, 12, 12],
      'text-variable-anchor': ['left', 'right', 'top', 'bottom'],
      'text-radial-offset': ['interpolate', ['linear'], ['zoom'], 6.5, 0.9, 12, 1.4],
      'text-justify': 'auto',
      'text-padding': 3,
      'text-max-width': 14,
      'text-line-height': 1.1,
      'symbol-sort-key': ['-', 0, ['get', 'sk']],
      'visibility': opts.labels ? 'visible' : 'none',
    },
    paint: {
      'text-color': ['case', ['==', ['get', 'emg'], 1], '#ffd6d2', ['==', ['get', 'est'], 1], '#aab4ca', '#ebe6d6'],
      'text-halo-color': INK, 'text-halo-width': 1.4,
    },
  }, 'moving');
}

function removeIfUnused() {
  if (!active.view && !active.mil && !active.alerts) ctx.map2.removeSource(SRC);
}

export function setActive(which, on) {
  active[which] = on;
  if (on) ensureLayers();
  else sets[which].clear();
  render();
  if (!on) removeIfUnused();
}

/** Decode a compact answer ({fields, ac, now}) into row objects keyed by hex. */
export function setRows(which, payload) {
  const f = payload.fields || [];
  const ix = Object.fromEntries(f.map((name, i) => [name, i]));
  const map = new Map();
  const now = payload.now || Date.now() / 1000;
  for (const r of payload.ac || []) {
    const row = {
      hex: r[ix.hex], lat: r[ix.lat], lon: r[ix.lon], trk: r[ix.trk], alt: r[ix.alt], gs: r[ix.gs], vr: r[ix.vr],
      cs: r[ix.cs], reg: r[ix.reg], typ: r[ix.typ], cls: r[ix.cls], shp: r[ix.shp], sz: r[ix.sz], flags: r[ix.flags] | 0,
      age: r[ix.age], sq: r[ix.sq], t: now - (r[ix.age] || 0), from: which,
    };
    if (typeof row.hex !== 'string' || !Number.isFinite(row.lat) || !Number.isFinite(row.lon)) continue;
    map.set(row.hex, row);
  }
  sets[which] = map;
  render();
  return map;
}

export function count(which) { return drawn[which]; }
export function rows(which) { return sets[which]; }

export function find(hex) {
  return sets.alerts.get(hex) || sets.view.get(hex) || sets.mil.get(hex) || null;
}

function passesView(row) {
  const fl = row.flags;
  if (opts.military && !(fl & FLAGS.MIL)) return false;
  if (opts.hideGround && (fl & FLAGS.GND)) return false;
  if (!opts.vehicles && row.cls === 'ground' && opts.kind !== 'ground') return false;   // airport service vehicles
  if (!opts.estimated && (fl & FLAGS.EST)) return false;
  if (opts.minAlt > 0 && ((fl & FLAGS.GND) || row.alt == null || row.alt < opts.minAlt)) return false;
  const k = opts.kind;
  if (k && k !== 'all') {
    if (k === 'emergency') return !!(fl & FLAGS.EMG);
    if (k === 'sanctioned') return !!(fl & FLAGS.SANC);
    if (CLASSES[k]) return row.cls === k;
  }
  return true;
}

function feature(row) {
  const fl = row.flags;
  const est = fl & FLAGS.EST ? 1 : 0;
  const emg = fl & FLAGS.EMG ? 1 : 0;
  const sanc = fl & FLAGS.SANC ? 1 : 0;
  const gnd = fl & FLAGS.GND ? 1 : 0;
  const title = titleOf(row);
  const alt = altText(ctx.fmt, row.alt, gnd);
  const lbl = est ? `${title}\nestimated` : title;
  let lbl2 = lbl;
  if (!est) lbl2 = emg ? `${title}\n${rowEmergencyShort(row)}` : (alt ? `${title}\n${gnd ? 'ground' : alt}` : title);
  return {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [row.lon, row.lat] },
    properties: {
      hex: row.hex, lbl, lbl2, est, emg, sanc, gnd, cls: row.cls,
      col: emg ? RED : classColor(row.cls),
      icon: `flights-${row.shp || 'generic'}`,
      sz: row.sz || 0.74,
      rot: row.trk == null ? 0 : row.trk,
      sk: (emg ? 200000 : 0) + (sanc ? 150000 : 0) + (gnd ? 0 : 1000 + Math.max(0, Math.min(60000, row.alt || 0))),
    },
  };
}

export function render() {
  if (!ctx || !ctx.map.getSource(SRC)) { drawn.view = drawn.mil = drawn.alerts = 0; return; }
  const chosen = new Map();
  const take = (row, which) => {
    const prev = chosen.get(row.hex);
    if (!prev || row.t > prev.t + 1) chosen.set(row.hex, row);
    drawn[which] += 1;
  };
  drawn.view = drawn.mil = drawn.alerts = 0;
  if (active.alerts) for (const r of sets.alerts.values()) take(r, 'alerts');
  if (active.view) for (const r of sets.view.values()) if (passesView(r)) take(r, 'view');
  if (active.mil) for (const r of sets.mil.values()) take(r, 'mil');
  const feats = [];
  for (const r of chosen.values()) feats.push(feature(r));
  lastFeatures = feats;
  ctx.map2.setData(SRC, { type: 'FeatureCollection', features: feats });
  if (ctx.map.getLayer(L_LABEL)) ctx.map.setLayoutProperty(L_LABEL, 'visibility', opts.labels ? 'visible' : 'none');
}

export function hoverText(f) {
  const p = f.properties || {};
  const row = find(p.hex);
  if (!row) return null;
  const parts = [titleOf(row)];
  const alt = altText(ctx.fmt, row.alt, row.flags & FLAGS.GND);
  if (alt) parts.push(alt);
  if (row.flags & FLAGS.EMG) parts.push(rowEmergencyText(row));
  if (row.flags & FLAGS.SANC) parts.push('OFAC-listed');
  if (row.flags & FLAGS.EST) parts.push(`estimated position, last report ${ctx.fmt.ago(row.t)}`);
  return parts.join(', ');
}

/** Browser search over loaded aircraft (callsign, registration, hex). */
export function search(q) {
  const text = String(q || '').trim().toUpperCase();
  if (text.length < 2) return [];
  const compact = text.replace(/[^A-Z0-9]/g, '');
  const out = [];
  const seen = new Set();
  for (const set of [sets.alerts, sets.view, sets.mil]) {
    for (const r of set.values()) {
      if (seen.has(r.hex)) continue;
      let score = 0;
      const reg = (r.reg || '').replace(/[^A-Z0-9]/g, '');
      if (r.hex.toUpperCase() === compact) score = 0.96;
      else if (r.cs && r.cs === compact) score = 0.92;
      else if (reg && reg === compact) score = 0.92;
      else if (compact.length >= 3 && r.cs && r.cs.startsWith(compact)) score = 0.75;
      else if (compact.length >= 3 && reg && reg.startsWith(compact)) score = 0.65;
      if (!score) continue;
      seen.add(r.hex);
      const alt = altText(ctx.fmt, r.alt, r.flags & FLAGS.GND);
      out.push({ kind: 'aircraft', id: r.hex, title: titleOf(r), subtitle: [r.typ, r.reg, alt].filter(Boolean).join(', '),
        lat: r.lat, lon: r.lon, layer: r.from === 'mil' ? 'flights.military' : r.from === 'alerts' ? 'flights.alerts' : 'flights',
        score, zoom: 9 });
      if (out.length >= 12) return out.sort((a, b) => b.score - a.score);
    }
  }
  return out.sort((a, b) => b.score - a.score);
}

export function featureCount() { return lastFeatures.length; }
