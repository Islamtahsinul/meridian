// Ports (World Port Index, or UN/LOCODE with Natural Earth sizes). Cards show the port's facts and,
// when the ships module runs, live vessel activity around it.

import { INFRA, INK } from './shared.js';

const SRC = 'ov-infra-ports';
const L_DOT = 'ov-infra-ports-c';
const L_LBL = 'ov-infra-ports-l';
export const PORT = '#5cc8c2';
const SIZE_LABEL = { 4: 'Large', 3: 'Medium', 2: 'Small', 1: 'Very small', 0: 'Size unknown' };

export function registerPorts(ctx, s) {
  const { h, fmt, map2 } = ctx;

  function filterFor(opts) {
    const min = +opts.size || 0;
    return ['step', ['zoom'], ['>=', ['get', 'z'], Math.max(min, 3)], 3.5, ['>=', ['get', 'z'], Math.max(min, 2)],
      5.5, ['>=', ['get', 'z'], min]];
  }

  async function show() {
    map2.addSource(SRC, { type: 'geojson', data: s.geoUrl('ports') });
    map2.addLayer({ id: L_DOT, type: 'circle', source: SRC, filter: filterFor(layer.options()), layout: { 'circle-sort-key': ['get', 'z'] }, paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'],
        2, ['match', ['get', 'z'], 4, 2.6, 3, 1.9, 2, 1.5, 1.2],
        6, ['match', ['get', 'z'], 4, 5.2, 3, 4, 2, 3, 2.2],
        11, ['match', ['get', 'z'], 4, 7.5, 3, 6, 2, 4.6, 3.6]],
      'circle-color': ['match', ['get', 'z'], 4, INK, 3, INK, 'rgba(92, 200, 194, 0.85)'],
      'circle-stroke-color': ['match', ['get', 'z'], 4, PORT, 3, PORT, INK],
      'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 2, ['match', ['get', 'z'], 4, 1.3, 3, 1, 0.5],
        6, ['match', ['get', 'z'], 4, 2, 3, 1.6, 0.6]] } }, 'points');
    map2.addLayer({ id: L_LBL, type: 'symbol', source: SRC, minzoom: 5,
      filter: ['step', ['zoom'], ['>=', ['get', 'z'], 4], 6.5, ['>=', ['get', 'z'], 3], 9, ['>=', ['get', 'z'], 0]],
      layout: { 'text-field': ['get', 'n'], 'text-font': ['Noto Sans Regular'], 'text-size': 11,
        'text-variable-anchor': ['top', 'bottom', 'left', 'right'], 'text-radial-offset': 0.8, 'text-justify': 'auto',
        'text-max-width': 9, 'text-optional': true, 'symbol-sort-key': ['-', 10, ['get', 'z']] },
      paint: { 'text-color': '#bfe9e5', 'text-halo-color': INK, 'text-halo-width': 1.4 } }, 'points');
    const st = await s.status();
    layer.setCount(st.counts?.ports ?? null);
  }

  const layer = ctx.layers.register({
    id: 'infra.ports', group: 'Infrastructure', title: 'Ports', subtitle: 'UN/LOCODE and Natural Earth',
    description: 'Sea and river ports. Major ports are rings; the rest appear as you zoom in. Cards show live vessel activity when the ships layer is available.',
    source: 'UN/LOCODE (UNECE), Natural Earth; NGA World Port Index when reachable', color: PORT, symbol: 'point',
    options: [{ id: 'size', label: 'Show', type: 'select', default: '0', choices: [
      { value: '0', label: 'All ports' }, { value: '2', label: 'Sized ports only' }, { value: '3', label: 'Medium and large' }, { value: '4', label: 'Large only' }] }],
    onOption(_id, _v, all) { if (ctx.map.getLayer(L_DOT)) ctx.map.setFilter(L_DOT, filterFor(all)); },
    legend: [{ color: PORT, label: 'Large or medium port (ring)' }, { color: 'rgba(92,200,194,.85)', label: 'Other port' }],
    async enable() { if (await s.ensureReady('ports', layer, show)) await show(); },
    disable() { map2.removeSource(SRC); },
  });

  map2.onClick([L_DOT], (f) => {
    const [lon, lat] = f.geometry.coordinates;
    openPort({ kind: 'port', id: f.properties.id, title: f.properties.n, lat, lon, layer: 'infra.ports' });
  });
  map2.onHover([L_DOT], (f) => `${f.properties.n}${f.properties.z ? `, ${SIZE_LABEL[f.properties.z].toLowerCase()} port` : ''}`);

  function activityEl(act, mooredInBlock = false) {
    if (act === undefined) return null;
    if (act === null) return h('p', { class: 'dim' }, 'Counting vessels around the port…');
    if (act.error) return h('p', { class: 'dim' }, act.error);
    // Without AIS coverage the counts are all zero, which would read as an empty port.
    if (act.coverage_ok === false) return h('p', { class: 'dim' }, act.coverage || 'No live AIS coverage around this port.');
    // The moored count is left out here when the data block already shows it.
    const cells = [mooredInBlock ? null : ['Moored', act.moored], ['At anchor', act.anchored], ['Under way', act.underway],
      ['Arrivals 24 h', act.arrivals_24h], ['Departures 24 h', act.departures_24h]].filter((c) => c && c[1] != null);
    const grid = h('div', { class: 'infra-stats' }, cells.map(([k, v]) => h('div', null, h('b', null, fmt.int(v)), h('span', null, k))));
    const ul = h('ul', { class: 'card-list' });
    for (const v of (act.vessels || []).slice(0, 8)) {
      const b = h('button', { type: 'button' }, h('span', { class: 'li-t' }, v.title || v.id), v.subtitle ? h('span', { class: 'li-s' }, v.subtitle) : null);
      b.addEventListener('click', () => ctx.entities.open(v));
      ul.append(h('li', null, b));
    }
    return h('div', null, grid, act.vessels?.length ? ul : null, act.coverage ? h('p', { class: 'faint' }, act.coverage) : null);
  }

  function spec(p, act) {
    const size = p.size ? `${p.size[0].toUpperCase()}${p.size.slice(1)}` : null;
    const shipsOn = ctx.moduleOk('ships');
    const facilities = (p.facilities || []).join(', ');
    const block = [
      { label: 'Size', value: size || 'Unknown' },
      p.locode ? { label: 'UN/LOCODE', value: p.locode } : (p.wpi ? { label: 'WPI number', value: p.wpi } : null),
      p.channel_depth_m != null ? { label: 'Channel depth', value: fmt.num(p.channel_depth_m, 1), unit: 'm' } : null,
      p.max_vessel_length_m != null ? { label: 'Longest ship', value: fmt.int(p.max_vessel_length_m), unit: 'm' } : null,
      act && !act.error && act.coverage_ok !== false && act.moored != null ? { label: 'Moored now', value: fmt.int(act.moored) } : null,
    ].filter(Boolean).slice(0, 4);
    return {
      key: `port:${p.id}`, kind: 'port', title: p.name, subtitle: s.placeLine(size ? `${size} port` : 'Port', p.country_name),
      icon: '⚓︎', accent: INFRA,
      badges: [p.source === 'World Port Index' ? { text: 'World Port Index', tone: 'blue' } : { text: 'UN/LOCODE', tone: 'grey' },
        p.harbour_type ? { text: p.harbour_type, tone: 'grey' } : null],
      block,
      sections: [
        shipsOn ? { title: 'Vessel activity', el: activityEl(act, block.some((b) => b.label === 'Moored now')) } : null,
        { title: 'Port', rows: [['Country', p.country_name], ['Region', p.region], ['Water body', p.water_body],
          ['Alternative name', p.alt_name], ['Harbour type', p.harbour_type], ['Harbour use', p.harbour_use], ['Shelter', p.shelter],
          ['Cargo pier depth', p.cargo_pier_depth_m != null ? `${fmt.num(p.cargo_pier_depth_m, 1)} m` : null],
          ['Largest draft', p.max_vessel_draft_m != null ? `${fmt.num(p.max_vessel_draft_m, 1)} m` : null],
          ['Tidal range', p.tidal_range_m != null ? `${fmt.num(p.tidal_range_m, 1)} m` : null],
          ['Facilities', facilities], ['Functions', (p.functions || []).join(', ')], ['Code status', p.status],
          ['Airport code', p.iata], ['Position', `${fmt.latlon(p.lat, p.lon)}${p.position_note ? ` (${p.position_note.charAt(0).toLowerCase()}${p.position_note.slice(1)})` : ''}`]] },
        { title: 'Nearby', nearby: { lat: p.lat, lon: p.lon, radius_km: 25, exclude: { kind: 'port', id: p.id } } },
      ],
      links: [p.locode ? { text: 'UN/LOCODE country list', href: `https://service.unece.org/trade/locode/${(p.country || '').toLowerCase()}.htm` } : null].filter(Boolean),
      entity: { ...p.entity, layer: 'infra.ports' },
      foot: p.source === 'World Port Index' ? 'NGA World Port Index (public domain).'
        : 'UN/LOCODE (UNECE); sizes and precise positions from Natural Earth where matched.',
    };
  }

  async function loadActivity(p) {
    try {
      return await ctx.api.get('/api/ships/port-activity', { lat: p.lat, lon: p.lon, radius_km: p.size === 'large' ? 15 : 8 });
    } catch { return { error: 'Vessel activity is unavailable right now.' }; }
  }

  async function openPort(entity) {
    let p;
    try { p = await s.get(s.entityPath('port', entity.id)); } catch (err) { return s.missingCard(entity, 'port', INFRA, err); }
    let act = ctx.moduleOk('ships') ? null : undefined;
    const card = ctx.cards.open({ ...spec(p, act),
      live: act === undefined ? null : { everyMs: 60000, update: async () => { act = await loadActivity(p); return spec(p, act); } } });
    if (act === null) loadActivity(p).then((a) => { act = a; if (card.isOpen()) card.update(spec(p, act)); });
    return card;
  }

  ctx.entities.handler('port', openPort, { group: 'Infrastructure' });
  return { openPort };
}
