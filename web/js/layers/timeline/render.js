// Replay layers on the map: aircraft and vessels at the replay time (interpolated between their
// samples) with short fading trails, and events fading in and out around it. Sources and layers are
// ov-timeline-*, icons timeline-*. They exist only while replaying (add on enter, remove on exit).

import { AIR, SEA, positionAt, trail, eventAlpha, severityLevel } from './interp.js';

export const GROUP_AIR = '#e05aa8';
export const GROUP_SEA = '#4fa8e8';
export const INK = '#111a2c';

// Class colours as the live layers draw them (flights/style.js and ships/common.js, copied so the
// timeline never depends on those display files).
export const AIR_CLASSES = {
  airliner: ['#e05aa8', 'Airliner'], cargo: ['#eba15e', 'Cargo'], business: ['#b9a5ff', 'Business jet'],
  military: ['#a8c467', 'Military'], helicopter: ['#5fcfc6', 'Helicopter'], light: ['#eed36c', 'Light aircraft'],
  glider: ['#91b9ee', 'Glider, balloon or drone'], ground: ['#8b97b0', 'Ground vehicle'], unknown: ['#d6d0c0', 'Unknown'],
};
export const SEA_CLASSES = {
  cargo: ['#3987e5', 'Cargo'], tanker: ['#d95926', 'Tanker'], passenger: ['#199e70', 'Passenger'],
  fishing: ['#c98500', 'Fishing'], service: ['#9085e9', 'Service craft'], pleasure: ['#d55181', 'Sailing or pleasure'],
  highspeed: ['#0ca30c', 'High-speed craft'], military: ['#ebe6d6', 'Military'], other: ['#8b97b0', 'Other or unknown'],
};
export const SEVERITY = {
  routine: { color: '#8b97b0', label: 'routine' },
  notable: { color: '#ffb547', label: 'notable' },
  serious: { color: '#ff5d52', label: 'serious' },
  extreme: { color: '#ff5d52', label: 'extreme' },
};
export const FLAG = { MIL: 2, EMG: 4, SANC: 8, GND: 16 };
const RED = '#ff5d52';

export const MAX_AIR = 4000;
export const MAX_SEA = 4000;
const MAX_AIR_TRAILS = 1500;
const MAX_SEA_TRAILS = 1200;

const SRC = {
  air: 'ov-timeline-air', airTrail: 'ov-timeline-air-trail', sea: 'ov-timeline-sea', seaTrail: 'ov-timeline-sea-trail',
  events: 'ov-timeline-events', track: 'ov-timeline-track',
};
export const CLICKABLE = ['ov-timeline-air-icon', 'ov-timeline-sea-icon', 'ov-timeline-sea-dot', 'ov-timeline-events-dot'];

export function airColor(cls, flags) {
  if (flags & FLAG.EMG) return RED;
  return (AIR_CLASSES[cls] || AIR_CLASSES.unknown)[0];
}
export function seaColor(cls) {
  return (SEA_CLASSES[cls] || SEA_CLASSES.other)[0];
}

function registerIcons(map2) {
  // Aircraft: a plain swept-wing silhouette, nose up (north), white for SDF tinting.
  map2.icon('timeline-plane', (g, s) => {
    const k = s / 32;
    g.fillStyle = '#fff';
    g.beginPath();
    g.moveTo(16 * k, 2 * k);
    g.lineTo(18 * k, 5 * k); g.lineTo(18.2 * k, 12 * k); g.lineTo(29 * k, 19 * k); g.lineTo(29 * k, 21.5 * k);
    g.lineTo(18.2 * k, 17.5 * k); g.lineTo(17.6 * k, 25 * k); g.lineTo(21.5 * k, 28 * k); g.lineTo(21.5 * k, 30 * k);
    g.lineTo(16 * k, 28.6 * k);
    g.lineTo(10.5 * k, 30 * k); g.lineTo(10.5 * k, 28 * k); g.lineTo(14.4 * k, 25 * k); g.lineTo(13.8 * k, 17.5 * k);
    g.lineTo(3 * k, 21.5 * k); g.lineTo(3 * k, 19 * k); g.lineTo(13.8 * k, 12 * k); g.lineTo(14 * k, 5 * k);
    g.closePath();
    g.fill();
  }, { size: 32, sdf: true });
  // Vessel under way: a pointed hull, bow up.
  map2.icon('timeline-ship', (g, s) => {
    const k = s / 32;
    g.fillStyle = '#fff';
    g.beginPath();
    g.moveTo(16 * k, 3 * k); g.lineTo(22 * k, 12 * k); g.lineTo(22 * k, 28 * k); g.lineTo(10 * k, 28 * k); g.lineTo(10 * k, 12 * k);
    g.closePath();
    g.fill();
  }, { size: 32, sdf: true });
}

