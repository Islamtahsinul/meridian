// Map layers of the ships module: live vessels, trails, dark-ship estimates, sanctioned rings, straits.

import {
  AMBER, CLASSES, GROUP_COLOR, INK, PAPER, RED, SIZE_FACTOR, F_SANCTIONED, F_STALE, F_SUSPECT, F_CLASS_B,
  classAt, circlePolygon, hours, iconName, padBounds,
} from './common.js';

const ZOOM_ICON = (k) => ['interpolate', ['linear'], ['zoom'], 2, ['*', 0.36 * k, ['get', 'sf']], 6, ['*', 0.52 * k, ['get', 'sf']],
  10, ['*', 0.78 * k, ['get', 'sf']], 14, ['*', 1.05 * k, ['get', 'sf']]];

// ------------------------------------------------------------------ live vessels
export function liveLayer(ctx, { onOpen }) {
  const { map2, api, layers } = ctx;
  const SRC = 'ov-ships-live';
  const IDS = { still: 'ov-ships-live-still', move: 'ov-ships-live-move', label: 'ov-ships-live-label' };
  let last = null;

  function toFeatures(res) {
    const feats = [];
    for (const r of res.rows) {
      const [mmsi, lon, lat, dir, cls, flags, sog10, size, name, age, n] = r;
      const c = classAt(cls);
      feats.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [lon, lat] }, properties: {
        mmsi: String(mmsi), dir, mv: dir >= 0 ? 1 : 0, cls: c.id, c: c.color, ic: iconName(c.id),
        sf: SIZE_FACTOR[size] ?? 0.9, fl: flags, sog: sog10 == null ? null : sog10 / 10, name: name || '', age, n: n || 1,
        op: flags & F_SUSPECT ? 0.35 : flags & F_STALE ? 0.5 : 1, rank: (size || 0) * 10 + (dir >= 0 ? 5 : 0),
      } });
    }
    return { type: 'FeatureCollection', features: feats };
  }

  function addLayers(options) {
    map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
    map2.addLayer({ id: IDS.still, type: 'circle', source: SRC, filter: ['==', ['get', 'mv'], 0], paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 2, ['+', 1.4, ['min', 2.2, ['*', 0.35, ['ln', ['get', 'n']]]]],
        6, ['+', 2.2, ['min', 2.2, ['*', 0.35, ['ln', ['get', 'n']]]]], 10, 3.6, 14, 5.5],
      'circle-color': ['get', 'c'], 'circle-opacity': ['get', 'op'],
      'circle-stroke-color': INK, 'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 3, 0.4, 10, 1],
      'circle-stroke-opacity': ['get', 'op'], 'circle-pitch-alignment': 'map',
    } }, 'points');
    map2.addLayer({ id: IDS.move, type: 'symbol', source: SRC, filter: ['==', ['get', 'mv'], 1], layout: {
      'icon-image': ['get', 'ic'],
      'icon-size': ZOOM_ICON(1), 'icon-rotate': ['get', 'dir'], 'icon-rotation-alignment': 'map', 'icon-pitch-alignment': 'map',
      'icon-allow-overlap': true, 'icon-ignore-placement': true, 'symbol-sort-key': ['get', 'rank'],
    }, paint: { 'icon-opacity': ['get', 'op'] } }, 'moving');
    map2.addLayer({ id: IDS.label, type: 'symbol', source: SRC, minzoom: 9, filter: ['!=', ['get', 'name'], ''], layout: {
      'text-field': ['get', 'name'], 'text-font': ['Open Sans Regular'], 'text-size': ['interpolate', ['linear'], ['zoom'], 9, 10.5, 14, 12.5],
      'text-offset': [0, 1.15], 'text-anchor': 'top', 'text-optional': true, 'symbol-sort-key': ['-', 0, ['get', 'rank']],
      'text-max-width': 9, 'text-padding': 3, visibility: options.labels === false ? 'none' : 'visible',
    }, paint: { 'text-color': PAPER, 'text-halo-color': INK, 'text-halo-width': 1.4, 'text-opacity': ['get', 'op'] } }, 'top');
  }

  map2.onClick([IDS.move, IDS.still, IDS.label], (f) => {
    const p = f.properties;
    const [lon, lat] = f.geometry.coordinates;
    onOpen({ kind: 'ship', id: p.mmsi, title: p.name || `MMSI ${p.mmsi}`, lat, lon, layer: 'ships' });
  });
  map2.onHover([IDS.move, IDS.still, IDS.label], (f) => {
    const p = f.properties;
    const c = CLASSES.find(x => x.id === p.cls);
    const bits = [p.name || `MMSI ${p.mmsi}`, c ? (c.short || c.label) : 'Vessel'];
    if (p.mv && p.sog != null) bits.push(`${ctx.fmt.num(p.sog, 1)} kn`);
    else bits.push('stationary');
    if (p.fl & F_SANCTIONED) bits.push('sanctioned');
    if (p.fl & F_SUSPECT) bits.push('position suspect');
    else if (p.fl & F_STALE) bits.push(`last report ${ctx.fmt.ago(Date.now() / 1000 - p.age)}`);
    if (p.n > 1) bits.push(`${ctx.fmt.int(p.n)} vessels here`);
    return bits.join(', ');
  });

  const typeChoices = [{ value: 'all', label: 'All vessels' }, { value: 'cargo,tanker', label: 'Cargo and tankers' }]
    .concat(CLASSES.map(c => ({ value: c.id, label: c.short || c.label })));

  const handle = layers.register({
    id: 'ships', group: 'Ships', title: 'Vessels', subtitle: 'Live AIS: Digitraffic without a key, aisstream.io with one',
    description: 'Ships reporting their position by AIS. Arrows show moving vessels by type and heading; dots are stationary. Faded symbols have not reported for a while.',
    source: 'Fintraffic Digitraffic (CC BY 4.0); aisstream.io (with key); Kystverket (NLOD, optional)',
    color: GROUP_COLOR, symbol: 'icon', defaultOn: true, refreshMs: 30000, viewDependent: true,
    legend: CLASSES.map(c => ({ color: c.color, label: c.label, shape: c.shape === 'hull' ? 'ships-hull' : 'ships-dart' }))
      .concat([{ color: '#aab4ca', label: 'Stationary', shape: '' }]),
    options: [
      { id: 'types', label: 'Show', type: 'select', choices: typeChoices, default: 'all' },
      { id: 'moving', label: 'Moving vessels only', type: 'toggle', default: false },
      { id: 'labels', label: 'Names from zoom 9', type: 'toggle', default: true, refresh: false },
    ],
    onOption(id, value) {
      if (id === 'labels') map2.setVisible(IDS.label, !!value);
    },
    async enable({ options }) { addLayers(options); },
    async disable() { map2.removeSource(SRC); last = null; },
    async refresh({ bounds, zoom, signal, options }) {
      const res = await api.get('/api/ships/live', {
        bbox: padBounds(bounds).join(','), zoom: zoom.toFixed(1),
        types: options.types && options.types !== 'all' ? options.types : null, moving: options.moving ? 1 : null,
      }, { signal });
      last = res;
      map2.setData(SRC, toFeatures(res));
      handle.setCount(res.in_view);
      if (res.note) handle.setStatus('warn', res.note);
      else if (res.thinned) handle.setStatus('ok', `Showing ${ctx.fmt.int(res.shown)} of ${ctx.fmt.int(res.in_view)} vessels at this zoom. Zoom in for all of them.`);
      else handle.setStatus('ok', null);
    },
  });
  return { handle, last: () => last, ids: IDS };
}

