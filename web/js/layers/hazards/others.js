// Smaller hazards layers: tsunami messages (NOAA), natural events (NASA EONET) and disease
// outbreaks (WHO Disease Outbreak News).

import { GROUP, INK, TSUNAMI, EONET, EONET_LABEL, EONET_ICON, OUTBREAK } from './kit.js';

export function registerTsunami(ctx, kit) {
  const { map2, fmt } = ctx;
  const SRC = 'ov-hazards-tsunami';
  const colour = ['match', ['get', 'cat'], ...Object.entries(TSUNAMI).flat(), TSUNAMI.Information];
  const layer = ctx.layers.register({
    id: 'hazards.tsunami', group: GROUP, title: 'Tsunami messages', subtitle: 'NOAA tsunami warning centres',
    description: 'Warnings, watches, advisories and information statements from the US National and Pacific tsunami warning centres.',
    source: 'NOAA tsunami.gov (public domain)', color: TSUNAMI.Information, symbol: 'point',
    defaultOn: false, refreshMs: 300000, viewDependent: false,
    options: [{ id: 'days', label: 'Messages from', type: 'select', default: '7', choices: [
      { value: '1', label: 'The past day' }, { value: '7', label: 'The past week' }, { value: '30', label: 'The past 30 days' }] }],
    legend: ['Warning', 'Watch', 'Advisory', 'Information'].map(k => ({ color: TSUNAMI[k], label: k })),
    async enable() {
      map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
      map2.addLayer({ id: 'ov-hazards-tsunami-ring', type: 'circle', source: SRC,
        paint: { 'circle-radius': ['match', ['get', 'cat'], 'Warning', 22, 'Watch', 19, 'Advisory', 17, 13],
          'circle-color': colour, 'circle-opacity': 0.14, 'circle-stroke-color': colour, 'circle-stroke-width': 1.5,
          'circle-pitch-alignment': 'map' } }, 'points');
      map2.addLayer({ id: 'ov-hazards-tsunami-icon', type: 'symbol', source: SRC,
        layout: { 'icon-image': 'hazards-wave', 'icon-size': ['match', ['get', 'cat'], 'Warning', 0.72, 'Watch', 0.64, 0.52],
          'icon-allow-overlap': true },
        paint: { 'icon-color': colour, 'icon-halo-color': INK, 'icon-halo-width': 1.2 } }, 'points');
    },
    async disable() { map2.removeSource(SRC); },
    async refresh({ signal, options }) {
      const data = await kit.fetchReady('/api/hazards/tsunami', { days: options.days || 7 }, signal, d => d.meta?.source?.pending);
      map2.setData(SRC, data);
      layer.setCount(data.features.length);
      const threats = data.features.filter(f => f.properties.cat !== 'Information').length;
      const st = kit.statusFrom(layer, data.meta, 'No tsunami messages in this period.',
        threats ? `${threats} warnings, watches or advisories` : 'Information statements only: no tsunami threat');
      if (st === 'pending') kit.retryLater(layer);
    },
  });
  const ids = ['ov-hazards-tsunami-ring', 'ov-hazards-tsunami-icon'];
  map2.onClick(ids, (f) => {
    const p = f.properties;
    const [lon, lat] = f.geometry.coordinates;
    ctx.entities.open({ kind: 'tsunami', id: p.id, title: `Tsunami ${String(p.cat).toLowerCase()} message`, lat, lon, layer: 'hazards.tsunami' }, { fly: false });
  });
  map2.onHover(ids, f => `Tsunami ${String(f.properties.cat).toLowerCase()}: ${f.properties.region}, ${fmt.ago(f.properties.t)}`);
  return { layer };
}

