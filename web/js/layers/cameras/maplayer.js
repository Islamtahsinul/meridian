// The "cameras" map layer. MapLibre's worker fetches the lean map file (/api/cameras/geojson: id,
// kind, type, filter tags and heading per camera, see mapfile.py), filters it for the "Show"
// choice and clusters it, so 100,000+ cameras never pass through the page's main thread. SDF
// glyphs tell stills, live video, webcams and page-only cameras apart; viewing-direction cones
// and names (from /api/cameras/labels) appear at street zoom.

import { CAM, LIVE, PAGE, INK } from './shared.js';

const SRC = 'ov-cameras';
const NAMES = 'ov-cameras-names';
const L = {
  clusters: 'ov-cameras-clusters',
  count: 'ov-cameras-count',
  cones: 'ov-cameras-cones',
  points: 'ov-cameras-points',
  labels: 'ov-cameras-labels',
};
export const CLICKABLE = [L.clusters, L.count, L.points, L.labels];
const NAMES_ZOOM = 12;        // names are fetched from here (hover), drawn from LABEL_ZOOM
const LABEL_ZOOM = 14.5;

// "Show" choices as MapLibre expressions on the lean properties (keep in step with catalog.py)
const G = ['coalesce', ['get', 'g'], ''];
const SHOW = {
  all: null,
  traffic: ['!=', ['get', 't'], 1],
  webcam: ['==', ['get', 't'], 1],
  live: ['==', ['get', 'k'], 1],
  still: ['!', ['has', 'k']],
  weather: ['any', ['in', 'w', G], ['in', 'm', G], ['in', 'f', G], ['in', 'v', G], ['in', 'i', G]],
  urban: ['in', 'u', G],
  coastal: ['any', ['in', 'c', G], ['in', 'p', G]],
};
const SHOW_CHOICES = [
  { value: 'all', label: 'All cameras' },
  { value: 'traffic', label: 'Traffic cameras' },
  { value: 'webcam', label: 'Webcams (scenic, weather, city)' },
  { value: 'live', label: 'Live video only' },
  { value: 'still', label: 'Still images only' },
  { value: 'weather', label: 'Weather, mountains and volcanoes' },
  { value: 'urban', label: 'Cities' },
  { value: 'coastal', label: 'Coasts and ports' },
];

// ------------------------------------------------------------------ icons (white shapes for SDF)
function roundRect(g, x, y, w, h, r) {
  g.beginPath();
  g.moveTo(x + r, y);
  g.arcTo(x + w, y, x + w, y + h, r);
  g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r);
  g.arcTo(x, y, x + w, y, r);
  g.closePath();
}

function disc(g, s) {
  g.fillStyle = '#fff';
  g.beginPath();
  g.arc(s / 2, s / 2, s / 2 - 1.5, 0, Math.PI * 2);
  g.fill();
  g.globalCompositeOperation = 'destination-out';
}

function drawStill(g, s) {
  const u = s / 24;
  disc(g, s);
  roundRect(g, 6 * u, 8.8 * u, 12 * u, 8.4 * u, 1.6 * u); g.fill();
  roundRect(g, 9.2 * u, 7 * u, 5.6 * u, 2.6 * u, 0.8 * u); g.fill();
  g.globalCompositeOperation = 'source-over';
  g.beginPath(); g.arc(12 * u, 13 * u, 2.6 * u, 0, Math.PI * 2); g.fill();
}

function drawVideo(g, s) {
  const u = s / 24;
  disc(g, s);
  roundRect(g, 5.2 * u, 8.6 * u, 9.6 * u, 7.6 * u, 1.6 * u); g.fill();
  g.beginPath();
  g.moveTo(14 * u, 12.4 * u); g.lineTo(19 * u, 9 * u); g.lineTo(19 * u, 15.8 * u); g.closePath(); g.fill();
  g.globalCompositeOperation = 'source-over';
}

// a webcam: a picture frame with a mountain and a sun (scenic, weather and city views)
function drawWebcam(g, s) {
  const u = s / 24;
  disc(g, s);
  roundRect(g, 5.6 * u, 7.4 * u, 12.8 * u, 9.6 * u, 1.4 * u); g.fill();
  g.globalCompositeOperation = 'source-over';
  g.beginPath();
  g.moveTo(6.6 * u, 15.9 * u); g.lineTo(10.4 * u, 10.6 * u); g.lineTo(12.9 * u, 13.6 * u);
  g.lineTo(14.6 * u, 11.9 * u); g.lineTo(17.4 * u, 15.9 * u); g.closePath(); g.fill();
  g.beginPath(); g.arc(15.2 * u, 9.9 * u, 1.3 * u, 0, Math.PI * 2); g.fill();
}

