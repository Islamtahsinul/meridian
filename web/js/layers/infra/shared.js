// Shared helpers for the infra module's layers: dataset readiness, cached detail fetches, lazily
// loaded GeoJSON sources, colours and small formatters.

export const INFRA = '#c9b458';
export const COMPANIES = '#a7b6ff';
export const INK = '#111a2c';
export const PAPER = '#ebe6d6';
export const AMBER = '#ffb547';
export const RED = '#ff5d52';

export const FUELS = [
  { id: 'coal', label: 'Coal', color: '#a68a74' },
  { id: 'gas', label: 'Gas', color: '#e9a15f' },
  { id: 'oil', label: 'Oil', color: '#c4664f' },
  { id: 'nuclear', label: 'Nuclear', color: '#c58df0' },
  { id: 'hydro', label: 'Hydro', color: '#3f9fe0' },
  { id: 'wind', label: 'Wind', color: '#8fe0d2' },
  { id: 'solar', label: 'Solar', color: '#f3d65a' },
  { id: 'biomass', label: 'Biomass', color: '#8cc26a' },
  { id: 'waste', label: 'Waste', color: '#9aa36b' },
  { id: 'geothermal', label: 'Geothermal', color: '#ea8aa0' },
  { id: 'storage', label: 'Storage', color: '#9fb0d8' },
  { id: 'other', label: 'Other', color: '#b6b0a2' },
];
export const FUEL_GROUPS = {
  fossil: ['coal', 'gas', 'oil', 'petcoke', 'cogeneration'],
  clean: ['nuclear', 'hydro', 'wind', 'solar', 'geothermal', 'tidal', 'biomass', 'waste', 'storage'],
  renewable: ['hydro', 'wind', 'solar', 'geothermal', 'tidal', 'biomass'],
};

export function fuelColorExpr() {
  const expr = ['match', ['get', 'f']];
  for (const f of FUELS) expr.push(f.id, f.color);
  expr.push('petcoke', '#8e7766', 'cogeneration', '#d99b72', 'tidal', '#59c2c9', '#b6b0a2');
  return expr;
}

export function fuelLabel(id) {
  return (FUELS.find(f => f.id === id) || {}).label || ({ petcoke: 'Petcoke', cogeneration: 'Cogeneration', tidal: 'Wave and tidal' })[id] || 'Other';
}

const DATASET_LABELS = {
  power: 'power plant list', airports: 'airport list', ports: 'port list', datacenters: 'data centre list',
  dams: 'dam list', cables: 'cable map', companies: 'company list', commodities: 'commodity prices',
};

