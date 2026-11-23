// "Export what's on screen": the features a layer has drawn inside the current view, read back from
// MapLibre (queryRenderedFeatures on its ov- layers), matched to their source features for exact
// positions and full shapes, and written as GeoJSON, CSV or KML in the browser. Nothing is uploaded.
// A point drawn from two sources (a camera and its street-zoom name) is written once.
//
// What is never exported: Windy webcams (their terms allow API links only), aircraft in the LADD or
// PIA privacy programmes (checked against the flights module; when that check can't be made, no
// aircraft at all), owner fields, and the trails and selections cards draw.

import { FORMATS, MAX_BYTES, MAX_FEATURES, PRIVACY_FLAG, RASTER_TYPES, aircraftRows, crossSourceDuplicates, exportFilename,
  indexFeatures, matchFeature, restrict, signature, styleLayersFor } from './logic.js';

const NOTE = 'Only what was drawn on the map inside the view at the time of export: not the whole dataset.';

function withTimeout(promise, ms) {
  return Promise.race([promise, new Promise((_, reject) => setTimeout(() => reject(new Error('timed out')), ms))]);
}

function registered(ctx) { return ctx.layers.list().map(l => l.id); }

/** The map layers that draw this layer now: {ids, vector, shared}. */
export function mapLayersOf(ctx, layerId) {
  const map = ctx.map;
  const styleIds = (map.getStyle()?.layers || []).map(l => l.id);
  const pick = styleLayersFor(layerId, styleIds, registered(ctx));
  const ids = pick.ids.filter(id => map.getLayer(id));
  const vector = ids.filter(id => !RASTER_TYPES.has(map.getLayer(id).type));
  return { ids, vector, shared: pick.shared };
}

/** A quick count of what a layer draws in view (clusters count their points, a thing drawn by two sources once). */
export function quickCount(ctx, layerId) {
  const { ids, vector } = mapLayersOf(ctx, layerId);
  if (!ids.length) return { count: 0, raster: false };
  if (!vector.length) return { count: 0, raster: true };
  let feats = [];
  try { feats = ctx.map.queryRenderedFeatures({ layers: vector }); } catch { return { count: 0, raster: false }; }
  const seen = new Set();
  const duplicate = crossSourceDuplicates();
  let n = 0;
  for (const f of feats) {
    const p = f.properties || {};
    if (p.cluster) {
      const key = `${f.source}|c${p.cluster_id}`;
      if (!seen.has(key)) { seen.add(key); n += +p.point_count || 0; }
      continue;
    }
    const key = `${f.source}|${signature(p)}`;
    if (!seen.has(key)) { seen.add(key); if (!duplicate(f, f.source)) n += 1; }
  }
  return { count: n, raster: false };
}

/** The sources behind these map layers, in style order (the lowest map layer that draws each first). */
function sourceOrder(map, layerIds) {
  const out = [];
  for (const id of layerIds) {
    const source = map.getLayer(id)?.source;
    if (source && !out.includes(source)) out.push(source);
  }
  return out;
}

async function sourceIndex(src) {
  if (!src || src.type !== 'geojson' || typeof src.getData !== 'function') return null;
  try { return indexFeatures(await withTimeout(src.getData(), 4000)); } catch { return null; }
}

/** Collect the features a layer draws in view. Returns {features, capped, raster, shared, sources}. */
export async function collect(ctx, layerId, { max = MAX_FEATURES } = {}) {
  const map = ctx.map;
  const { ids, vector, shared } = mapLayersOf(ctx, layerId);
  if (!ids.length) return { features: [], capped: false, raster: false, shared, empty: true };
  if (!vector.length) return { features: [], capped: false, raster: true, shared };
  const drawn = map.queryRenderedFeatures({ layers: vector });
  const bySource = new Map();
  for (const f of drawn) {
    if (!bySource.has(f.source)) bySource.set(f.source, []);
    bySource.get(f.source).push(f);
  }
  // Read the sources in style order, so a layer's own points come before a separate source of names
  // drawn above them (cameras): the points are kept, the names that repeat them are left out.
  const ranked = sourceOrder(map, vector);
  const rank = (id) => (ranked.includes(id) ? ranked.indexOf(id) : ranked.length);
  const sources = [...bySource.keys()].sort((a, b) => rank(a) - rank(b));
  const out = [];
  const taken = new Set();
  const duplicate = crossSourceDuplicates();
  let capped = false;
  const add = (feature, key, sourceId) => {
    if (taken.has(key)) return;
    if (duplicate(feature, sourceId)) { taken.add(key); return; }
    if (out.length >= max) { capped = true; return; }
    taken.add(key);
    out.push(feature);
  };
  for (const sourceId of sources) {
    const feats = bySource.get(sourceId);
    const src = map.getSource(sourceId);
    const index = await sourceIndex(src);
    const clusters = new Set();
    for (const f of feats) {
      if (capped) break;
      const p = f.properties || {};
      if (p.cluster && src && typeof src.getClusterLeaves === 'function') {
        if (clusters.has(p.cluster_id)) continue;
        clusters.add(p.cluster_id);
        let leaves = [];
        try { leaves = await withTimeout(Promise.resolve(src.getClusterLeaves(p.cluster_id, Math.max(1, max - out.length + 1), 0)), 8000); } catch { leaves = []; }
        for (const leaf of leaves || []) add({ type: 'Feature', geometry: leaf.geometry, properties: { ...(leaf.properties || {}) } },
          `${sourceId}|${signature(leaf.properties)}|${JSON.stringify(leaf.geometry?.coordinates)}`, sourceId);
        continue;
      }
      const json = typeof f.toJSON === 'function' ? f.toJSON() : { geometry: f.geometry, properties: p };
      const original = index ? matchFeature(index, json) : null;
      if (original) {
        add({ type: 'Feature', geometry: original.geometry, properties: { ...(original.properties || {}) } },
          `${sourceId}|${signature(original.properties)}|${JSON.stringify(original.geometry?.coordinates?.slice?.(0, 2))}`, sourceId);
      } else {
        add({ type: 'Feature', geometry: json.geometry, properties: { ...(json.properties || {}) } },
          `${sourceId}|${signature(json.properties)}|${JSON.stringify(json.geometry)}`, sourceId);
      }
    }
  }
  return { features: out, capped, raster: false, shared, sources };
}

