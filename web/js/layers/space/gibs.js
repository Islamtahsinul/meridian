// Space module: one map layer per imagery product of the catalogue (NASA GIBS WMTS and EUMETSAT
// EUMETView WMS): weather overlays and the imagery products of the extension pack. Times come from the
// server catalogue; products may offer variants, a composite of the newest N times and a chosen day.

import { COLOR, addRaster, gibsCatalogue, productTileUrl, hm, dateLabel, clamp, addDays, isoDate, validDate, pickDate,
  watchTileErrors, resetTileErrors } from './common.js';

export const SUB_DAILY = new Set(['PT5M', 'PT10M', 'PT30M']);

// Order inside the polygons slot (common.js addRaster): true colour 10 and night lights 2016 12 sit below;
// the RainViewer radar is 50 and the aurora 55.
const RANK = { nightdaily: 13, sar: 14, meteosat: 15, flood: 16, sst: 20, seaice: 21, lst: 22, snow: 24, aod: 26,
  no2: 27, so2: 28, clouds: 30, precip: 40, lightning: 52 };
const DEFAULT_OPACITY = { precip: 85, clouds: 55, sst: 80, lst: 80, aod: 75, snow: 85 };
const SUBTITLES = {
  precip: 'GPM IMERG, half-hourly, about 5 hours behind', clouds: 'GOES, Himawari and Meteosat infrared, every 10 minutes',
  sst: 'GHRSST MUR, daily', lst: 'MODIS Terra, daily', aod: 'MODIS Terra and Aqua, daily', snow: 'MODIS Terra, daily',
};
const VARIANT_LABEL = { so2: 'Plume height', nightdaily: 'Version', flood: 'Window', meteosat: 'View' };

// Geostationary discs, split at the antimeridian so no box wraps. GIBS answers 404 for tiles outside a
// satellite's view, so these keep MapLibre from asking for them. Catalogue layer bounds win over these.
const DISCS = [[/^GOES-East/i, [-156, -81, 6, 81]], [/^GOES-West/i, [-180, -81, -56, 81]], [/^Himawari/i, [60, -81, 180, 81]]];

export function whenChoices(product) {
  const combo = product?.composite ? [{ value: 'combo', label: product.composite_label || `Last ${product.composite} combined` }] : [];
  if (SUB_DAILY.has(product?.period)) {
    const latest = [{ value: 'latest', label: 'Latest' }];
    return [...latest, ...combo, { value: 'h1', label: '1 hour earlier' }, { value: 'h3', label: '3 hours earlier' },
      { value: 'h6', label: '6 hours earlier' }, { value: 'h12', label: '12 hours earlier' }, { value: 'h24', label: '24 hours earlier' }];
  }
  const latest = { value: 'latest', label: product?.composite ? 'Latest day only' : 'Latest available' };
  const days = [{ value: 'd1', label: '1 day earlier' }, { value: 'd2', label: '2 days earlier' }, { value: 'd3', label: '3 days earlier' },
    { value: 'd5', label: '5 days earlier' }, { value: 'd7', label: '7 days earlier' }, { value: 'custom', label: 'A day I choose' }];
  return product?.composite_default ? [...combo, latest, ...days] : [latest, ...combo, ...days];
}

/** The layer entries a product shows for a variant id (all of them when it has no variants). */
export function shownLayers(product, variant) {
  const variants = product?.variants || [];
  if (!variants.length) return product?.layers || [];
  const v = variants.find(x => x.id === variant) || variants[0];
  return (product.layers || []).filter(le => v.layers.includes(le.layer));
}

/** The time a layer opens at: 10-minute and 5-minute frames one before the newest (they are listed before
 *  every tile is rendered); daily products at their default (the latest complete day for swaths). */
function baseTime(product, le) {
  const times = le.times || [];
  const fast = product.period === 'PT10M' || product.period === 'PT5M';
  if (SUB_DAILY.has(product.period)) return fast && times.length > 1 ? times[times.length - 2] : le.latest;
  return le.default || product.default_time || le.latest;
}

/** Resolve a 'when' option to a concrete time for one layer of a product. */
export function resolveTime(product, le, when, custom = null) {
  const base = baseTime(product, le);
  if (!base) return null;
  if (!when || when === 'latest' || when === 'combo') return base;
  if (when === 'custom') return validDate(custom) && !SUB_DAILY.has(product.period) ? custom : base;
  const m = /^([hd])(\d+)$/.exec(when);
  if (!m) return base;
  const n = +m[2];
  const times = le.times || [];
  const t = m[1] === 'h' ? Date.parse(base) - n * 3600000 : Date.parse(`${base}T00:00:00Z`) - n * 86400000;
  const fmt = SUB_DAILY.has(product.period) ? (x) => new Date(x).toISOString().replace(/\.\d{3}Z$/, 'Z') : (x) => new Date(x).toISOString().slice(0, 10);
  const target = fmt(t);
  const earlier = times.filter(x => x <= target);
  return earlier.length ? earlier[earlier.length - 1] : target;
}

