// Shared helpers for the hazards layers: palettes, canvas icons, the selection overlay (outline of
// the warning, disaster area or storm you opened), formatting and the "Compare imagery" action.

export const GROUP = 'Events';
export const ACCENT = '#f0a43a';
export const INK = '#111a2c';
export const PAPER = '#ebe6d6';
export const RED = '#ff5d52';
export const AMBER = '#ffb547';
export const GREEN = '#5fd38d';
export const BLUE = '#6cb6ff';

// Quake age (hours) -> colour: fresh is hot, old fades to chart-paper.
export const QUAKE_AGE = [
  { h: 1, color: '#ff5d52', label: 'Past hour' },
  { h: 24, color: '#ff8c42', label: 'Past day' },
  { h: 24 * 7, color: '#ffd166', label: 'Past week' },
  { h: Infinity, color: '#b9b3a0', label: 'Older' },
];
export const PAGER = { green: GREEN, yellow: '#ffe066', orange: '#ff8c42', red: RED };
export const ALERT = { Green: GREEN, Orange: '#ff9f43', Red: RED };
export const SEVERITY = { 1: BLUE, 2: '#ffd34d', 3: '#ff8c42', 4: RED };
export const SEVERITY_NAME = { 0: 'Unknown', 1: 'Minor', 2: 'Moderate', 3: 'Severe', 4: 'Extreme' };
export const STORM = { '-2': '#9aa6bd', '-1': '#6cb6ff', 0: '#5fd38d', 1: '#ffe066', 2: '#ffc04d', 3: '#ff8c42', 4: '#ff5d52', 5: '#d65cd0' };
export const FRP_RAMP = ['interpolate', ['linear'], ['coalesce', ['get', 'f'], 0], 0, '#ffd166', 10, '#ffab40', 40, '#ff7b39', 150, '#ff4e3a', 600, '#ff2d55'];
export const EONET = {
  wildfires: '#ff7b39', severeStorms: '#86d6f7', volcanoes: '#ff5d52', floods: '#4fa8e8', seaLakeIce: '#cfe8ff',
  landslides: '#c39a6b', dustHaze: '#d9b26f', drought: '#d9a441', snow: '#e8f1ff', tempExtremes: '#ff8c42',
  earthquakes: '#ffb547', manmade: '#a7b6ff', waterColor: '#43c4b2',
};
export const EONET_LABEL = {
  wildfires: 'Wildfires', severeStorms: 'Severe storms', volcanoes: 'Volcanoes', floods: 'Floods', seaLakeIce: 'Sea and lake ice',
  landslides: 'Landslides', dustHaze: 'Dust and haze', drought: 'Drought', snow: 'Snow', tempExtremes: 'Temperature extremes',
  earthquakes: 'Earthquakes', manmade: 'Manmade', waterColor: 'Water colour',
};
export const OUTBREAK = '#e07ad4';
export const TSUNAMI = { Warning: RED, Threat: '#ff8c42', Watch: '#ff9f43', Advisory: AMBER, Information: BLUE, Cancellation: '#9aa6bd' };
export const GDACS_TYPE = { EQ: 'Earthquake', TC: 'Tropical cyclone', FL: 'Flood', VO: 'Volcano', DR: 'Drought', WF: 'Wildfire' };

// Card head glyphs (plain text symbols, no emoji).
export const GLYPH = { earthquake: '◎', fire: '✹', volcano: '▲', storm: '◍', flood: '≋', warning: '!', outbreak: '✚',
  disaster: '◆', tsunami: '≈', emergency_mapping: '▣', gauge: '▮' };

// Copernicus EMS categories (cems.py keys) and England flood levels (ea.py).
export const CEMS_COLOR = { flood: '#4fa8e8', fire: '#ff7b39', earthquake: '#ffb547', storm: '#86d6f7', landslide: '#c39a6b',
  volcano: '#ff5d52', tsunami: '#6cb6ff', drought: '#d9a441', industrial: '#a7b6ff', humanitarian: '#e07ad4', other: '#a7b6ff' };
export const CEMS_ICON = { flood: 'hazards-flood', fire: 'hazards-fire', earthquake: 'hazards-quake', storm: 'hazards-storm',
  landslide: 'hazards-slide', volcano: 'hazards-volcano', tsunami: 'hazards-wave', drought: 'hazards-drought' };