export function registerNatural(ctx, kit) {
  const { map2, fmt } = ctx;
  const SRC = 'ov-hazards-natural';
  const colour = ['match', ['get', 'cat'], ...Object.entries(EONET).flat(), '#a7b6ff'];
  const layer = ctx.layers.register({
    id: 'hazards.natural', group: GROUP, title: 'Natural events', subtitle: 'NASA EONET',
    description: 'Open events curated by NASA EONET: storms, volcanoes, icebergs, floods, dust, landslides and recent wildfires.',
    source: 'NASA Earth Observatory Natural Event Tracker (open data)', color: '#86d6f7', symbol: 'point',
    defaultOn: false, refreshMs: 900000, viewDependent: false,
    options: [{ id: 'cats', label: 'Show', type: 'select', default: '', choices: [{ value: '', label: 'Every category' },
      ...Object.entries(EONET_LABEL).map(([value, label]) => ({ value, label }))] }],
    legend: ['severeStorms', 'volcanoes', 'wildfires', 'seaLakeIce', 'floods', 'dustHaze'].map(k => ({ color: EONET[k], label: EONET_LABEL[k] })),
    async enable() {
      map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
      map2.addLayer({ id: 'ov-hazards-natural-track', type: 'line', source: SRC, filter: ['==', ['get', 'role'], 'track'],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': colour, 'line-width': 1.6, 'line-opacity': 0.7, 'line-dasharray': [1, 1.4] } }, 'lines');
      map2.addLayer({ id: 'ov-hazards-natural-disc', type: 'circle', source: SRC, filter: ['==', ['get', 'role'], 'event'],
        paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 1, 6.5, 6, 9.5], 'circle-color': colour,
          'circle-opacity': 0.92, 'circle-stroke-color': INK, 'circle-stroke-width': 1.2, 'circle-pitch-alignment': 'map' } }, 'points');
      map2.addLayer({ id: 'ov-hazards-natural-icon', type: 'symbol', source: SRC, filter: ['==', ['get', 'role'], 'event'],
        layout: { 'icon-image': ['match', ['get', 'cat'], ...Object.entries(EONET_ICON).flat(), 'hazards-dot'],
          'icon-size': ['interpolate', ['linear'], ['zoom'], 1, 0.3, 6, 0.44], 'icon-allow-overlap': true, 'icon-ignore-placement': true,
          'text-field': ['step', ['zoom'], '', 5, ['get', 'title']], 'text-font': ['Open Sans Regular'], 'text-size': 10.5,
          'text-anchor': 'left', 'text-offset': [1.3, 0], 'text-optional': true, 'text-max-width': 14 },
        paint: { 'icon-color': INK, 'text-color': '#ebe6d6', 'text-halo-color': INK, 'text-halo-width': 1.3 } }, 'points');
    },
    async disable() { map2.removeSource(SRC); },
    async refresh({ signal, options }) {
      const data = await kit.fetchReady('/api/hazards/natural', { cats: options.cats || '' }, signal, d => d.meta?.source?.pending);
      map2.setData(SRC, data);
      layer.setCount(data.meta?.count ?? 0);
      const st = kit.statusFrom(layer, data.meta, 'No open events in this category.', data.meta?.source?.note || null);
      if (st === 'pending') kit.retryLater(layer);
    },
  });
  const ids = ['ov-hazards-natural-disc', 'ov-hazards-natural-icon'];
  map2.onClick(ids, (f) => {
    const p = f.properties;
    const [lon, lat] = f.geometry.coordinates;
    ctx.entities.open({ kind: p.kind, id: p.id, title: p.title, lat, lon, layer: 'hazards.natural' }, { fly: false });
  });
  map2.onHover(ids, (f) => {
    const p = f.properties;
    const mag = p.mag != null && p.mag !== 'null' ? `, ${fmt.num(p.mag, 1)} ${p.unit}` : '';
    return `${p.title}${mag}, ${fmt.ago(p.t)}`;
  });
  return { layer };
}