/** The newest N times of a composite, oldest first, ending at the layer's opening time (or a given day). */
export function compositeTimes(product, le, endDay = null) {
  const n = Math.max(1, product.composite || 1);
  if (endDay && !SUB_DAILY.has(product.period)) {
    const out = [];
    for (let i = n - 1; i >= 0; i--) out.push(addDays(endDay, -i));
    return out;
  }
  const end = baseTime(product, le);
  const times = (le.times || []).filter(t => t <= end);
  return times.slice(Math.max(0, times.length - n));
}

/** Latitudes where the Sun is within `solar_bounds` degrees of the zenith at noon on the day shown:
 *  products that need daylight (aerosols) are only requested there. */
function sunBounds(product, time) {
  if (!product?.solar_bounds || !time) return null;
  const t = Date.parse(time.length === 10 ? `${time}T12:00:00Z` : time);
  if (Number.isNaN(t)) return null;
  const doy = Math.floor((t - Date.UTC(new Date(t).getUTCFullYear(), 0, 0)) / 86400000);
  const dec = 23.44 * Math.sin((2 * Math.PI * (284 + doy)) / 365);
  const reach = product.solar_bounds;
  return [-180, clamp(dec - reach, -85, -40), 180, clamp(dec + reach, 40, 85)];
}

function layerBounds(product, le, time) {
  const sun = sunBounds(product, time);
  const disc = le?.bounds || (DISCS.find(([re]) => re.test(String(le?.layer || ''))) || [])[1];
  if (!disc) return sun;
  if (!sun) return disc;
  return [Math.max(disc[0], sun[0]), Math.max(disc[1], sun[1]), Math.min(disc[2], sun[2]), Math.min(disc[3], sun[3])];
}

function attributionFor(le, credit) {
  return le.provider === 'eumetsat'
    ? `<a href="https://www.eumetsat.int/legal-framework/data-policy">${credit || 'EUMETSAT Meteosat product'}</a>`
    : '<a href="https://www.earthdata.nasa.gov/gibs">NASA GIBS</a>';
}

/**
 * Register one catalogue product as a map layer.
 * hooks.reference(on): borders and place names above opaque imagery (imagery.js owns them).
 */