/** Aircraft the flights module shows and may be exported: {allowed: Set of hex, rows}, or null when unknown. */
async function aircraftCheck(ctx) {
  if (!ctx.moduleOk('flights')) return null;
  const bounds = ctx.map2.viewBounds();
  const asks = [ctx.api.get('/api/flights/live', { bbox: bounds.join(','), zoom: ctx.map.getZoom().toFixed(1) }),
    ctx.api.get('/api/flights/military'), ctx.api.get('/api/flights/alerts')];
  const answers = await Promise.allSettled(asks);
  if (answers.every(a => a.status !== 'fulfilled')) return null;
  const rows = aircraftRows(answers.filter(a => a.status === 'fulfilled').map(a => a.value));
  const allowed = new Set([...rows.values()].filter(r => !(r.flags & PRIVACY_FLAG)).map(r => r.hex));
  return { allowed, rows };
}

/** Camera names for up to 600 cameras in view (the map file carries ids only). */
async function cameraNames(ctx) {
  if (!ctx.moduleOk('cameras')) return new Map();
  try {
    const r = await ctx.api.get('/api/cameras/labels', { bbox: ctx.map2.viewBounds().join(','), limit: 600 });
    return new Map((r.features || []).map(f => [f.properties?.id, f.properties]));
  } catch { return new Map(); }
}

/** Credits for the file: the layer's source text and its declared sources' licences. */
async function credits(ctx, layerId, spec) {
  const out = { attribution: spec?.source || '', licence: '' };
  try {
    const status = await ctx.api.get('/api/core/status');
    const rows = (status.sources || []).filter(s => s.layer === layerId && s.licence);
    if (rows.length) {
      out.licence = [...new Set(rows.map(s => `${s.name}: ${s.licence}`))].join('; ').slice(0, 2000);
    }
  } catch { /* the layer's own source text is enough */ }
  if (!out.licence) out.licence = 'Each source\'s own terms apply; see the attribution and Sources and status in Meridian.';
  return out;
}

/** Build the file for one layer in one format and hand it to the browser. Returns what happened. */
export async function exportLayer(ctx, layerId, format) {
  const fmt = FORMATS[format];
  if (!fmt) throw new Error('Choose GeoJSON, CSV or KML.');
  const entry = ctx.layers.get(layerId);
  if (!entry || !entry.on) throw new Error('Turn the layer on first.');
  const spec = entry.spec || {};
  const got = await collect(ctx, layerId);
  if (got.raster) return { ok: false, message: `${spec.title || layerId} is a picture (map tiles), so it has no features to export.` };
  const isFlights = layerId === 'flights' || layerId.startsWith('flights.');
  const aircraft = isFlights && got.features.some(f => typeof f.properties?.hex === 'string') ? await aircraftCheck(ctx) : undefined;
  const restricted = restrict(got.features, aircraft === undefined ? {} : { aircraft });
  const features = restricted.features;
  if (aircraft) {
    const rows = aircraft.rows;
    for (const f of features) {
      const r = rows.get(f.properties.hex);
      if (!r) continue;
      Object.assign(f.properties, { callsign: r.cs || '', registration: r.reg || '', type: r.typ || '',
        alt_ft: Number.isFinite(r.alt) ? r.alt : '', speed_kn: Number.isFinite(r.gs) ? r.gs : '', track_deg: Number.isFinite(r.trk) ? r.trk : '' });
    }
  }
  if (layerId === 'cameras' && features.length) {
    const names = await cameraNames(ctx);
    for (const f of features) {
      const n = names.get(f.properties?.id);
      if (n) { f.properties.name = n.n || ''; if (n.a) f.properties.area = n.a; }
    }
  }
  if (!features.length) {
    const why = restricted.dropped.windy || restricted.dropped.privacy ? ' (what is drawn here may not be exported)' : '';
    return { ok: false, message: `Nothing from ${spec.title || layerId} is drawn in this view${why}. Move or zoom the map, then try again.` };
  }
  const meta = { layer: layerId, title: `${spec.title || layerId}, on screen`, color: spec.color, note: NOTE,
    exportedAt: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'), ...(await credits(ctx, layerId, spec)) };
  meta.source = meta.attribution;
  const text = fmt.write(features, meta);
  if (text.length > MAX_BYTES) {
    return { ok: false, message: `That would be a ${(text.length / 1048576).toFixed(0)} MB file, over the 25 MB limit. Zoom in to export a smaller area.` };
  }
  const name = exportFilename(layerId, format);
  ctx.dom.download(name, text, fmt.type);
  return { ok: true, name, count: features.length, capped: got.capped, shared: got.shared, dropped: restricted.dropped };
}