// ------------------------------------------------------------------ trails
export function trails(ctx) {
  const { map2, api, ui } = ctx;
  const SRC = 'ov-ships-trail';
  const on = new Map();     // mmsi -> feature collection parts

  function render() {
    const feats = [];
    for (const parts of on.values()) feats.push(...parts);
    if (!feats.length) { map2.removeSource(SRC); return; }
    map2.addSource(SRC, { type: 'geojson', data: { type: 'FeatureCollection', features: feats } });
    map2.addLayer({ id: 'ov-ships-trail-casing', type: 'line', source: SRC, filter: ['==', ['geometry-type'], 'LineString'],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': INK, 'line-width': ['interpolate', ['linear'], ['zoom'], 4, 3.2, 12, 5], 'line-opacity': 0.7 } }, 'lines');
    map2.addLayer({ id: 'ov-ships-trail-line', type: 'line', source: SRC, filter: ['==', ['geometry-type'], 'LineString'],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': ['get', 'c'], 'line-width': ['interpolate', ['linear'], ['zoom'], 4, 1.4, 12, 2.6], 'line-opacity': 0.85 } }, 'lines');
    map2.addLayer({ id: 'ov-ships-trail-pts', type: 'circle', source: SRC, minzoom: 8, filter: ['==', ['geometry-type'], 'Point'],
      paint: { 'circle-radius': 2.2, 'circle-color': ['get', 'c'], 'circle-stroke-color': INK, 'circle-stroke-width': 0.8 } }, 'lines');
    map2.setData(SRC, { type: 'FeatureCollection', features: feats });
  }

  map2.onHover(['ov-ships-trail-pts'], (f) => `${ctx.fmt.utc(f.properties.t)}${f.properties.sog != null ? `, ${ctx.fmt.num(f.properties.sog, 1)} kn` : ''}`);

  return {
    isOn: (mmsi) => on.has(String(mmsi)),
    async show(mmsi, color = GROUP_COLOR) {
      const r = await api.get(`/api/ships/trail/${mmsi}`, { hours: 24 });
      if (!r.geometry) { ui.toast('No track yet: Meridian has fewer than two positions for this vessel in the last 24 hours.'); return false; }
      on.set(String(mmsi), [
        { type: 'Feature', geometry: r.geometry, properties: { mmsi: String(mmsi), c: color } },
        ...r.points.map(p => ({ type: 'Feature', geometry: { type: 'Point', coordinates: [p[0], p[1]] }, properties: { mmsi: String(mmsi), c: color, t: p[2], sog: p[3] } })),
      ]);
      render();
      return true;
    },
    hide(mmsi) { on.delete(String(mmsi)); render(); },
    async toggle(mmsi, color) {
      if (on.has(String(mmsi))) { on.delete(String(mmsi)); render(); return false; }
      return this.show(mmsi, color);
    },
  };
}

// ------------------------------------------------------------------ dark ships
export function darkLayer(ctx, { onOpen }) {
  const { map2, api, layers } = ctx;
  const SRC = 'ov-ships-dark';
  let items = [];

  function features(list) {
    const feats = [];
    for (const d of list) {
      const props = { mmsi: d.mmsi, name: d.name || `MMSI ${d.mmsi}`, silent: d.silent_s, radius: d.radius_km };
      feats.push({ type: 'Feature', geometry: circlePolygon(d.est.lat, d.est.lon, d.radius_km), properties: { ...props, part: 'area' } });
      feats.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: [[d.last.lon, d.last.lat], [d.est.lon, d.est.lat]] }, properties: { ...props, part: 'path' } });
      feats.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [d.last.lon, d.last.lat] }, properties: { ...props, part: 'last' } });
      feats.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [d.est.lon, d.est.lat] }, properties: { ...props, part: 'est', label: `${props.name}\nestimated, silent ${hours(d.silent_s)}` } });
    }
    return { type: 'FeatureCollection', features: feats };
  }

  const ids = ['ov-ships-dark-fill', 'ov-ships-dark-ring', 'ov-ships-dark-path', 'ov-ships-dark-last', 'ov-ships-dark-est', 'ov-ships-dark-label'];
  map2.onClick(ids.slice(1), (f) => {
    const d = items.find(x => x.mmsi === f.properties.mmsi);
    onOpen({ kind: 'ship', id: f.properties.mmsi, title: f.properties.name, lat: d?.last.lat, lon: d?.last.lon, layer: 'ships.dark' });
  });
  map2.onHover(ids.slice(1), (f) => `${f.properties.name}: AIS silent for ${hours(f.properties.silent)}, ${f.properties.part === 'last' ? 'last report here' : `estimated position (±${Math.round(f.properties.radius)} km)`}`);

  const handle = layers.register({
    id: 'ships.dark', group: 'Ships', title: 'Dark ships', subtitle: 'Estimated positions of vessels that went quiet',
    description: 'Vessels that stopped transmitting AIS mid-voyage for over 2 hours while other vessels nearby kept reporting. The dashed circle is where each could be now, from its last course and speed; it grows with time.',
    source: 'Derived from the AIS feeds by Meridian', color: AMBER, symbol: 'area', refreshMs: 60000,
    legend: [{ color: AMBER, label: 'Uncertainty circle', shape: 'area' }, { color: AMBER, label: 'Estimated position' },
      { color: 'transparent', label: 'Last report (ring)' }],
    async enable() {
      map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
      map2.addLayer({ id: ids[0], type: 'fill', source: SRC, filter: ['==', ['get', 'part'], 'area'], paint: { 'fill-color': AMBER, 'fill-opacity': 0.07 } }, 'polygons');
      map2.addLayer({ id: ids[1], type: 'line', source: SRC, filter: ['==', ['get', 'part'], 'area'], paint: { 'line-color': AMBER, 'line-width': 1.4, 'line-dasharray': [2, 2], 'line-opacity': 0.9 } }, 'lines');
      map2.addLayer({ id: ids[2], type: 'line', source: SRC, filter: ['==', ['get', 'part'], 'path'], paint: { 'line-color': AMBER, 'line-width': 1.2, 'line-dasharray': [1, 2], 'line-opacity': 0.8 } }, 'lines');
      map2.addLayer({ id: ids[3], type: 'circle', source: SRC, filter: ['==', ['get', 'part'], 'last'], paint: { 'circle-radius': 4.5, 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': AMBER, 'circle-stroke-width': 1.6 } }, 'top');
      map2.addLayer({ id: ids[4], type: 'circle', source: SRC, filter: ['==', ['get', 'part'], 'est'], paint: { 'circle-radius': 3.5, 'circle-color': AMBER, 'circle-stroke-color': INK, 'circle-stroke-width': 1 } }, 'top');
      map2.addLayer({ id: ids[5], type: 'symbol', source: SRC, minzoom: 4, filter: ['==', ['get', 'part'], 'est'], layout: {
        'text-field': ['get', 'label'], 'text-font': ['Open Sans Regular'], 'text-size': 11, 'text-offset': [0, 0.9], 'text-anchor': 'top', 'text-max-width': 14,
      }, paint: { 'text-color': '#ffd79a', 'text-halo-color': INK, 'text-halo-width': 1.4 } }, 'top');
    },
    async disable() { map2.removeSource(SRC); },
    async refresh({ signal }) {
      const r = await api.get('/api/ships/dark', null, { signal });
      items = r.dark || [];
      map2.setData(SRC, features(items));
      handle.setCount(items.length);
      handle.setStatus('ok', items.length ? null : 'No dark ships right now. The check needs a few hours of continuous reception first.');
    },
  });
  return { handle, items: () => items };
}

// ------------------------------------------------------------------ sanctioned vessels
export function sanctionedLayer(ctx, { onOpen }) {
  const { map2, api, layers } = ctx;
  const SRC = 'ov-ships-sanc';
  let items = [];
  const ids = ['ov-ships-sanc-ring', 'ov-ships-sanc-label'];
  map2.onClick(ids, (f) => {
    const [lon, lat] = f.geometry.coordinates;
    onOpen({ kind: 'ship', id: f.properties.mmsi, title: f.properties.name, lat, lon, layer: 'ships' });
  });
  map2.onHover(ids, (f) => `${f.properties.name}: ${f.properties.strength === 'confirmed' ? 'on a sanctions list' : 'possible match, not confirmed'}${f.properties.program ? ` (${f.properties.program})` : ''}${f.properties.live ? '' : `, last seen ${ctx.fmt.ago(f.properties.t)}`}`);

  const handle = layers.register({
    id: 'ships.sanctioned', group: 'Ships', title: 'Sanctioned vessels', subtitle: 'OFAC, UK, UN and EU lists matched by IMO or MMSI',
    description: 'Red rings mark live vessels whose IMO or MMSI is on the US Treasury OFAC SDN list, the UK Sanctions List, the UN Security Council list or the EU financial sanctions list. Name-only matches are optional and amber: they are possible matches, never confirmed.',
    source: 'OFAC SDN list (public domain), UK Sanctions List (OGL v3.0), UN Security Council and EU consolidated lists, matched against AIS', color: RED, symbol: 'point', refreshMs: 60000,
    legend: [{ color: RED, label: 'Listed by IMO or MMSI' }, { color: AMBER, label: 'Possible match (name only)' }],
    options: [{ id: 'possible', label: 'Include possible name matches', type: 'toggle', default: false }],
    async enable() {
      map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
      map2.addLayer({ id: ids[0], type: 'circle', source: SRC, paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 2, 4.5, 8, 9, 14, 13], 'circle-color': 'rgba(0,0,0,0)',
        'circle-stroke-color': ['match', ['get', 'strength'], 'confirmed', RED, AMBER],
        'circle-stroke-width': ['match', ['get', 'strength'], 'confirmed', 2.4, 1.5],
        'circle-stroke-opacity': ['case', ['get', 'live'], 1, 0.45], 'circle-pitch-alignment': 'map',
      } }, 'top');
      map2.addLayer({ id: ids[1], type: 'symbol', source: SRC, minzoom: 3, layout: {
        // The vessel layer already labels names from zoom 9; below that the badge names the ship.
        'text-field': ['step', ['zoom'], ['match', ['get', 'strength'], 'confirmed', 'Sanctioned', 'Possible match'], 5,
          ['concat', ['get', 'name'], ['match', ['get', 'strength'], 'confirmed', ', sanctioned', ', possible match']], 9,
          ['match', ['get', 'strength'], 'confirmed', 'Sanctioned', 'Possible match']],
        'text-font': ['Open Sans Bold'], 'text-size': 10.5, 'text-offset': [0, -1.5], 'text-anchor': 'bottom', 'text-optional': true,
      }, paint: { 'text-color': ['match', ['get', 'strength'], 'confirmed', '#ffb3ad', '#ffd79a'], 'text-halo-color': INK, 'text-halo-width': 1.5,
        'text-opacity': ['case', ['get', 'live'], 1, 0.55] } }, 'top');
    },
    async disable() { map2.removeSource(SRC); },
    async refresh({ signal, options }) {
      const r = await api.get('/api/ships/sanctioned', { possible: options.possible ? 1 : null }, { signal });
      items = r.vessels || [];
      map2.setData(SRC, { type: 'FeatureCollection', features: items.map(v => ({ type: 'Feature', geometry: { type: 'Point', coordinates: [v.lon, v.lat] },
        properties: { mmsi: v.mmsi, name: v.name, strength: v.strength, program: v.program, live: !!v.live, t: v.t } })) });
      handle.setCount(items.filter(v => v.live).length);
      if (!r.ready) handle.setStatus('warn', 'The sanctions list is still loading.');
      else handle.setStatus('ok', items.length ? null : 'No listed vessels among the ships Meridian receives right now.');
    },
  });
  return { handle, items: () => items };
}