export const EA_LEVEL = { 1: '#ff5d52', 2: '#ff8c42', 3: '#ffd34d', 4: '#9aa6bd' };
export const EA_TONE = { 1: 'red', 2: 'amber', 3: 'amber', 4: 'grey' };
export const GAUGE = { highest: '#ff5d52', high: '#ffb547', normal: '#6cb6ff', low: '#9cc29a', unknown: '#9aa6bd' };

export function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => { clearTimeout(t); reject(new DOMException('Aborted', 'AbortError')); }, { once: true });
  });
}

export function stepExpr(input, stops, fallback) {
  // stops: [[threshold, value], ...] ascending; values below the first threshold use stops[0][1]
  const expr = ['step', input, stops[0][1]];
  for (let i = 1; i < stops.length; i++) expr.push(stops[i][0], stops[i][1]);
  return fallback === undefined ? expr : ['coalesce', expr, fallback];
}

export function matchExpr(input, table, fallback) {
  const expr = ['match', input];
  for (const [k, v] of Object.entries(table)) expr.push(isNaN(+k) ? k : +k, v);
  expr.push(fallback);
  return expr;
}

export const now = () => Date.now() / 1000;

export function ageHours(t) {
  return t ? (now() - t) / 3600 : Infinity;
}

export function quakeAgeColor(t) {
  const h = ageHours(t);
  return (QUAKE_AGE.find(a => h < a.h) || QUAKE_AGE[QUAKE_AGE.length - 1]).color;
}

export function ymd(t) {
  const d = new Date((t || now()) * 1000);
  return d.toISOString().slice(0, 10);
}

export function daysFrom(t, days) {
  return (t || now()) + days * 86400;
}

/** before/after dates for the imagery tool: before = t - beforeDays, after = min(today, t + afterDays). */
export function imageryDates(t, beforeDays = 7, afterDays = 3) {
  const start = t || now();
  return { before: ymd(daysFrom(start, -beforeDays)), after: ymd(Math.min(now(), daysFrom(start, afterDays))) };
}

export function clip(text, n) {
  if (!text) return '';
  const s = String(text);
  return s.length > n ? `${s.slice(0, n - 1).replace(/\s+\S*$/, '')}…` : s;
}

export function sentence(text) {
  if (!text) return '';
  const s = String(text).trim();
  if (s === s.toUpperCase() && /[A-Z]{4}/.test(s)) return s.charAt(0) + s.slice(1).toLowerCase();
  return s;
}

// Some MeteoAlarm feeds (Romania, for one) send the raw CAP parameters as the event name:
// "Awareness_type=1, awareness_level=3". Turn those into "Orange wind warning".
const AWARENESS_TYPE = { 1: 'wind', 2: 'snow and ice', 3: 'thunderstorm', 4: 'fog', 5: 'high temperature', 6: 'low temperature',
  7: 'coastal', 8: 'forest fire', 9: 'avalanche', 10: 'rain', 12: 'flood', 13: 'rain and flood' };
const AWARENESS_LEVEL = { 1: 'Green', 2: 'Yellow', 3: 'Orange', 4: 'Red' };

export function warningName(event, hazard = null) {
  const s = String(event || '').trim();
  const m = /^awareness_type\s*=\s*(\d+)\s*[,;]?\s*awareness_level\s*=\s*(\d+)/i.exec(s);
  if (!m) return s || 'Weather warning';
  const type = AWARENESS_TYPE[+m[1]] || (hazard ? String(hazard).toLowerCase() : 'weather');
  const level = AWARENESS_LEVEL[+m[2]];
  return level ? `${level} ${type} warning` : `${type.charAt(0).toUpperCase()}${type.slice(1)} warning`;
}