function drawCone(g, s) {
  const c = s / 2;
  const r = s / 2 - 1;
  const spread = (30 * Math.PI) / 180;
  const grad = g.createRadialGradient(c, c, 2, c, c, r);
  grad.addColorStop(0, 'rgba(239, 227, 194, 0.55)');
  grad.addColorStop(1, 'rgba(239, 227, 194, 0)');
  g.fillStyle = grad;
  g.beginPath();
  g.moveTo(c, c);
  g.arc(c, c, r, -Math.PI / 2 - spread, -Math.PI / 2 + spread);
  g.closePath();
  g.fill();
}

export function registerIcons(ctx) {
  ctx.map2.icon('cameras-still', drawStill, { size: 24, sdf: true });
  ctx.map2.icon('cameras-video', drawVideo, { size: 24, sdf: true });
  ctx.map2.icon('cameras-webcam', drawWebcam, { size: 24, sdf: true });
  ctx.map2.icon('cameras-cone', drawCone, { size: 64, sdf: false });
}

// ------------------------------------------------------------------ names for hover and labels
const NAME_MAX = 6000;

function nameCache(ctx) {
  const names = new Map();      // camera id -> {n, a}
  let queue = new Set();
  let timer = null;
  const put = (id, value) => {
    names.delete(id);
    names.set(id, value);
    if (names.size > NAME_MAX) names.delete(names.keys().next().value);
  };
  async function flush() {
    timer = null;
    const ids = [...queue].slice(0, 100);
    queue = new Set([...queue].slice(100));
    if (queue.size) timer = setTimeout(flush, 150);
    if (!ids.length) return;
    try {
      const res = await ctx.api.get('/api/cameras/list', { ids: ids.join(',') });
      for (const c of res.cameras || []) {
        if (!c.missing) put(c.id, { n: c.name, a: (c.subtitle || '').split(', ')[0] || '' });
      }
    } catch { /* hover text stays generic */ }
  }
  return {
    get: (id) => names.get(id) || null,
    put,
    want(id) {
      if (!id || names.has(id) || queue.has(id)) return;
      queue.add(id);
      if (!timer) timer = setTimeout(flush, 120);
    },
  };
}