// ------------------------------------------------------------------ straits
export function straitsLayer(ctx, { onOpen, cache }) {
  const { map2, api, layers } = ctx;
  const SRC = 'ov-ships-straits';
  const LBL = 'ov-ships-straits-lbl';
  const ids = ['ov-ships-straits-fill', 'ov-ships-straits-line', 'ov-ships-straits-label', 'ov-ships-straits-dash', 'ov-ships-straits-pt'];
  map2.onClick(ids, (f) => {
    const s = cache.byId(f.properties.id);
    onOpen({ kind: 'strait', id: `ships:${f.properties.id}`, title: f.properties.name, lat: s?.label[1], lon: s?.label[0], layer: 'ships.straits' });
  });
  map2.onHover(ids, (f) => {
    const p = f.properties;
    return p.cov ? `${p.name}: ${ctx.fmt.int(p.inside)} inside now, ${ctx.fmt.int(p.transits)} transits in 24 h` : `${p.name}: no live coverage`;
  });

  function data(list) {
    const polys = [];
    const labels = [];
    for (const s of list) {
      const props = { id: s.id, name: s.name, inside: s.inside_now, transits: s.transits_24h, cov: s.coverage ? 1 : 0, busy: s.congestion ? 1 : 0 };
      polys.push({ type: 'Feature', geometry: s.geometry, properties: props });
      const short = s.name.replace(/^Strait of /, '').replace(/, Porkkala to Paldiski$/, '');
      labels.push({ type: 'Feature', geometry: { type: 'Point', coordinates: s.label }, properties: { ...props,
        text: s.coverage ? `${short}\n${ctx.fmt.int(s.inside_now)} now, ${ctx.fmt.int(s.transits_24h)} in 24 h` : `${short}\nno coverage` } });
    }
    return { polys: { type: 'FeatureCollection', features: polys }, labels: { type: 'FeatureCollection', features: labels } };
  }

  const handle = layers.register({
    id: 'ships.straits', group: 'Ships', title: 'Straits monitor', subtitle: 'Fourteen chokepoints, now and in the last 24 hours',
    description: 'Vessels inside each strait now and transits in the last 24 hours. Without an aisstream.io key only the Baltic straits (Gulf of Finland, Åland Sea) have live coverage.',
    source: 'Derived from the AIS feeds; outlines drawn for Meridian', color: GROUP_COLOR, symbol: 'area', refreshMs: 60000,
    legend: [{ color: GROUP_COLOR, label: 'Live coverage', shape: 'area' }, { color: '#75839f', label: 'No live coverage', shape: 'area' },
      { color: AMBER, label: 'Busier than usual', shape: 'area' }],
    async enable() {
      map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
      map2.addSource(LBL, { type: 'geojson', data: map2.emptyFC() });
      map2.addLayer({ id: ids[0], type: 'fill', source: SRC, paint: {
        'fill-color': ['case', ['==', ['get', 'busy'], 1], AMBER, ['==', ['get', 'cov'], 1], GROUP_COLOR, '#75839f'],
        'fill-opacity': ['case', ['==', ['get', 'cov'], 1], 0.1, 0.05] } }, 'polygons');
      map2.addLayer({ id: ids[1], type: 'line', source: SRC, filter: ['==', ['get', 'cov'], 1], paint: {
        'line-color': ['case', ['==', ['get', 'busy'], 1], AMBER, GROUP_COLOR],
        'line-width': ['interpolate', ['linear'], ['zoom'], 3, 0.8, 9, 1.6], 'line-opacity': 0.85 } }, 'polygons');
      map2.addLayer({ id: ids[3], type: 'line', source: SRC, filter: ['==', ['get', 'cov'], 0], paint: {
        'line-color': '#8b97b0', 'line-width': ['interpolate', ['linear'], ['zoom'], 3, 0.8, 9, 1.4], 'line-opacity': 0.8,
        'line-dasharray': [3, 2] } }, 'polygons');
      // At world zoom the outlines are a few pixels wide: mark each strait with a small ring instead.
      map2.addLayer({ id: ids[4], type: 'circle', source: LBL, maxzoom: 6, paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 1, 3, 5, 4.5], 'circle-color': INK,
        'circle-stroke-color': ['case', ['==', ['get', 'busy'], 1], AMBER, ['==', ['get', 'cov'], 1], GROUP_COLOR, '#8b97b0'],
        'circle-stroke-width': 1.6, 'circle-opacity': ['interpolate', ['linear'], ['zoom'], 5, 1, 6, 0],
        'circle-stroke-opacity': ['interpolate', ['linear'], ['zoom'], 5, 1, 6, 0] } }, 'points');
      map2.addLayer({ id: ids[2], type: 'symbol', source: LBL, minzoom: 2, layout: {
        'text-field': ['get', 'text'], 'text-font': ['Open Sans Regular'], 'text-size': ['interpolate', ['linear'], ['zoom'], 2, 9.5, 8, 12],
        'text-max-width': 12, 'text-allow-overlap': false, 'text-optional': true, 'symbol-sort-key': ['-', 0, ['get', 'inside']],
        'text-variable-anchor': ['left', 'right', 'top', 'bottom'], 'text-radial-offset': 0.7, 'text-justify': 'auto',
      }, paint: { 'text-color': ['case', ['==', ['get', 'cov'], 1], '#d8ecff', '#aab4ca'], 'text-halo-color': INK, 'text-halo-width': 1.5 } }, 'points');
    },
    async disable() { map2.removeSource(SRC); map2.removeSource(LBL); },
    async refresh({ signal }) {
      const list = await cache.load(signal, true);
      const d = data(list);
      map2.setData(SRC, d.polys);
      map2.setData(LBL, d.labels);
      const live = list.filter(s => s.coverage).length;
      handle.setCount(live ? `${live} live` : 0);
      handle.setStatus(live ? 'ok' : 'warn', live < list.length ? `${list.length - live} of ${list.length} straits have no live coverage. ${cache.note() || ''}`.trim() : null);
    },
  });
  return { handle };
}

export function straitCache(ctx) {
  let list = [];
  let note = null;
  let at = 0;
  let pending = null;
  return {
    async load(signal, force = false) {
      if (!force && list.length && Date.now() - at < 60000) return list;
      if (pending && !force) return pending;
      pending = ctx.api.get('/api/ships/straits', null, { signal }).then((r) => {
        list = r.straits || [];
        note = r.note;
        at = Date.now();
        return list;
      }).finally(() => { pending = null; });
      return pending;
    },
    list: () => list,
    note: () => note,
    byId: (id) => list.find(s => s.id === id) || null,
  };
}

export { F_CLASS_B };