export function createRender(ctx, data) {
  const { map2 } = ctx;
  let on = false;
  let iconsReady = false;
  let lastEventsSig = '';
  const visible = { air: new Map(), sea: new Map() };   // id -> position (for cards and hover)
  let counts = { air: 0, sea: 0, events: 0, airCapped: false, seaCapped: false };
  let showTrails = true;
  let frameMs = 0;

  function add() {
    if (on) return;
    on = true;
    if (!iconsReady) { registerIcons(map2); iconsReady = true; }
    const empty = map2.emptyFC();
    map2.addSource(SRC.track, { type: 'geojson', data: empty });
    map2.addSource(SRC.seaTrail, { type: 'geojson', data: empty, lineMetrics: true });
    map2.addSource(SRC.airTrail, { type: 'geojson', data: empty, lineMetrics: true });
    map2.addSource(SRC.events, { type: 'geojson', data: empty });
    map2.addSource(SRC.sea, { type: 'geojson', data: empty });
    map2.addSource(SRC.air, { type: 'geojson', data: empty });
    map2.addLayer({ id: 'ov-timeline-track-line', type: 'line', source: SRC.track,
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': ['get', 'color'], 'line-width': 1.6, 'line-opacity': 0.7 } }, 'lines');
    map2.addLayer({ id: 'ov-timeline-sea-trail-line', type: 'line', source: SRC.seaTrail,
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-width': ['interpolate', ['linear'], ['zoom'], 3, 1, 9, 2],
        'line-gradient': ['interpolate', ['linear'], ['line-progress'], 0, 'rgba(79,168,232,0)', 1, 'rgba(79,168,232,0.8)'] } }, 'lines');
    map2.addLayer({ id: 'ov-timeline-air-trail-line', type: 'line', source: SRC.airTrail,
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-width': ['interpolate', ['linear'], ['zoom'], 3, 1, 9, 2],
        'line-gradient': ['interpolate', ['linear'], ['line-progress'], 0, 'rgba(224,90,168,0)', 1, 'rgba(224,90,168,0.85)'] } }, 'lines');
    map2.addLayer({ id: 'ov-timeline-events-dot', type: 'circle', source: SRC.events,
      paint: { 'circle-radius': ['get', 'r'], 'circle-color': ['get', 'color'], 'circle-opacity': ['*', 0.32, ['get', 'a']],
        'circle-stroke-color': ['get', 'color'], 'circle-stroke-width': 1.6, 'circle-stroke-opacity': ['get', 'a'],
        'circle-pitch-alignment': 'map' } }, 'points');
    map2.addLayer({ id: 'ov-timeline-sea-dot', type: 'circle', source: SRC.sea, filter: ['==', ['get', 'mv'], 0],
      paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 3, 1.6, 8, 3, 12, 4.5], 'circle-color': ['get', 'color'],
        'circle-stroke-color': INK, 'circle-stroke-width': 0.8, 'circle-opacity': ['case', ['==', ['get', 'st'], 1], 0.4, 0.9],
        'circle-stroke-opacity': ['case', ['==', ['get', 'st'], 1], 0.4, 0.9] } }, 'moving');
    map2.addLayer({ id: 'ov-timeline-sea-icon', type: 'symbol', source: SRC.sea, filter: ['==', ['get', 'mv'], 1],
      layout: { 'icon-image': 'timeline-ship', 'icon-rotate': ['get', 'dir'], 'icon-rotation-alignment': 'map',
        'icon-allow-overlap': true, 'icon-ignore-placement': true,
        'icon-size': ['interpolate', ['linear'], ['zoom'], 3, 0.28, 8, 0.45, 12, 0.6] },
      paint: { 'icon-color': ['get', 'color'], 'icon-halo-color': INK, 'icon-halo-width': 1.2,
        'icon-opacity': ['case', ['==', ['get', 'st'], 1], 0.45, 1] } }, 'moving');
    map2.addLayer({ id: 'ov-timeline-air-ring', type: 'circle', source: SRC.air, filter: ['==', ['get', 'sanc'], 1],
      paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 3, 7, 10, 12], 'circle-color': 'rgba(0,0,0,0)',
        'circle-stroke-color': RED, 'circle-stroke-width': 1.6, 'circle-pitch-alignment': 'map' } }, 'moving');
    map2.addLayer({ id: 'ov-timeline-air-icon', type: 'symbol', source: SRC.air,
      layout: { 'icon-image': 'timeline-plane', 'icon-rotate': ['get', 'dir'], 'icon-rotation-alignment': 'map',
        'icon-allow-overlap': true, 'icon-ignore-placement': true, 'symbol-sort-key': ['get', 'z'],
        'icon-size': ['interpolate', ['linear'], ['zoom'], 3, ['*', 0.34, ['get', 'sz']], 8, ['*', 0.52, ['get', 'sz']], 12, ['*', 0.66, ['get', 'sz']]] },
      paint: { 'icon-color': ['get', 'color'], 'icon-halo-color': INK, 'icon-halo-width': 1.3,
        'icon-opacity': ['case', ['==', ['get', 'st'], 1], 0.45, 1] } }, 'moving');
    map2.addLayer({ id: 'ov-timeline-air-label', type: 'symbol', source: SRC.air, minzoom: 7,
      layout: { 'text-field': ['get', 'label'], 'text-font': ['Open Sans Regular'], 'text-size': 11,
        'text-offset': [0, 1.35], 'text-anchor': 'top', 'text-optional': true },
      paint: { 'text-color': '#ebe6d6', 'text-halo-color': INK, 'text-halo-width': 1.4,
        'text-opacity': ['case', ['==', ['get', 'st'], 1], 0.5, 0.9] } }, 'top');
    lastEventsSig = '';
  }

  function remove() {
    if (!on) return;
    on = false;
    for (const id of Object.values(SRC)) map2.removeSource(id);
    visible.air.clear();
    visible.sea.clear();
    counts = { air: 0, sea: 0, events: 0, airCapped: false, seaCapped: false };
  }

  function buildAir(t, zoom) {
    const k = data.kinds.air;
    const feats = [];
    const priority = [];
    visible.air.clear();
    if (!data.enabled.air) return { feats, trails: [] };
    for (const tr of k.tracks.values()) {
      const pos = positionAt(tr, t, AIR);
      if (!pos) continue;
      const flags = pos.flags || 0;
      const item = { tr, pos, flags, rank: (flags & FLAG.EMG ? 4 : 0) + (flags & FLAG.SANC ? 3 : 0) + (flags & FLAG.MIL ? 1 : 0) };
      if (item.rank) priority.push(item); else feats.push(item);
    }
    let items = priority.concat(feats);
    counts.airCapped = items.length > MAX_AIR;
    if (counts.airCapped) items = items.slice(0, MAX_AIR);
    const out = [];
    const trails = [];
    for (const { tr, pos, flags, rank } of items) {
      visible.air.set(tr.id, pos);
      const ident = k.idents.get(tr.id);
      const emg = flags & FLAG.EMG ? 1 : 0;
      out.push({ type: 'Feature', id: parseInt(tr.id, 16), geometry: { type: 'Point', coordinates: [pos.lon, pos.lat] },
        properties: { id: tr.id, color: airColor(tr.cls, flags), dir: pos.dir == null ? 0 : Math.round(pos.dir),
          sz: emg ? 1.3 : (tr.cls === 'helicopter' || tr.cls === 'light' || tr.cls === 'glider' ? 0.8 : 1),
          st: pos.stale ? 1 : 0, sanc: flags & FLAG.SANC ? 1 : 0, z: rank,
          label: (ident && (ident.cs || ident.reg)) || tr.id.toUpperCase() } });
      if (showTrails && zoom >= 3 && trails.length < MAX_AIR_TRAILS && pos.state !== 'held') {
        const line = trail(tr, t, AIR.trail, pos);
        if (line) trails.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: line }, properties: {} });
      }
    }
    counts.air = out.length;
    return { feats: out, trails };
  }

  function buildSea(t, zoom) {
    const k = data.kinds.sea;
    const out = [];
    const trails = [];
    visible.sea.clear();
    if (!data.enabled.sea) return { feats: out, trails };
    let n = 0;
    for (const tr of k.tracks.values()) {
      const pos = positionAt(tr, t, SEA);
      if (!pos) continue;
      n += 1;
      if (out.length >= MAX_SEA) continue;
      visible.sea.set(tr.id, pos);
      const moving = pos.a != null && pos.a >= 1 && pos.dir != null;
      out.push({ type: 'Feature', id: +tr.id, geometry: { type: 'Point', coordinates: [pos.lon, pos.lat] },
        properties: { id: tr.id, color: seaColor(tr.cls), dir: moving ? Math.round(pos.dir) : 0, mv: moving ? 1 : 0,
          st: pos.stale ? 1 : 0 } });
      if (showTrails && moving && zoom >= 4 && trails.length < MAX_SEA_TRAILS) {
        const line = trail(tr, t, SEA.trail, pos);
        if (line) trails.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: line }, properties: {} });
      }
    }
    counts.sea = out.length;
    counts.seaCapped = n > MAX_SEA;
    return { feats: out, trails };
  }

  function buildEvents(t) {
    const out = [];
    let sig = '';
    if (data.enabled.events) {
      for (const ev of data.events.list) {
        const a = Math.round(eventAlpha(ev, t) * 10) / 10;
        if (a <= 0) continue;
        const level = severityLevel(ev.sev);
        out.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [ev.lon, ev.lat] },
          properties: { key: ev.key, color: SEVERITY[level].color, a, r: 3 + Math.min(10, ev.sev) * 0.6 } });
        sig += `${ev.key}${a};`;
      }
    }
    counts.events = out.length;
    return { feats: out, sig };
  }

  /** Draw the replay at time t (epoch s); `sea: false` leaves the vessels as they are (they move a
   *  pixel or less between frames, and thousands of symbols are costly to lay out). Returns ms. */
  function frame(t, zoom, { sea: withSea = true } = {}) {
    if (!on) return 0;
    const started = performance.now();
    const air = buildAir(t, zoom);
    map2.setData(SRC.air, { type: 'FeatureCollection', features: air.feats });
    map2.setData(SRC.airTrail, { type: 'FeatureCollection', features: air.trails });
    if (withSea) {
      const sea = buildSea(t, zoom);
      map2.setData(SRC.sea, { type: 'FeatureCollection', features: sea.feats });
      map2.setData(SRC.seaTrail, { type: 'FeatureCollection', features: sea.trails });
    }
    const ev = buildEvents(t);
    if (ev.sig !== lastEventsSig) {
      lastEventsSig = ev.sig;
      map2.setData(SRC.events, { type: 'FeatureCollection', features: ev.feats });
    }
    frameMs = performance.now() - started;
    return frameMs;
  }

  function showTrack(points, color) {
    if (!on) return;
    const coords = points.map(p => [p.lon, p.lat]);
    for (let i = 1; i < coords.length; i++) {
      const d = coords[i][0] - coords[i - 1][0];
      if (d > 180) coords[i][0] -= 360 * Math.round(d / 360);
      else if (d < -180) coords[i][0] += 360 * Math.round(-d / 360);
    }
    map2.setData(SRC.track, coords.length >= 2
      ? { type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'LineString', coordinates: coords }, properties: { color } }] }
      : map2.emptyFC());
  }

  function clearTrack() {
    if (on) map2.setData(SRC.track, map2.emptyFC());
  }

  return {
    add, remove, frame, showTrack, clearTrack, visible,
    isOn: () => on,
    counts: () => ({ ...counts, frameMs: Math.round(frameMs * 10) / 10 }),
    setTrails(v) { showTrails = !!v; },
  };
}