export function registerProductLayer(ctx, pid, meta, hooks = {}) {
  const P = { pid, layer: null, product: meta, shown: '', credit: null, sids: new Set() };
  const id = `space.${pid}`;
  const prefix = `ov-space-gibs-${pid}-`;
  const group = meta?.group || 'Weather';
  const sub = SUB_DAILY.has(meta?.period);
  const opacity0 = DEFAULT_OPACITY[pid] ?? Math.round((meta?.opacity ?? 0.8) * 100);
  const settingKey = `space.${pid}.date`;

  const removeAll = () => {
    for (const sid of P.sids) ctx.map2.removeSource(sid);
    P.sids.clear();
  };

  const apply = (options) => {
    const { map, map2 } = ctx;
    const product = P.product;
    if (!product || !P.layer.isOn()) return;
    const op = (options.opacity ?? opacity0) / 100;
    const when = options.when === 'combo' && !product.composite ? 'latest' : (options.when || 'latest');
    const custom = when === 'custom' ? ctx.settings.get(settingKey, null) : null;
    const plan = [];
    shownLayers(product, options.variant).forEach((le, i) => {
      if (when === 'combo') {
        compositeTimes(product, le).forEach((t, k) => plan.push({ sid: `${prefix}${i}-${k}`, le, time: t, rank: (RANK[pid] ?? 25) + i * 0.1 + k * 0.01 }));
      } else {
        plan.push({ sid: `${prefix}${i}`, le, time: resolveTime(product, le, when, custom), rank: (RANK[pid] ?? 25) + i * 0.1 });
      }
    });
    const wanted = new Set(plan.map(p => p.sid));
    for (const sid of [...P.sids]) if (!wanted.has(sid)) { map2.removeSource(sid); P.sids.delete(sid); }
    for (const p of plan) {
      if (!p.time) continue;
      const url = productTileUrl(ctx._spaceGibsBase, product, p.le, p.time);
      const bounds = layerBounds(product, p.le, p.time);
      const src = map.getSource(p.sid);
      // Bounds are fixed per source, and a new time gets a new source too: in MapLibre 5.24 setTiles() turns
      // tiles whose first load failed (fresh frames often 404) into "reloading" tiles without a texture,
      // and the raster draw then throws on every frame until they reload.
      if (src && ((src._spaceBounds ?? '') !== (bounds || []).join() || src._spaceUrl !== url)) map2.removeSource(p.sid);
      if (!map.getSource(p.sid)) {
        const spec = { type: 'raster', tiles: [url], tileSize: 256, maxzoom: p.le.maxzoom || product.maxzoom || 8,
          attribution: attributionFor(p.le, P.credit) };
        if (bounds) spec.bounds = bounds;
        map2.addSource(p.sid, spec);
        map.getSource(p.sid)._spaceBounds = bounds ? bounds.join() : '';
        map.getSource(p.sid)._spaceUrl = url;
        P.sids.add(p.sid);
      }
      if (!map.getLayer(p.sid)) {
        const paint = { 'raster-opacity': op, 'raster-fade-duration': 200, ...(product.paint || {}) };
        if (pid === 'clouds') { paint['raster-contrast'] = 0.35; paint['raster-brightness-max'] = 0.95; }
        addRaster(map, map2, { id: p.sid, type: 'raster', source: p.sid, paint }, p.rank);
      } else {
        map.setPaintProperty(p.sid, 'raster-opacity', op);
      }
    }
    if (product.reference) hooks.reference?.(true, pid);
    const times = plan.map(p => p.time).filter(Boolean).sort();
    const key = times.join();
    if (key !== P.shown) resetTileErrors(prefix);
    P.shown = key;
    const last = times[times.length - 1];
    const label = (t) => (sub ? `${hm(t)} UTC` : dateLabel(t, false));
    let count = last ? label(last) : null;
    if (when === 'combo' && times.length > 1) count = sub ? `${hm(times[0])} to ${hm(last)} UTC` : `${dateLabel(times[0], false)} to ${dateLabel(last, false)}`;
    P.layer.setCount(count);
    const shownEntries = shownLayers(product, options.variant);
    const est = shownEntries.some(l => l.estimated);
    const latest = shownEntries.map(l => l.latest).filter(Boolean).sort().pop();
    if (est) {
      P.layer.setStatus('warn', shownEntries.some(l => l.provider === 'eumetsat')
        ? 'EUMETSAT did not say which frames are ready, so this shows a best guess.'
        : 'NASA GIBS did not say which dates are ready, so this shows a best guess.');
    } else if (when === 'custom' && custom && latest && custom > latest) {
      P.layer.setStatus('warn', `${product.title} is only available to ${dateLabel(latest)} so far.`);
    } else {
      P.layer.setStatus('ok', null);
    }
  };

  watchTileErrors(ctx.map, prefix, () => {
    if (!P.layer?.isOn()) return;
    // a combined view expects gaps: GIBS answers 404 for days without a pass over an area
    if (P.product?.composite && P.layer.option('when') === 'combo') return;
    P.layer.setStatus('warn', pid === 'sar' ? 'Some radar tiles did not load. Days without a pass over an area are normal.'
      : 'Some tiles did not load. The newest frame may still be in production; try an earlier time.');
  });

  const options = [
    { id: 'opacity', label: 'Opacity', type: 'range', min: 10, max: 100, step: 5, default: opacity0, format: v => `${v}%`, refresh: false },
  ];
  if (meta?.variants?.length) {
    options.push({ id: 'variant', label: VARIANT_LABEL[pid] || 'Show', type: 'select', refresh: false,
      choices: meta.variants.map(v => ({ value: v.id, label: v.label })), default: meta.variants[0].id });
  }
  options.push({ id: 'when', label: 'Showing', type: 'select', choices: whenChoices(meta), refresh: false,
    default: meta?.composite_default ? 'combo' : 'latest' });

  P.layer = ctx.layers.register({
    id, group, title: meta?.title || pid, subtitle: meta?.subtitle || SUBTITLES[pid] || '',
    description: meta?.about || '', source: meta?.source || 'NASA GIBS',
    color: group === 'Imagery' ? COLOR.imagery : COLOR.weather, symbol: 'area',
    refreshMs: meta?.period === 'PT5M' ? 5 * 60000 : sub ? 10 * 60000 : 3 * 3600000,
    options,
    legend: (meta?.legend || []).map(([color, label]) => ({ color, label, shape: 'area' })),
    async refresh({ options: opts, signal }) {
      const cat = await gibsCatalogue(ctx.api, { products: [pid], maxAgeMs: sub ? 5 * 60000 : 30 * 60000 });
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      ctx._spaceGibsBase = cat.base;
      P.credit = cat.meteosat_credit || P.credit;
      P.product = cat.products[pid] || P.product;
      apply(opts);
    },
    async disable() {
      removeAll();
      if (meta?.reference) hooks.reference?.(false, pid);
    },
    async onOption(optId, value, all) {
      if (optId === 'when' && value === 'custom') {
        const latest = (shownLayers(P.product, all.variant)[0] || {}).latest;
        const d = await pickDate(ctx, ctx.settings.get(settingKey, null) || (validDate(latest) ? latest : isoDate(Date.now() - 86400000)),
          { note: `${P.product?.title || pid}${latest ? `: the newest day available is ${dateLabel(latest)}.` : '.'}` });
        if (d) ctx.settings.set(settingKey, d);
      }
      if (P.layer.isOn()) apply(all);
    },
  });
  return P.layer;
}