/** A tiny kit shared by every hazards layer, built once in register(ctx). */
export function createKit(ctx) {
  const { map2, fmt } = ctx;
  const selections = new Map();   // card key -> features drawn in the selection overlay
  let selectionReady = false;

  function ensureSelection() {
    if (selectionReady && ctx.map.getSource('ov-hazards-selection')) return;
    map2.addSource('ov-hazards-selection', { type: 'geojson', data: map2.emptyFC() });
    map2.addLayer({ id: 'ov-hazards-selection-fill', type: 'fill', source: 'ov-hazards-selection',
      filter: ['in', ['geometry-type'], ['literal', ['Polygon', 'MultiPolygon']]],
      paint: { 'fill-color': ['coalesce', ['get', 'color'], ACCENT], 'fill-opacity': 0.16 } }, 'polygons');
    map2.addLayer({ id: 'ov-hazards-selection-line', type: 'line', source: 'ov-hazards-selection',
      paint: { 'line-color': ['coalesce', ['get', 'color'], ACCENT], 'line-width': 2.2, 'line-opacity': 0.95 } }, 'lines');
    selectionReady = true;
  }

  function renderSelection() {
    ensureSelection();
    const feats = [];
    for (const list of selections.values()) feats.push(...list);
    map2.setData('ov-hazards-selection', { type: 'FeatureCollection', features: feats });
  }

  const kit = {
    ctx,
    /** Draw an outline for an open card (replaces that card's previous one). */
    select(key, geometryOrFeatures, color = ACCENT) {
      if (!geometryOrFeatures) return;
      const feats = Array.isArray(geometryOrFeatures)
        ? geometryOrFeatures
        : [{ type: 'Feature', geometry: geometryOrFeatures, properties: { color } }];
      for (const f of feats) f.properties = { color, ...(f.properties || {}) };
      selections.set(key, feats);
      renderSelection();
    },
    unselect(key) {
      if (!selections.delete(key)) return;
      renderSelection();
    },
    compareAction(lat, lon, zoom, dates) {
      return {
        text: 'Compare imagery',
        onClick: () => {
          ctx.bus.emit('imagery:compare', { lat, lon, zoom, before: dates.before, after: dates.after });
          if (!ctx.manifest.modules.some(m => m.name === 'space')) ctx.ui.toast('The imagery tool comes with the space module, which is not loaded here.');
        },
      };
    },
    ago(t) { return t ? fmt.ago(t) : 'time unknown'; },
    utc(t) { return t ? fmt.utc(t) : '–'; },
    when(t) { return t ? `${fmt.utc(t)} (${fmt.ago(t)})` : 'Unknown'; },
    statusFrom(layer, meta, emptyText, countText) {
      const src = meta?.source || {};
      if (src.pending) { layer.setStatus('loading', 'Fetching the first data from the source…'); return 'pending'; }
      if (src.error && !src.updated_at) { layer.setStatus('error', `Source unavailable: ${src.error}`); return 'error'; }
      if (src.error) { layer.setStatus('warn', `Showing data from ${fmt.ago(src.updated_at)}; the last refresh failed (${src.error}).`); return 'stale'; }
      const n = meta?.count ?? 0;
      layer.setStatus('ok', n ? (countText || null) : emptyText);
      return 'ok';
    },
    /** GET, then keep asking (with the layer still showing "loading") while isPending(data). */
    async fetchReady(path, params, signal, isPending, { tries = 6, delay = 2500 } = {}) {
      let data = await ctx.api.get(path, params, { signal });
      for (let i = 0; i < tries && isPending(data); i++) {
        await sleep(delay, signal);
        data = await ctx.api.get(path, params, { signal });
      }
      return data;
    },
    retryLater(layer, ms = 4000) {
      setTimeout(() => { if (layer.isOn()) layer.refresh('retry'); }, ms);
    },
    /** Long text clipped to about n characters, with a "Show all" button. */
    expandable(body, n) {
      const { h } = ctx;
      const full = String(body || '');
      if (full.length <= n + 40) return h('div', { class: 'hazards-text' }, full);
      const el = h('div', { class: 'hazards-text' }, clip(full, n));
      const more = h('button', { type: 'button', class: 'hazards-more' }, 'Show all');
      more.addEventListener('click', () => { el.textContent = full; more.remove(); });
      return h('div', null, el, more);
    },
  };
  registerIcons(ctx);
  ctx.bus.on('card:closed', ({ key }) => kit.unselect(key));
  return kit;
}