export function createShared(ctx) {
  const { api, h, fmt } = ctx;
  let statusDoc = null;
  let statusAt = 0;
  let inflight = null;
  const cache = new Map();
  const waiting = new Map();   // dataset -> timer

  async function status(force = false) {
    if (!force && statusDoc && Date.now() - statusAt < 15000) return statusDoc;
    if (!inflight) {
      inflight = api.get('/api/infra/status').then((doc) => { statusDoc = doc; statusAt = Date.now(); return doc; })
        .finally(() => { inflight = null; });
    }
    return inflight;
  }

  /** True when the dataset is built. Otherwise shows why on the layer row and calls onReady later. */
  async function ensureReady(dataset, layer, onReady) {
    let doc;
    try { doc = await status(true); } catch (err) {
      layer.setStatus('error', 'The Meridian server is not answering.');
      return false;
    }
    const d = doc.datasets?.[dataset] || {};
    if (d.ready) return true;
    const what = DATASET_LABELS[dataset] || dataset;
    if (d.error && !d.building) layer.setStatus('error', `The ${what} could not be built: ${d.error}`);
    else layer.setStatus('warn', `The ${what} is still being built on the server. It appears here when ready.`);
    if (!waiting.has(dataset)) {
      const timer = setInterval(async () => {
        if (!layer.isOn()) { clearInterval(timer); waiting.delete(dataset); return; }
        try {
          const s2 = await status(true);
          if (s2.datasets?.[dataset]?.ready) {
            clearInterval(timer);
            waiting.delete(dataset);
            layer.setStatus('loading');
            await onReady();
            layer.setStatus('ok');
          }
        } catch { /* keep waiting */ }
      }, 20000);
      waiting.set(dataset, timer);
    }
    return false;
  }

  /** GET with a small in-memory cache (details rarely change). */
  async function get(path, params = null, { ttlMs = 10 * 60 * 1000, signal } = {}) {
    const key = path + (params ? '?' + new URLSearchParams(params).toString() : '');
    const hit = cache.get(key);
    if (hit && Date.now() - hit.at < ttlMs) return hit.value;
    const value = await api.get(path, params, { signal });
    cache.set(key, { at: Date.now(), value });
    if (cache.size > 300) cache.delete(cache.keys().next().value);
    return value;
  }

  const geoUrl = (name) => api.url(`/api/infra/geo/${name}`);

  /**
   * A GeoJSON source added only once the map reaches minzoom (saves the download at world view).
   * addLayers() is called right after the source is added.
   */
  function lazySource(sourceId, url, minzoom, addLayers) {
    let added = false;
    let off = null;
    const check = () => {
      if (added || ctx.map.getZoom() < minzoom - 0.25) return;
      added = true;
      ctx.map2.addSource(sourceId, { type: 'geojson', data: url, attribution: '' });
      addLayers();
    };
    return {
      start() { added = !!ctx.map.getSource(sourceId); check(); off = ctx.bus.on('map:moveend', check); },
      stop() { off?.(); off = null; ctx.map2.removeSource(sourceId); added = false; },
      get loaded() { return added; },
    };
  }

  function entityPath(kind, id) {
    const seg = encodeURIComponent(String(id));
    return { power_plant: `/api/infra/power/${seg}`, airport: `/api/infra/airport/${seg}`, port: `/api/infra/port/${seg}`,
      datacenter: `/api/infra/datacenter/${seg}`, dam: `/api/infra/dam/${seg}`, cable: `/api/infra/cable/${seg}`,
      landing_point: `/api/infra/landing/${seg}`, company: `/api/infra/company/${seg}` }[kind];
  }

  /** A card that says the thing could not be found (stale favourite, removed from the source). */
  function missingCard(entity, what, accent = INFRA, err = null) {
    const gone = err?.status === 404;
    return ctx.cards.open({
      key: `${entity.kind}:${entity.id}`, kind: entity.kind, title: entity.title || what, subtitle: entity.subtitle || '',
      icon: '?', accent,
      sections: [{ text: gone ? `This ${what} is no longer in the source data.` : (err?.status === 503
        ? `The ${what} list is still being built. Try again in a minute.` : `The ${what} details could not be loaded right now.`) }],
      entity: ctx.dom.validLatLon(entity.lat, entity.lon) ? entity : null,
    });
  }

  function linkRow(label, href, text) {
    const url = ctx.dom.safeUrl(href);
    return url ? [label, h('a', { href: url }, text || shortUrl(url))] : null;
  }

  function shortUrl(url) {
    try { const u = new URL(url); return (u.hostname.replace(/^www\./, '') + (u.pathname !== '/' ? u.pathname : '')).slice(0, 48); } catch { return url; }
  }

  function power(mw) {
    if (mw == null || Number.isNaN(+mw)) return { value: '–', unit: '' };
    const v = +mw;
    if (v >= 10000) return { value: fmt.num(v / 1000, 1), unit: 'GW' };
    if (v >= 1000) return { value: fmt.num(v / 1000, 2), unit: 'GW' };
    return { value: fmt.num(v, v < 10 ? 1 : 0), unit: 'MW' };
  }

  function bigMoney(amount, currency) {
    const p = moneyParts(amount, currency);
    return p ? `${p.value}${p.unit ? ` ${p.unit}` : ''}` : null;
  }

  /** {value: "$5.2", unit: "tn"} for data blocks; currencies without a symbol keep their code. */
  function moneyParts(amount, currency) {
    if (amount == null || Number.isNaN(+amount)) return null;
    const v = +amount;
    const sym = { USD: '$', EUR: '€', GBP: '£', JPY: '¥', CNY: '¥', INR: '₹', KRW: '₩' }[currency || 'USD'];
    const [scale, unit] = Math.abs(v) >= 1e12 ? [1e12, 'tn'] : Math.abs(v) >= 1e9 ? [1e9, 'bn'] : Math.abs(v) >= 1e6 ? [1e6, 'm'] : [1, ''];
    const n = v / scale;
    const num = fmt.num(n, Math.abs(n) >= 100 ? 0 : 1);
    return sym ? { value: `${sym}${num}`, unit } : { value: num, unit: `${unit} ${currency || ''}`.trim() };
  }

  function placeLine(...parts) { return parts.filter(Boolean).join(', '); }

  return { ctx, status, ensureReady, get, geoUrl, lazySource, entityPath, missingCard, linkRow, shortUrl, power, bigMoney, moneyParts, placeLine };
}

/** Circle radius that grows with zoom and with sqrt(value). */
export function sqrtRadius(prop, { z = [2, 6, 10, 14], base = [1, 2, 3, 4.5], k = [0.07, 0.12, 0.2, 0.28], max = 28, fallback = 0 } = {}) {
  const v = ['sqrt', ['max', 0, ['coalesce', ['get', prop], fallback]]];
  const expr = ['interpolate', ['linear'], ['zoom']];
  z.forEach((zz, i) => expr.push(zz, ['min', max, ['+', base[i], ['*', k[i], v]]]));
  return expr;
}
