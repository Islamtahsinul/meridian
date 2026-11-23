// Submarine cables and landing points (TeleGeography Submarine Cable Map, CC BY-NC-SA 3.0).
// Cables keep TeleGeography's colours, widen on hover, and planned cables are dashed. Landing points
// in countries reached by two or fewer cables are flagged in amber.

import { AMBER, INFRA, INK, PAPER } from './shared.js';

const SRC = 'ov-infra-cables';
const SRC_LAND = 'ov-infra-landings';
const L_SEL = 'ov-infra-cables-sel';
const L_LIVE = 'ov-infra-cables-line';
const L_PLAN = 'ov-infra-cables-planned';
const L_HIT = 'ov-infra-cables-hit';
const L_LBL = 'ov-infra-cables-lbl';
const L_LAND = 'ov-infra-landings-c';
const CABLE = '#9fb8d6';

export function registerCables(ctx, s) {
  const { h, fmt, map2, map } = ctx;
  let hovered = null;
  let selected = null;

  const hover = ['boolean', ['feature-state', 'hover'], false];
  const width = (k = 1) => ['interpolate', ['linear'], ['zoom'],
    1, ['case', hover, 3.2, 0.7 * k], 4, ['case', hover, 3.8, 1.15 * k], 8, ['case', hover, 4.6, 1.8 * k], 12, ['case', hover, 6, 2.6 * k]];

  function landingPaint(flag) {
    const low = ['all', ['>=', ['coalesce', ['get', 'd'], 0], 1], ['<=', ['coalesce', ['get', 'd'], 99], 2]];
    return {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 1, 1.4, 4, 2.4, 8, 3.8, 12, 5.2],
      'circle-color': PAPER, 'circle-opacity': 0.95,
      'circle-stroke-color': flag ? ['case', low, AMBER, INK] : INK,
      'circle-stroke-width': flag ? ['interpolate', ['linear'], ['zoom'], 1, ['case', low, 1.3, 0.7], 6, ['case', low, 2.2, 0.9]] : 0.8,
    };
  }

  async function show() {
    const opts = layer.options();
    map2.addSource(SRC, { type: 'geojson', data: s.geoUrl('cables'), promoteId: 'id' });
    map2.addSource(SRC_LAND, { type: 'geojson', data: s.geoUrl('landings') });
    map2.addLayer({ id: L_SEL, type: 'line', source: SRC, filter: ['==', ['get', 'id'], ''], layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': '#ffffff', 'line-opacity': 0.35, 'line-width': ['interpolate', ['linear'], ['zoom'], 1, 5, 8, 9], 'line-blur': 2 } }, 'lines');
    map2.addLayer({ id: L_LIVE, type: 'line', source: SRC, filter: ['!=', ['get', 'p'], 1], layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': ['get', 'c'], 'line-opacity': 0.9, 'line-width': width() } }, 'lines');
    map2.addLayer({ id: L_PLAN, type: 'line', source: SRC, filter: ['==', ['get', 'p'], 1], layout: { visibility: opts.planned === false ? 'none' : 'visible' },
      paint: { 'line-color': ['get', 'c'], 'line-opacity': 0.7, 'line-width': width(0.9), 'line-dasharray': [2, 1.6] } }, 'lines');
    map2.addLayer({ id: L_HIT, type: 'line', source: SRC, filter: opts.planned === false ? ['!=', ['get', 'p'], 1] : true,
      paint: { 'line-color': '#000000', 'line-opacity': 0.01, 'line-width': 10 } }, 'lines');
    map2.addLayer({ id: L_LBL, type: 'symbol', source: SRC, minzoom: 4.5, filter: opts.planned === false ? ['!=', ['get', 'p'], 1] : true,
      layout: { visibility: opts.labels === false ? 'none' : 'visible', 'symbol-placement': 'line', 'symbol-spacing': 420,
        'text-field': ['get', 'n'], 'text-font': ['Noto Sans Regular'], 'text-size': 10.5, 'text-letter-spacing': 0.04,
        'text-max-angle': 30, 'text-optional': true },
      paint: { 'text-color': ['get', 'c'], 'text-halo-color': INK, 'text-halo-width': 1.6 } }, 'lines');
    map2.addLayer({ id: L_LAND, type: 'circle', source: SRC_LAND, minzoom: 1.2, paint: landingPaint(opts.flag !== false) }, 'points');
    const st = await s.status();
    layer.setCount(st.counts?.cables ?? null);
    if (selected) setSelected(selected);
  }

  function setSelected(cid) {
    selected = cid;
    if (map.getLayer(L_SEL)) map.setFilter(L_SEL, ['==', ['get', 'id'], cid || '']);
  }

  const layer = ctx.layers.register({
    id: 'infra.cables', group: 'Infrastructure', title: 'Submarine cables', subtitle: 'TeleGeography Submarine Cable Map',
    description: 'Undersea internet cables in their map colours, with landing points. Amber rings mark countries reached by two or fewer cables.',
    source: 'TeleGeography Submarine Cable Map (CC BY-NC-SA 3.0, non-commercial)', color: CABLE, symbol: 'line',
    options: [
      { id: 'planned', label: 'Planned cables', type: 'toggle', default: true },
      { id: 'labels', label: 'Cable names', type: 'toggle', default: true },
      { id: 'flag', label: 'Flag countries with 2 or fewer cables', type: 'toggle', default: true },
    ],
    onOption(id, value, all) {
      if (!map.getLayer(L_LIVE)) return;
      if (id === 'planned') {
        map2.setVisible(L_PLAN, value);
        const f = value ? true : ['!=', ['get', 'p'], 1];
        map.setFilter(L_HIT, f);
        map.setFilter(L_LBL, f);
      } else if (id === 'labels') map2.setVisible(L_LBL, value);
      else if (id === 'flag') {
        const p = landingPaint(value);
        map.setPaintProperty(L_LAND, 'circle-stroke-color', p['circle-stroke-color']);
        map.setPaintProperty(L_LAND, 'circle-stroke-width', p['circle-stroke-width']);
      }
    },
    legend: [{ color: CABLE, label: 'Cable in service', shape: 'line' }, { color: 'rgba(159,184,214,.6)', label: 'Planned (dashed)', shape: 'line' },
      { color: PAPER, label: 'Landing point' }, { color: AMBER, label: 'Country with 2 or fewer cables' }],
    async enable() { if (await s.ensureReady('cables', layer, show)) await show(); },
    disable() { hovered = null; map2.removeSource(SRC); map2.removeSource(SRC_LAND); },
  });

  // Hover: widen every segment of the cable under the pointer.
  map.on('mousemove', L_HIT, (e) => {
    const id = e.features?.[0]?.properties?.id;
    if (!id || id === hovered) return;
    if (hovered) map.setFeatureState({ source: SRC, id: hovered }, { hover: false });
    hovered = id;
    map.setFeatureState({ source: SRC, id }, { hover: true });
  });
  map.on('mouseleave', L_HIT, () => {
    if (hovered && map.getSource(SRC)) map.setFeatureState({ source: SRC, id: hovered }, { hover: false });
    hovered = null;
  });

  map2.onClick([L_LAND], (f) => {
    const [lon, lat] = f.geometry.coordinates;
    openLanding({ kind: 'landing_point', id: `tg:${f.properties.id}`, title: f.properties.n, lat, lon, layer: 'infra.cables' });
  });
  map2.onClick([L_HIT], (f, lngLat) => openCable({ kind: 'cable', id: `tg:${f.properties.id}`, title: f.properties.n, layer: 'infra.cables' }, lngLat));
  map2.onHover([L_LAND], (f) => `${f.properties.n}, ${f.properties.k} cable${f.properties.k === 1 ? '' : 's'}`);
  map2.onHover([L_HIT], (f) => `${f.properties.n}${f.properties.p === 1 ? ' (planned)' : f.properties.y ? `, ${f.properties.y}` : ''}`);

  // A card list that shows the first few rows and a button for the rest.
  function moreList(rows, render, first = 8, noun = 'items') {
    const ul = h('ul', { class: 'card-list' });
    const draw = (all) => {
      ctx.dom.clear(ul);
      for (const r of all ? rows : rows.slice(0, first)) ul.append(h('li', null, render(r)));
      if (!all && rows.length > first) {
        const more = h('button', { type: 'button', class: 'text-btn infra-more' }, `Show all ${rows.length} ${noun}`);
        more.addEventListener('click', () => draw(true));
        ul.append(h('li', null, more));
      }
    };
    draw(false);
    return ul;
  }

  function countryList(rows) {
    if (!rows?.length) return null;
    return moreList(rows, (c) => [h('span', { class: `li-d${c.low ? ' infra-warn' : ''}` }, `${fmt.int(c.cables)} cable${c.cables === 1 ? '' : 's'}`),
      h('span', { class: 'li-t' }, c.name || c.country), c.low ? h('span', { class: 'li-s infra-warn' }, 'Depends on 2 or fewer cables') : null], 6, 'countries');
  }

  function landingList(rows) {
    if (!rows?.length) return h('p', { class: 'dim' }, 'No landing points listed.');
    return moreList(rows, (p) => {
      const b = h('button', { type: 'button' }, h('span', { class: 'li-t' }, p.name));
      b.addEventListener('click', () => ctx.entities.open({ kind: 'landing_point', id: p.id, title: p.name, lat: p.lat, lon: p.lon, layer: 'infra.cables' }));
      return b;
    }, 8, 'landing points');
  }

  function dependencyTable() {
    const body = h('div', null, h('p', { class: 'dim' }, 'Loading…'));
    const m = ctx.ui.modal({ title: 'Cable dependency by country', body, wide: true });
    s.get('/api/infra/countries', null, { ttlMs: 30 * 60 * 1000 }).then((res) => {
      const rows = res.countries || [];
      const low = rows.filter(r => r.low).length;
      const intel = ctx.moduleOk('intel');
      const tbl = h('table', { class: 'tbl infra-dep' }, h('tr', null, h('th', null, 'Country'), h('th', { class: 'num' }, 'Cables'),
        h('th', { class: 'num' }, 'Planned'), h('th', { class: 'num' }, 'Landings'), h('th', null, 'Dependency'), h('th', null, 'Cables in service')));
      for (const r of rows) {
        const name = intel ? h('button', { class: 'text-btn', type: 'button' }, r.name) : r.name;
        if (intel) name.addEventListener('click', () => { m.close(); ctx.entities.open({ kind: 'country', id: r.country, title: r.name }); });
        const flag = r.low ? h('span', { class: 'badge amber' }, `${r.cables} cable${r.cables === 1 ? '' : 's'} only`)
          : r.cables === 0 ? h('span', { class: 'badge grey' }, 'None in service') : null;
        tbl.append(h('tr', { class: r.low ? 'infra-low' : '' }, h('td', { class: 'infra-dep-name' }, name),
          h('td', { class: 'num' }, fmt.int(r.cables)), h('td', { class: 'num' }, r.planned ? fmt.int(r.planned) : ''),
          h('td', { class: 'num' }, fmt.int(r.landing_points)), h('td', { class: 'infra-dep-flag' }, flag),
          h('td', { class: 'dim', style: { fontSize: '12px' } }, r.cable_names.join(', '))));
      }
      m.setBody(h('div', null, h('p', { class: 'dim' }, `${rows.length} countries and territories with cable landings. ${low} depend on two or fewer cables in service, so one or two cuts could isolate them. Counts are cables, not capacity.`), tbl,
        h('p', { class: 'faint', style: { fontSize: '12px' } }, 'From the TeleGeography Submarine Cable Map (CC BY-NC-SA 3.0).')));
    }).catch(() => m.setBody(h('p', { class: 'empty' }, 'The cable table could not be loaded right now.')));
  }

  function cableSpec(c) {
    const d = c.details || {};
    const n = (c.landing_points || []).length;
    const status = c.is_planned ? 'Planned' : 'In service';
    return {
      key: `cable:${c.id}`, kind: 'cable', title: c.name, subtitle: `Submarine cable${c.rfs_year ? `, ready for service ${c.rfs_year}` : ''}`,
      icon: '∿', accent: INFRA,
      badges: [{ text: status, tone: c.is_planned ? 'blue' : 'green' }, ...(c.countries || []).filter(x => x.low).slice(0, 3).map(x => ({ text: `${x.name}: ${x.cables} cable${x.cables === 1 ? '' : 's'}`, tone: 'amber' }))],
      block: [
        { label: 'Length', value: d.length ? String(d.length).replace(/\s*km$/i, '') : '–', unit: d.length ? 'km' : '' },
        { label: 'Ready', value: c.rfs_year ? String(c.rfs_year) : '–' },
        { label: 'Landing points', value: fmt.int(n) },
        { label: 'Countries', value: fmt.int((c.countries || []).length) },
      ],
      sections: [
        c.details_error ? { text: c.details_error } : null,
        d.owners ? { title: 'Owners', text: d.owners } : null,
        d.suppliers ? { title: 'Built by', text: d.suppliers } : null,
        d.notes ? { title: 'Notes', text: String(d.notes).slice(0, 600) } : null,
        (c.countries || []).length ? { title: 'Countries it reaches, fewest cables first', el: countryList(c.countries) } : null,
        { title: `Landing points (${n})`, el: landingList(c.landing_points || []) },
      ],
      links: [{ text: 'TeleGeography cable page', href: c.page }, d.url ? { text: 'Cable website', href: d.url } : null].filter(Boolean),
      actions: [c.bbox ? { text: 'Show whole cable', onClick: () => map2.fitBbox(c.bbox) } : null, { text: 'Country dependency', onClick: dependencyTable }].filter(Boolean),
      entity: c.entity ? { ...c.entity, layer: 'infra.cables' } : null,
      foot: 'Cable data from TeleGeography’s Submarine Cable Map (CC BY-NC-SA 3.0). Owners and lengths as TeleGeography lists them.',
      onClose() { if (selected === c.tg_id) setSelected(null); },
    };
  }

  async function openCable(entity) {
    let c;
    try { c = await s.get(s.entityPath('cable', entity.id), null, { ttlMs: 30 * 60 * 1000 }); } catch (err) { return s.missingCard(entity, 'cable', INFRA, err); }
    setSelected(c.tg_id);
    return ctx.cards.open(cableSpec(c));
  }

  async function openLanding(entity) {
    let l;
    try { l = await s.get(s.entityPath('landing_point', entity.id)); } catch (err) { return s.missingCard(entity, 'landing point', INFRA, err); }
    const cabs = (l.cables || []).map(c => ({ title: c.name, subtitle: c.is_planned ? `Planned${c.rfs_year ? `, ${c.rfs_year}` : ''}` : (c.rfs_year ? `Since ${c.rfs_year}` : 'In service'),
      entity: { kind: 'cable', id: c.id, title: c.name, layer: 'infra.cables' } }));
    const country = l.country_name || 'This country';
    return ctx.cards.open({
      key: `landing_point:${l.id}`, kind: 'landing_point', title: l.place || l.name, subtitle: s.placeLine('Cable landing point', l.country_name),
      icon: '◉', accent: INFRA,
      badges: [l.low_dependency ? { text: `${country} depends on ${l.country_cables} cable${l.country_cables === 1 ? '' : 's'}`, tone: 'amber' } : null,
        l.is_tbd ? { text: 'Location to be confirmed', tone: 'grey' } : null],
      block: [
        { label: 'Cables here', value: fmt.int((l.cables || []).length) },
        { label: 'Country cables', value: fmt.int(l.country_cables) },
        { label: 'Country landings', value: fmt.int(l.country_landing_points) },
      ],
      sections: [
        { title: 'Cables landing here', list: cabs, empty: 'No cables listed.' },
        { title: 'Country', text: l.country_cables ? `${country} is reached by ${l.country_cables} cable${l.country_cables === 1 ? '' : 's'} in service${l.country_planned ? ` and ${l.country_planned} planned` : ''}, at ${l.country_landing_points} landing point${l.country_landing_points === 1 ? '' : 's'}.${l.low_dependency ? ' One or two cuts could cut it off.' : ''}` : `No cables in service are listed for ${country}.` },
        { title: 'Nearby', nearby: { lat: l.lat, lon: l.lon, radius_km: 25, exclude: { kind: 'landing_point', id: l.id } } },
      ],
      links: [{ text: 'TeleGeography landing page', href: l.page }],
      actions: [{ text: 'Country dependency', onClick: dependencyTable }],
      entity: l.entity ? { ...l.entity, layer: 'infra.cables' } : null,
      foot: 'From TeleGeography’s Submarine Cable Map (CC BY-NC-SA 3.0).',
    });
  }

  ctx.entities.handler('cable', openCable, { group: 'Infrastructure' });
  ctx.entities.handler('landing_point', openLanding, { group: 'Infrastructure' });
  return { openCable, openLanding, dependencyTable, INFRA };
}