// ------------------------------------------------------------------ canvas icons (white, SDF-tinted)
function registerIcons(ctx) {
  const icon = (name, draw, opts) => ctx.map2.icon(`hazards-${name}`, draw, opts);
  const W = '#fff';
  icon('volcano', (g, s) => {
    g.fillStyle = W;
    g.beginPath();
    g.moveTo(s * 0.08, s * 0.86); g.lineTo(s * 0.4, s * 0.22); g.lineTo(s * 0.5, s * 0.32); g.lineTo(s * 0.6, s * 0.22);
    g.lineTo(s * 0.92, s * 0.86); g.closePath(); g.fill();
  });
  icon('storm', (g, s) => {
    const c = s / 2;
    g.strokeStyle = W; g.fillStyle = W; g.lineWidth = s * 0.13; g.lineCap = 'round';
    g.beginPath(); g.arc(c, c, s * 0.16, 0, Math.PI * 2); g.fill();
    g.beginPath(); g.arc(c, c, s * 0.3, Math.PI * 1.05, Math.PI * 1.75); g.stroke();
    g.beginPath(); g.arc(c, c, s * 0.3, Math.PI * 0.05, Math.PI * 0.75); g.stroke();
    g.beginPath(); g.moveTo(c - s * 0.3, c - s * 0.02); g.quadraticCurveTo(c - s * 0.36, c - s * 0.36, c - s * 0.06, c - s * 0.42); g.stroke();
    g.beginPath(); g.moveTo(c + s * 0.3, c + s * 0.02); g.quadraticCurveTo(c + s * 0.36, c + s * 0.36, c + s * 0.06, c + s * 0.42); g.stroke();
  });
  icon('fire', (g, s) => {
    g.fillStyle = W;
    g.beginPath();
    g.moveTo(s * 0.5, s * 0.06);
    g.bezierCurveTo(s * 0.62, s * 0.3, s * 0.86, s * 0.42, s * 0.8, s * 0.66);
    g.bezierCurveTo(s * 0.76, s * 0.86, s * 0.62, s * 0.94, s * 0.5, s * 0.94);
    g.bezierCurveTo(s * 0.34, s * 0.94, s * 0.2, s * 0.84, s * 0.2, s * 0.64);
    g.bezierCurveTo(s * 0.2, s * 0.46, s * 0.34, s * 0.4, s * 0.36, s * 0.24);
    g.bezierCurveTo(s * 0.44, s * 0.32, s * 0.46, s * 0.4, s * 0.44, s * 0.5);
    g.bezierCurveTo(s * 0.56, s * 0.4, s * 0.54, s * 0.22, s * 0.5, s * 0.06);
    g.fill();
  });
  icon('flood', (g, s) => {
    g.strokeStyle = W; g.lineWidth = s * 0.11; g.lineCap = 'round';
    for (const y of [0.3, 0.52, 0.74]) {
      g.beginPath(); g.moveTo(s * 0.1, s * y);
      g.bezierCurveTo(s * 0.25, s * (y - 0.12), s * 0.35, s * (y + 0.12), s * 0.5, s * y);
      g.bezierCurveTo(s * 0.65, s * (y - 0.12), s * 0.75, s * (y + 0.12), s * 0.9, s * y);
      g.stroke();
    }
  });
  icon('quake', (g, s) => {
    g.strokeStyle = W; g.lineWidth = s * 0.1; g.lineJoin = 'round'; g.lineCap = 'round';
    g.beginPath();
    const pts = [[0.06, 0.55], [0.26, 0.55], [0.36, 0.3], [0.48, 0.8], [0.6, 0.16], [0.7, 0.62], [0.76, 0.55], [0.94, 0.55]];
    pts.forEach(([x, y], i) => (i ? g.lineTo(s * x, s * y) : g.moveTo(s * x, s * y)));
    g.stroke();
  });
  icon('drought', (g, s) => {
    const c = s / 2;
    g.fillStyle = W; g.strokeStyle = W; g.lineWidth = s * 0.08; g.lineCap = 'round';
    g.beginPath(); g.arc(c, c, s * 0.18, 0, Math.PI * 2); g.fill();
    for (let i = 0; i < 8; i++) {
      const a = (i / 8) * Math.PI * 2;
      g.beginPath(); g.moveTo(c + Math.cos(a) * s * 0.28, c + Math.sin(a) * s * 0.28);
      g.lineTo(c + Math.cos(a) * s * 0.42, c + Math.sin(a) * s * 0.42); g.stroke();
    }
  });
  icon('wave', (g, s) => {
    g.fillStyle = W;
    g.beginPath();
    g.moveTo(s * 0.06, s * 0.84);
    g.bezierCurveTo(s * 0.12, s * 0.4, s * 0.42, s * 0.12, s * 0.72, s * 0.2);
    g.bezierCurveTo(s * 0.9, s * 0.26, s * 0.94, s * 0.44, s * 0.82, s * 0.5);
    g.bezierCurveTo(s * 0.7, s * 0.38, s * 0.5, s * 0.42, s * 0.5, s * 0.6);
    g.bezierCurveTo(s * 0.52, s * 0.72, s * 0.7, s * 0.76, s * 0.94, s * 0.72);
    g.lineTo(s * 0.94, s * 0.84); g.closePath(); g.fill();
  });
  icon('ice', (g, s) => {
    const c = s / 2;
    g.strokeStyle = W; g.lineWidth = s * 0.08; g.lineCap = 'round';
    for (let i = 0; i < 3; i++) {
      const a = (i / 3) * Math.PI;
      const dx = Math.cos(a) * s * 0.4; const dy = Math.sin(a) * s * 0.4;
      g.beginPath(); g.moveTo(c - dx, c - dy); g.lineTo(c + dx, c + dy); g.stroke();
    }
  });
  icon('dust', (g, s) => {
    g.strokeStyle = W; g.lineWidth = s * 0.09; g.lineCap = 'round';
    for (const [y, x0, x1] of [[0.3, 0.12, 0.7], [0.5, 0.24, 0.9], [0.7, 0.1, 0.62]]) {
      g.beginPath(); g.moveTo(s * x0, s * y); g.lineTo(s * x1, s * y); g.stroke();
    }
  });
  icon('slide', (g, s) => {
    g.fillStyle = W;
    g.beginPath(); g.moveTo(s * 0.06, s * 0.88); g.lineTo(s * 0.06, s * 0.2); g.lineTo(s * 0.94, s * 0.88); g.closePath(); g.fill();
  });
  icon('heat', (g, s) => {
    g.fillStyle = W;
    g.beginPath(); g.arc(s * 0.5, s * 0.74, s * 0.16, 0, Math.PI * 2); g.fill();
    g.fillRect(s * 0.43, s * 0.12, s * 0.14, s * 0.56);
  });
  icon('outbreak', (g, s) => {
    const c = s / 2;
    g.fillStyle = W; g.strokeStyle = W; g.lineWidth = s * 0.07; g.lineCap = 'round';
    g.beginPath(); g.arc(c, c, s * 0.22, 0, Math.PI * 2); g.fill();
    for (let i = 0; i < 8; i++) {
      const a = (i / 8) * Math.PI * 2;
      g.beginPath(); g.moveTo(c + Math.cos(a) * s * 0.22, c + Math.sin(a) * s * 0.22);
      g.lineTo(c + Math.cos(a) * s * 0.36, c + Math.sin(a) * s * 0.36); g.stroke();
      g.beginPath(); g.arc(c + Math.cos(a) * s * 0.4, c + Math.sin(a) * s * 0.4, s * 0.055, 0, Math.PI * 2); g.fill();
    }
  });
  icon('alert', (g, s) => {
    g.fillStyle = W;
    g.beginPath(); g.moveTo(s * 0.5, s * 0.08); g.lineTo(s * 0.94, s * 0.88); g.lineTo(s * 0.06, s * 0.88); g.closePath(); g.fill();
  });
  icon('dot', (g, s) => {
    g.fillStyle = W;
    g.beginPath(); g.arc(s / 2, s / 2, s * 0.3, 0, Math.PI * 2); g.fill();
  });
  // a map frame (rounded square) for emergency mapping activations, so they read apart from GDACS discs
  icon('frame', (g, s) => {
    const r = s * 0.16; const a = s * 0.1; const b = s * 0.9;
    g.fillStyle = W;
    g.beginPath();
    g.moveTo(a + r, a); g.lineTo(b - r, a); g.quadraticCurveTo(b, a, b, a + r); g.lineTo(b, b - r);
    g.quadraticCurveTo(b, b, b - r, b); g.lineTo(a + r, b); g.quadraticCurveTo(a, b, a, b - r); g.lineTo(a, a + r);
    g.quadraticCurveTo(a, a, a + r, a); g.closePath(); g.fill();
  });
  icon('frame-line', (g, s) => {
    const a = s * 0.12; const b = s * 0.88;
    g.strokeStyle = W; g.lineWidth = s * 0.09;
    g.strokeRect(a, a, b - a, b - a);
  });
}

export const GDACS_ICON = { EQ: 'hazards-quake', TC: 'hazards-storm', FL: 'hazards-flood', VO: 'hazards-volcano', DR: 'hazards-drought', WF: 'hazards-fire' };
export const EONET_ICON = {
  wildfires: 'hazards-fire', severeStorms: 'hazards-storm', volcanoes: 'hazards-volcano', floods: 'hazards-flood', seaLakeIce: 'hazards-ice',
  landslides: 'hazards-slide', dustHaze: 'hazards-dust', drought: 'hazards-drought', snow: 'hazards-ice', tempExtremes: 'hazards-heat',
  earthquakes: 'hazards-quake', manmade: 'hazards-alert', waterColor: 'hazards-flood',
};
