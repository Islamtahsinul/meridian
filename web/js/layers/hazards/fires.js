// Active fires (NASA FIRMS). Low zoom: grid clusters sized by detections and coloured by the
// hottest detection. Closer in: a heat glow plus single detections. If FIRMS is unreachable the
// layer falls back to NASA EONET wildfire events and says so.

import { GROUP, INK, FRP_RAMP } from './kit.js';

const SRC = 'ov-hazards-fires';
const CLICKABLE = ['ov-hazards-fires-cells', 'ov-hazards-fires-pts', 'ov-hazards-fires-events'];

export function registerFires(ctx, kit) {
  const { map2, fmt } = ctx;
  let lastMode = null;

  function addLayers() {
    map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
    // grid clusters
    map2.addLayer({ id: 'ov-hazards-fires-cells-glow', type: 'circle', source: SRC, filter: ['==', ['get', 'kind'], 'cell'],
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['ln', ['+', 1, ['get', 'n']]], 0, 4, 3, 9, 6, 20, 8, 30],
        'circle-color': FRP_RAMP, 'circle-opacity': 0.22, 'circle-blur': 0.9, 'circle-pitch-alignment': 'map',
      } }, 'polygons');
    map2.addLayer({ id: 'ov-hazards-fires-cells', type: 'circle', source: SRC, filter: ['==', ['get', 'kind'], 'cell'],
      layout: { 'circle-sort-key': ['get', 'n'] },
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['ln', ['+', 1, ['get', 'n']]], 0, 1.8, 3, 3.6, 6, 8, 8, 13],
        'circle-color': FRP_RAMP, 'circle-opacity': 0.88, 'circle-stroke-color': INK, 'circle-stroke-width': 0.6,
        'circle-stroke-opacity': 0.6, 'circle-pitch-alignment': 'map',
      } }, 'polygons');
    map2.addLayer({ id: 'ov-hazards-fires-cells-label', type: 'symbol', source: SRC,
      filter: ['all', ['==', ['get', 'kind'], 'cell'], ['>=', ['get', 'n'], 250]],
      layout: { 'text-field': ['number-format', ['get', 'n'], { locale: 'en-GB' }], 'text-font': ['Open Sans Bold'],
        'text-size': 10.5, 'text-offset': [0, 1.5], 'text-anchor': 'top', 'symbol-sort-key': ['-', 0, ['get', 'n']] },
      paint: { 'text-color': '#ffd9b8', 'text-halo-color': INK, 'text-halo-width': 1.3 } }, 'top');
    // single detections: heat glow fading out as dots take over
    map2.addLayer({ id: 'ov-hazards-fires-heat', type: 'heatmap', source: SRC, filter: ['==', ['get', 'kind'], 'fire'],
      maxzoom: 11,
      paint: {
        'heatmap-weight': ['interpolate', ['linear'], ['get', 'f'], 0, 0.15, 20, 0.5, 100, 1],
        'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], 5, 0.7, 10, 1.4],
        'heatmap-radius': ['interpolate', ['linear'], ['zoom'], 5, 8, 8, 14, 11, 24],
        'heatmap-color': ['interpolate', ['linear'], ['heatmap-density'], 0, 'rgba(255,120,40,0)', 0.2, 'rgba(255,150,60,0.35)',
          0.45, 'rgba(255,110,45,0.6)', 0.7, 'rgba(255,70,50,0.78)', 1, 'rgba(255,225,140,0.92)'],
        'heatmap-opacity': ['interpolate', ['linear'], ['zoom'], 5.5, 0.85, 9, 0.55, 11, 0],
      } }, 'polygons');
    map2.addLayer({ id: 'ov-hazards-fires-pts', type: 'circle', source: SRC, filter: ['==', ['get', 'kind'], 'fire'],
      layout: { 'circle-sort-key': ['get', 'f'] },
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 5.5, ['interpolate', ['linear'], ['get', 'f'], 0, 1.3, 100, 3],
          10, ['interpolate', ['linear'], ['get', 'f'], 0, 3, 100, 6.5], 14, ['interpolate', ['linear'], ['get', 'f'], 0, 5, 100, 10]],
        'circle-color': FRP_RAMP, 'circle-blur': 0.25,
        'circle-opacity': ['interpolate', ['linear'], ['zoom'], 5.5, 0.55, 8, 0.9],
        'circle-stroke-color': INK, 'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 7, 0, 10, 0.8],
        'circle-pitch-alignment': 'map',
      } }, 'polygons');
    // fallback: EONET wildfire events
    map2.addLayer({ id: 'ov-hazards-fires-events', type: 'symbol', source: SRC, filter: ['==', ['get', 'kind'], 'event'],
      layout: { 'icon-image': 'hazards-fire', 'icon-size': ['interpolate', ['linear'], ['zoom'], 1, 0.42, 8, 0.7],
        'icon-allow-overlap': true },
      paint: { 'icon-color': '#ff7b39', 'icon-halo-color': INK, 'icon-halo-width': 1 } }, 'points');
  }

  const layer = ctx.layers.register({
    id: 'hazards.fires', group: GROUP, title: 'Active fires', subtitle: 'NASA FIRMS satellite detections, past 24 hours',
    description: 'Thermal anomalies seen by VIIRS (NOAA-20, Suomi NPP) and MODIS (Terra, Aqua). Grouped at world view, single detections closer in.',
    source: 'NASA FIRMS (open data); fallback NASA EONET', color: '#ff7b39', symbol: 'point',
    defaultOn: false, refreshMs: 600000, viewDependent: true,
    options: [
      { id: 'conf', label: 'Confidence', type: 'select', default: '0', choices: [
        { value: '0', label: 'All detections' }, { value: '1', label: 'Nominal and high' }, { value: '2', label: 'High only' }] },
    ],
    legend: [
      { color: '#ffd166', label: 'Under 10 MW' }, { color: '#ff7b39', label: '40 MW' }, { color: '#ff2d55', label: '600 MW and up' },
    ],
    async enable() { addLayers(); },
    async disable() { map2.removeSource(SRC); lastMode = null; },
    async refresh({ bounds, zoom, signal, options }) {
      const params = { bbox: bounds.join(','), zoom: zoom.toFixed(2), conf: options.conf || 0 };
      const data = await kit.fetchReady('/api/hazards/fires', params, signal, d => d.meta?.mode === 'pending', { tries: 10, delay: 2000 });
      map2.setData(SRC, data);
      const m = data.meta || {};
      if (m.mode === 'pending') { layer.setStatus('warn', 'Still downloading the FIRMS fire files.'); kit.retryLater(layer, 8000); return; }
      lastMode = m.mode;
      layer.setCount(m.mode === 'fallback' ? data.features.length : (m.count ?? data.features.length));
      if (m.mode === 'fallback') {
        layer.setStatus('warn', `NASA FIRMS is unavailable (${m.reason || m.source?.error || 'no data'}). Showing NASA EONET wildfire events instead.`);
        return;
      }
      const st = kit.statusFrom(layer, m, 'No fire detections in view.', fireNote(m));
      if (st === 'pending') kit.retryLater(layer, 6000);
    },
  });

  function fireNote(m) {
    const bits = [];
    for (const [key, s] of Object.entries(m.sources || {})) {
      const name = { noaa20: 'NOAA-20', noaa21: 'NOAA-21', snpp: 'Suomi NPP', modis: 'MODIS' }[key] || key;
      if (s.ok === false) bits.push(`${name} unavailable`);
      else if (s.rows === 0) bits.push(`${name} published no detections`);
      else bits.push(`${name} ${fmt.int(s.rows)}`);
    }
    const mode = m.mode === 'grid' ? `Grouped in ${m.cell}° cells` : m.mode === 'points' && m.shown < m.count ? `Showing the ${fmt.int(m.shown)} strongest of ${fmt.int(m.count)}` : null;
    const note = [mode, bits.join(', '), m.newest ? `Newest detection ${fmt.ago(m.newest)}` : null].filter(Boolean).join('. ');
    return note ? `${note}.` : '';
  }

  map2.onClick(CLICKABLE, (f) => {
    const p = f.properties;
    if (p.kind === 'cell') {
      const s = +p.size;
      ctx.map2.fitBbox([+p.w, +p.so, +p.w + s, +p.so + s], { maxZoom: Math.min(12, ctx.map.getZoom() + 3) });
      return;
    }
    const [lon, lat] = f.geometry.coordinates;
    if (p.kind === 'event') {
      ctx.entities.open({ kind: 'fire', id: p.id, title: p.title, lat, lon, layer: 'hazards.fires' }, { fly: false });
      return;
    }
    ctx.entities.open({ kind: 'fire', id: p.id, title: 'Fire detection', lat, lon, layer: 'hazards.fires' }, { fly: false });
  });
  map2.onHover(CLICKABLE, (f) => {
    const p = f.properties;
    if (p.kind === 'cell') return `${fmt.int(p.n)} detection${+p.n === 1 ? '' : 's'}, strongest ${fmt.num(p.f, 0)} MW, latest ${fmt.ago(p.t)}. Click to zoom in`;
    if (p.kind === 'event') return `${p.title}, ${fmt.ago(p.t)}`;
    return `Fire ${fmt.num(p.f, 1)} MW, ${fmt.ago(p.t)}${+p.nt ? ', at night' : ''}`;
  });
  return { layer, mode: () => lastMode };
}