// ------------------------------------------------------------------ the layer
export function createCameraLayer(ctx, { onPick }) {
  const map = ctx.map;
  let builtAt = null;
  let counts = null;           // per "Show" choice, from the summary
  let retry = null;
  let layer = null;
  let ready = false;
  let sourceUrl = null;        // what the map source was last pointed at
  let warned = false;
  let namesSeq = 0;
  const names = nameCache(ctx);

  const dataUrl = () => ctx.api.url('/api/cameras/geojson', builtAt ? { v: Math.round(builtAt) } : null);
  const showOf = (options) => (SHOW[options?.show] !== undefined ? options.show : 'all');

  function sourceSpec(options) {
    const spec = { type: 'geojson', data: ready ? dataUrl() : ctx.map2.emptyFC(), cluster: true, clusterRadius: 44,
      clusterMaxZoom: 11, clusterMinPoints: 3, buffer: 64, tolerance: 0.5, maxzoom: 14 };
    const filter = SHOW[showOf(options)];
    if (filter) spec.filter = filter;
    return spec;
  }

  function addLayers(options) {
    ctx.map2.addSource(SRC, sourceSpec(options));
    sourceUrl = ready ? dataUrl() : null;
    ctx.map2.addSource(NAMES, { type: 'geojson', data: ctx.map2.emptyFC() });
    ctx.map2.addLayer({ id: L.clusters, type: 'circle', source: SRC, filter: ['has', 'point_count'],
      paint: {
        'circle-color': CAM,
        'circle-opacity': ['interpolate', ['linear'], ['zoom'], 2, 0.2, 10, 0.28],
        'circle-radius': ['step', ['get', 'point_count'], 11, 10, 13.5, 50, 16, 200, 19.5, 1000, 24, 4000, 28, 15000, 32],
        'circle-stroke-color': CAM,
        'circle-stroke-opacity': 0.85,
        'circle-stroke-width': 1.25,
        'circle-pitch-alignment': 'viewport',
      } }, 'points');
    ctx.map2.addLayer({ id: L.count, type: 'symbol', source: SRC, filter: ['has', 'point_count'],
      layout: { 'text-field': ['get', 'point_count_abbreviated'], 'text-font': ['Montserrat SemiBold'],
        'text-size': ['step', ['get', 'point_count'], 11, 200, 12, 1000, 13], 'text-allow-overlap': true, 'text-ignore-placement': false,
        'text-padding': 6 },
      paint: { 'text-color': '#f7f1df', 'text-halo-color': INK, 'text-halo-width': 1.2 } }, 'points');
    ctx.map2.addLayer({ id: L.cones, type: 'symbol', source: SRC, minzoom: 13,
      filter: ['all', ['!', ['has', 'point_count']], ['has', 'h']],
      layout: { 'icon-image': 'cameras-cone', 'icon-rotate': ['get', 'h'], 'icon-rotation-alignment': 'map',
        'icon-pitch-alignment': 'map', 'icon-allow-overlap': true, 'icon-ignore-placement': true,
        'icon-size': ['interpolate', ['linear'], ['zoom'], 13, 0.7, 16, 1.3, 19, 2],
        visibility: options?.cones === false ? 'none' : 'visible' },
      paint: { 'icon-opacity': ['interpolate', ['linear'], ['zoom'], 13, 0, 13.6, 0.9] } }, 'points');
    ctx.map2.addLayer({ id: L.points, type: 'symbol', source: SRC, filter: ['!', ['has', 'point_count']],
      layout: {
        'icon-image': ['case', ['==', ['get', 'k'], 1], 'cameras-video', ['==', ['get', 't'], 1], 'cameras-webcam', 'cameras-still'],
        'icon-size': ['interpolate', ['linear'], ['zoom'], 2, 0.5, 8, 0.66, 12, 0.82, 16, 1],
        'icon-allow-overlap': true, 'icon-ignore-placement': true,
        'symbol-sort-key': ['case', ['==', ['get', 'k'], 1], 2, 1],
      },
      paint: {
        'icon-color': ['match', ['get', 'k'], 1, LIVE, 2, PAGE, CAM],
        'icon-halo-color': INK, 'icon-halo-width': 1.4, 'icon-halo-blur': 0.2,
      } }, 'points');
    ctx.map2.addLayer({ id: L.labels, type: 'symbol', source: NAMES, minzoom: LABEL_ZOOM,
      layout: { 'text-field': ['coalesce', ['get', 'l'], ['get', 'n']], 'text-font': ['Open Sans Regular'], 'text-size': 11.5,
        'text-offset': [0, 1.15], 'text-anchor': 'top', 'text-max-width': 12, 'text-optional': true,
        visibility: options?.labels === false ? 'none' : 'visible' },
      paint: { 'text-color': '#efe3c2', 'text-halo-color': INK, 'text-halo-width': 1.5 } }, 'points');
  }

  function showCount(options) {
    if (!counts) return;
    const n = counts[showOf(options)];
    if (n != null) layer?.setCount(n);
  }

  // a new "Show" choice: the worker re-reads the map file (a 304 from the browser cache) with the
  // new filter and clusters again; without that hook the source is rebuilt
  function applyShow(options) {
    const src = map.getSource(SRC);
    if (!src) return;
    const filter = SHOW[showOf(options)];
    if (src.workerOptions && typeof src.setData === 'function') {
      src.workerOptions.filter = filter || undefined;
      src.setData(ready ? dataUrl() : ctx.map2.emptyFC());
      sourceUrl = ready ? dataUrl() : null;
    } else {
      ctx.map2.removeSource(SRC);
      ctx.map2.removeSource(NAMES);
      addLayers(options);
    }
    showCount(options);
    updateNames();
  }

  function scheduleRetry() {
    clearTimeout(retry);
    retry = setTimeout(() => { if (layer?.isOn()) layer.refresh('retry'); }, 20000);
  }

  // street-zoom names (and hover text) for the cameras in view
  const updateNames = ctx.dom.debounce(async () => {
    if (!layer?.isOn() || !ready || !map.getSource(NAMES)) return;
    const my = ++namesSeq;
    if (map.getZoom() < NAMES_ZOOM) { ctx.map2.setData(NAMES, ctx.map2.emptyFC()); return; }
    const [w, s, e, n] = ctx.map2.viewBounds();
    try {
      const res = await ctx.api.get('/api/cameras/labels', { bbox: `${w},${s},${e},${n}`, limit: 400, show: showOf(layer.options()) });
      if (my !== namesSeq || !map.getSource(NAMES)) return;
      for (const f of res.features || []) {
        const p = f.properties || {};
        names.put(p.id, { n: p.n, a: p.a || '' });
      }
      ctx.map2.setData(NAMES, res);
    } catch { /* names are a nicety */ }
  }, 250);
  ctx.bus.on('map:moveend', () => updateNames());

  layer = ctx.layers.register({
    id: 'cameras', group: 'Cameras', title: 'Public cameras',
    subtitle: 'Traffic cameras and webcams worldwide',
    description: 'Latest images from official public camera feeds and webcams (Windy.com with a key). Click a camera to see it, or open the camera grid from the toolbar.',
    source: 'TfL, Caltrans, IDOT, Iowa DOT, DGT, Fintraffic, HK Transport Department, DriveBC, ALERTCalifornia, Vegagerðin, Alaska DOT&PF, NZTA, York Region, Austin, Calgary, Newfoundland and Labrador, data.gov.sg, USGS, FAA, NOAA and others; webcams provided by Windy.com',
    color: CAM, symbol: 'point', defaultOn: true, refreshMs: 10 * 60 * 1000, viewDependent: false,
    options: [
      { id: 'show', label: 'Show', type: 'select', default: 'all', refresh: false, choices: SHOW_CHOICES },
      { id: 'cones', label: 'Viewing direction (street zoom)', type: 'toggle', default: true, refresh: false },
      { id: 'labels', label: 'Camera names (street zoom)', type: 'toggle', default: true, refresh: false },
    ],
    onOption(id, value, all) {
      if (!layer.isOn()) return;
      if (id === 'show') applyShow(all);
      if (id === 'cones') ctx.map2.setVisible(L.cones, !!value);
      if (id === 'labels') ctx.map2.setVisible(L.labels, !!value);
    },
    legend: [
      { color: CAM, label: 'Still image' },
      { color: LIVE, label: 'Live video' },
      { color: CAM, label: 'Webcam (picture frame glyph)' },
      { color: PAGE, label: 'Opens on its source' },
      { color: 'rgba(239, 227, 194, 0.45)', label: 'Group, click to zoom in' },
    ],
    async enable({ options }) {
      addLayers(options);
    },
    async disable() {
      clearTimeout(retry);
      ctx.map2.removeSource(SRC);
      ctx.map2.removeSource(NAMES);
    },
    async refresh({ signal, reason, options }) {
      let sum;
      try {
        sum = await ctx.api.get('/api/cameras/summary', null, { signal });
      } catch (err) {
        if (err?.name === 'AbortError') throw err;
        throw new Error(err?.message || 'The camera list could not be loaded.');
      }
      counts = sum.show || null;
      if (!sum.ready) {
        layer.setStatus('loading', 'The camera list is being built. This takes a few minutes the first time.');
        scheduleRetry();
        return;
      }
      builtAt = sum.built_at || builtAt;
      ready = true;
      const url = dataUrl();
      if (url !== sourceUrl || reason === 'manual' || reason === 'retry') {
        const src = map.getSource(SRC);
        if (src) { src.setData(url); sourceUrl = url; }
        updateNames();
      }
      showCount(options);
      warnings(sum);
    },
  });

  // the worker could not read the map file (server restarting, list rebuilding): say so and retry
  map.on('error', (e) => {
    if (e?.sourceId !== SRC || !layer.isOn()) return;
    const status = e?.error?.status;
    layer.setStatus(status === 503 ? 'loading' : 'error',
      status === 503 ? 'The camera list is being rebuilt. Trying again shortly.' : 'The camera map could not be loaded. Trying again shortly.');
    scheduleRetry();
  });

  // a source that failed at the last daily update keeps its previous list; say so on the layer row
  function warnings(sum) {
    const bad = (sum.sources || []).filter(s => s.ok === false && !s.optional);
    if (bad.length) layer.setStatus('warn', `${bad.map(s => s.name).join(', ')} ${bad.length > 1 ? 'are' : 'is'} not answering; showing the last good list.`);
    else if (warned) layer.setStatus('ok');
    warned = bad.length > 0;
  }

  // clicks and hover
  ctx.map2.onClick(CLICKABLE, async (f) => {
    const p = f.properties || {};
    if (p.cluster) {
      const src = map.getSource(SRC);
      try {
        const zoom = await src.getClusterExpansionZoom(p.cluster_id);
        map.easeTo({ center: f.geometry.coordinates, zoom: Math.min(zoom + 0.3, 16), duration: ctx.map2.reducedMotion() ? 0 : 650 });
      } catch { map.easeTo({ center: f.geometry.coordinates, zoom: map.getZoom() + 2 }); }
      return;
    }
    const [lon, lat] = f.geometry.coordinates;
    onPick({ kind: 'camera', id: p.id, title: names.get(p.id)?.n || p.n || 'Camera', lat, lon, layer: 'cameras' });
  });
  ctx.map2.onHover(CLICKABLE, (f) => {
    const p = f.properties || {};
    if (p.cluster) return `${ctx.fmt.int(p.point_count)} cameras, click to zoom in`;
    const what = p.k === 1 ? 'live video' : p.k === 2 ? 'opens on its source' : p.t === 1 ? 'webcam' : 'still image';
    const known = names.get(p.id) || (p.n ? { n: p.n, a: p.a } : null);
    if (known) return `${known.n}${known.a ? `, ${known.a}` : ''} (${what})`;
    names.want(p.id);
    return `Camera (${what})`;
  });

  return { layer };
}