export function registerOutbreaks(ctx, kit) {
  const { map2, fmt } = ctx;
  const SRC = 'ov-hazards-outbreaks';
  const layer = ctx.layers.register({
    id: 'hazards.outbreaks', group: GROUP, title: 'Disease outbreaks', subtitle: 'WHO Disease Outbreak News',
    description: 'Outbreaks reported by the World Health Organization, placed in the countries each report names. Brighter means more recent.',
    source: 'World Health Organization (non-commercial reuse with credit)', color: OUTBREAK, symbol: 'point',
    defaultOn: false, refreshMs: 3600000, viewDependent: false,
    options: [{ id: 'days', label: 'Reports from', type: 'select', default: '60', choices: [
      { value: '30', label: 'The past 30 days' }, { value: '60', label: 'The past 60 days' }, { value: '180', label: 'The past 6 months' }] }],
    legend: [{ color: OUTBREAK, label: 'Outbreak report' }, { color: OUTBREAK, label: 'Countries named', shape: 'area' }],
    async enable() {
      map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
      map2.addLayer({ id: 'ov-hazards-outbreaks-area', type: 'fill', source: SRC, filter: ['==', ['get', 'role'], 'area'],
        paint: { 'fill-color': OUTBREAK, 'fill-opacity': ['interpolate', ['linear'], ['get', 'age'], 0, 0.13, 60, 0.04] } }, 'polygons');
      map2.addLayer({ id: 'ov-hazards-outbreaks-area-line', type: 'line', source: SRC, filter: ['==', ['get', 'role'], 'area'],
        paint: { 'line-color': OUTBREAK, 'line-width': 0.8, 'line-opacity': ['interpolate', ['linear'], ['get', 'age'], 0, 0.5, 60, 0.2],
          'line-dasharray': [2, 2] } }, 'lines');
      map2.addLayer({ id: 'ov-hazards-outbreaks-disc', type: 'circle', source: SRC, filter: ['==', ['get', 'role'], 'report'],
        layout: { 'circle-sort-key': ['-', 0, ['get', 'age']] },
        paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 1, 8, 6, 12],
          'circle-color': OUTBREAK, 'circle-opacity': ['interpolate', ['linear'], ['get', 'age'], 0, 0.95, 60, 0.45],
          'circle-stroke-color': INK, 'circle-stroke-width': 1.3, 'circle-pitch-alignment': 'map' } }, 'points');
      map2.addLayer({ id: 'ov-hazards-outbreaks-icon', type: 'symbol', source: SRC, filter: ['==', ['get', 'role'], 'report'],
        layout: { 'icon-image': 'hazards-outbreak', 'icon-size': ['interpolate', ['linear'], ['zoom'], 1, 0.4, 6, 0.58],
          'icon-allow-overlap': true, 'icon-ignore-placement': true,
          'text-field': ['step', ['zoom'], '', 3, ['get', 'disease']], 'text-font': ['Open Sans Regular'], 'text-size': 10.5,
          'text-anchor': 'top', 'text-offset': [0, 1.3], 'text-optional': true, 'text-max-width': 12 },
        paint: { 'icon-color': INK, 'text-color': '#f3d6ef', 'text-halo-color': INK, 'text-halo-width': 1.3 } }, 'points');
    },
    async disable() { map2.removeSource(SRC); },
    async refresh({ signal, options }) {
      const data = await kit.fetchReady('/api/hazards/outbreaks', { days: options.days || 60 }, signal, d => d.meta?.source?.pending);
      // spread reports that share a country so each stays clickable
      const seen = new Map();
      for (const f of data.features) {
        if (f.properties.role !== 'report') continue;
        const k = f.geometry.coordinates.join(',');
        const n = seen.get(k) || 0;
        seen.set(k, n + 1);
        if (n) {
          const a = n * 2.3;
          f.geometry.coordinates = [f.geometry.coordinates[0] + Math.cos(a) * 1.6 * Math.ceil(n / 3), f.geometry.coordinates[1] + Math.sin(a) * 1.2 * Math.ceil(n / 3)];
        }
      }
      map2.setData(SRC, data);
      layer.setCount(data.meta?.count ?? 0);
      const st = kit.statusFrom(layer, data.meta, 'No outbreak reports in this period.', `${data.meta?.count ?? 0} outbreaks reported`);
      if (st === 'pending') kit.retryLater(layer);
    },
  });
  const ids = ['ov-hazards-outbreaks-disc', 'ov-hazards-outbreaks-icon', 'ov-hazards-outbreaks-area'];
  map2.onClick(ids, (f, lngLat) => {
    const p = f.properties;
    const pt = f.geometry.type === 'Point' ? f.geometry.coordinates : [lngLat.lng, lngLat.lat];
    ctx.entities.open({ kind: 'outbreak', id: p.id, title: p.disease, lat: pt[1], lon: pt[0], layer: 'hazards.outbreaks' }, { fly: false });
  });
  map2.onHover(['ov-hazards-outbreaks-disc', 'ov-hazards-outbreaks-icon'], (f) => `${f.properties.disease}, ${f.properties.place}, reported ${fmt.ago(f.properties.t)}`);
  return { layer };
}
