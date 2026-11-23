// Copernicus EMS rapid mapping activations: a framed marker per activation (category glyph,
// solid while open, faded once closed) and the areas of interest it maps. The card lists the map
// products (first estimate, delineation, grading) with download links, links the GDACS event,
// draws the areas of interest and can overlay the mapped flood or burnt area.

import { GROUP, INK, CEMS_COLOR, CEMS_ICON, imageryDates, clip, now } from './kit.js';
import { agoShort } from './cards.js';

const SRC = 'ov-hazards-mapping';
const EXT = 'ov-hazards-mapping-extent';
const CLICKABLE = ['ov-hazards-mapping-frame', 'ov-hazards-mapping-icon', 'ov-hazards-mapping-aoi'];

export function registerMapping(ctx, kit, extras) {
  const { map2, fmt, h } = ctx;
  const colour = ['match', ['get', 'cat'], ...Object.entries(CEMS_COLOR).flat(), CEMS_COLOR.other];
  let extentFor = null;   // card key whose mapped area is drawn

  function addLayers() {
    map2.addSource(SRC, { type: 'geojson', data: map2.emptyFC() });
    map2.addLayer({ id: 'ov-hazards-mapping-aoi', type: 'fill', source: SRC, filter: ['==', ['get', 'role'], 'aoi'],
      paint: { 'fill-color': colour, 'fill-opacity': ['case', ['==', ['get', 'open'], 1], 0.16, 0.07] } }, 'polygons');
    map2.addLayer({ id: 'ov-hazards-mapping-aoi-line', type: 'line', source: SRC, filter: ['==', ['get', 'role'], 'aoi'],
      paint: { 'line-color': colour, 'line-width': ['interpolate', ['linear'], ['zoom'], 4, 0.8, 10, 1.8],
        'line-opacity': ['case', ['==', ['get', 'open'], 1], 0.9, 0.5], 'line-dasharray': [3, 1.5] } }, 'lines');
    map2.addLayer({ id: 'ov-hazards-mapping-frame', type: 'symbol', source: SRC, filter: ['==', ['get', 'role'], 'point'],
      layout: { 'icon-image': 'hazards-frame', 'icon-size': ['interpolate', ['linear'], ['zoom'], 1, ['case', ['==', ['get', 'open'], 1], 0.62, 0.48], 6, ['case', ['==', ['get', 'open'], 1], 0.86, 0.66]],
        'icon-allow-overlap': true, 'icon-ignore-placement': true, 'symbol-sort-key': ['get', 'open'] },
      paint: { 'icon-color': colour, 'icon-opacity': ['case', ['==', ['get', 'open'], 1], 0.97, 0.55], 'icon-halo-color': INK, 'icon-halo-width': 1.2 } }, 'points');
    map2.addLayer({ id: 'ov-hazards-mapping-icon', type: 'symbol', source: SRC, filter: ['==', ['get', 'role'], 'point'],
      layout: { 'icon-image': ['match', ['get', 'cat'], ...Object.entries(CEMS_ICON).flat(), 'hazards-alert'],
        'icon-size': ['interpolate', ['linear'], ['zoom'], 1, ['case', ['==', ['get', 'open'], 1], 0.34, 0.26], 6, ['case', ['==', ['get', 'open'], 1], 0.48, 0.36]],
        'icon-allow-overlap': true, 'icon-ignore-placement': true, 'symbol-sort-key': ['get', 'open'] },
      paint: { 'icon-color': INK, 'icon-opacity': ['case', ['==', ['get', 'open'], 1], 1, 0.7] } }, 'points');
    map2.addLayer({ id: 'ov-hazards-mapping-label', type: 'symbol', source: SRC,
      filter: ['all', ['==', ['get', 'role'], 'point'], ['any', ['==', ['get', 'open'], 1], ['>=', ['zoom'], 5]]],
      layout: { 'text-field': ['get', 'label'], 'text-font': ['Open Sans Regular'], 'text-size': 11, 'text-anchor': 'left',
        'text-offset': [1.3, 0], 'text-optional': true, 'text-max-width': 14, 'symbol-sort-key': ['-', 1, ['get', 'open']] },
      paint: { 'text-color': '#ebe6d6', 'text-halo-color': INK, 'text-halo-width': 1.4, 'text-opacity': ['case', ['==', ['get', 'open'], 1], 1, 0.7] } }, 'top');
  }

  const layer = ctx.layers.register({
    id: 'hazards.mapping', group: GROUP, title: 'Emergency mapping', subtitle: 'Copernicus EMS rapid mapping',
    description: 'Activations of the Copernicus Emergency Management Service: satellite emergency maps of floods, fires, earthquakes, storms and landslides, with the areas they cover. Open activations are solid; closed ones are faded.',
    source: 'Copernicus Emergency Management Service (© European Union), free, full and open access', color: CEMS_COLOR.flood, symbol: 'point',
    defaultOn: false, refreshMs: 1800000, viewDependent: false,
    options: [
      { id: 'scope', label: 'Show', type: 'select', default: 'recent', choices: [
        { value: 'recent', label: 'Open, and closed in the period' }, { value: 'open', label: 'Open activations only' }] },
      { id: 'days', label: 'Period', type: 'select', default: '60', choices: [
        { value: '30', label: 'The past 30 days' }, { value: '60', label: 'The past 60 days' }, { value: '180', label: 'The past 6 months' }] },
    ],
    legend: [{ color: CEMS_COLOR.flood, label: 'Flood' }, { color: CEMS_COLOR.fire, label: 'Wildfire' },
      { color: CEMS_COLOR.earthquake, label: 'Earthquake' }, { color: CEMS_COLOR.storm, label: 'Storm' },
      { color: CEMS_COLOR.landslide, label: 'Mass movement' }, { color: CEMS_COLOR.other, label: 'Area of interest', shape: 'area' }],
    async enable() { addLayers(); },
    async disable() { map2.removeSource(SRC); },
    async refresh({ signal, options }) {
      const data = await kit.fetchReady('/api/hazards/mapping', { days: options.days || 60, scope: options.scope || 'recent' }, signal, d => d.meta?.source?.pending);
      map2.setData(SRC, data);
      const m = data.meta || {};
      layer.setCount(m.count ?? 0);
      const st = kit.statusFrom(layer, m, 'No activations in this period.',
        `${fmt.int(m.open || 0)} open activation${+m.open === 1 ? '' : 's'}, ${fmt.int(m.count || 0)} shown`);
      if (st === 'pending') kit.retryLater(layer);
    },
  });

  map2.onClick(CLICKABLE, (f, lngLat) => {
    const p = f.properties;
    const pt = f.geometry.type === 'Point' ? f.geometry.coordinates : [lngLat.lng, lngLat.lat];
    ctx.entities.open({ kind: 'emergency_mapping', id: p.id, title: p.name, lat: pt[1], lon: pt[0], layer: 'hazards.mapping' }, { fly: false });
  });
  map2.onHover(CLICKABLE, (f) => {
    const p = f.properties;
    if (p.role === 'aoi') return `Area of interest: ${p.name} (${p.code})`;
    return `${p.name}, ${+p.open ? 'open' : 'closed'}, activated ${fmt.ago(+p.t)} (${p.code})`;
  });

  // ------------------------------------------------------------------ mapped area overlay
  function ensureExtent() {
    if (ctx.map.getSource(EXT)) return;
    map2.addSource(EXT, { type: 'geojson', data: map2.emptyFC() });
    map2.addLayer({ id: 'ov-hazards-mapping-extent-fill', type: 'fill', source: EXT,
      paint: { 'fill-color': ['coalesce', ['get', 'color'], CEMS_COLOR.flood], 'fill-opacity': 0.34 } }, 'polygons');
    map2.addLayer({ id: 'ov-hazards-mapping-extent-line', type: 'line', source: EXT,
      paint: { 'line-color': ['coalesce', ['get', 'color'], CEMS_COLOR.flood], 'line-width': 0.8, 'line-opacity': 0.9 } }, 'lines');
  }
  function clearExtent() {
    extentFor = null;
    if (ctx.map.getSource(EXT)) map2.removeSource(EXT);
  }
  ctx.bus.on('card:closed', ({ key }) => { if (key === extentFor) clearExtent(); });

  async function showExtent(card, a, button) {
    if (extentFor === card.key) { clearExtent(); return; }
    ctx.ui.toast('Loading the mapped area from Copernicus EMS…', 'blue');
    let data;
    try {
      data = await ctx.api.get('/api/hazards/mapping/extent', { id: a.id });
    } catch (err) {
      ctx.ui.toast(`The mapped area is unavailable (${err.message || err}).`, 'amber');
      return;
    }
    if (!card.isOpen()) return;
    if (!data.features?.length) { ctx.ui.toast(data.meta?.note || 'No mapped area is published yet.', 'amber'); return; }
    const color = CEMS_COLOR[a.cat] || CEMS_COLOR.other;
    for (const f of data.features) f.properties = { ...(f.properties || {}), color };
    ensureExtent();
    map2.setData(EXT, data);
    extentFor = card.key;
    const what = a.cat === 'fire' ? 'burnt area' : a.cat === 'flood' ? 'flooded area' : 'affected area';
    ctx.ui.toast(`Showing the mapped ${what}${data.meta?.truncated ? ' (simplified, partly shown)' : ''}.`, 'green');
    button?.blur?.();
  }

  // ------------------------------------------------------------------ card
  function productsEl(a) {
    const box = h('div', { class: 'hazards-products' });
    for (const aoi of a.aois || []) {
      box.append(h('h4', { class: 'hazards-sub' }, aoi.name));
      if (!aoi.products?.length) { box.append(h('p', { class: 'dim' }, 'No products requested for this area yet.')); continue; }
      const ul = h('ul', { class: 'hazards-plist' });
      for (const p of aoi.products) {
        const when = p.delivered ? (p.delivered_at ? `delivered ${fmt.utc(p.delivered_at)}` : 'delivered')
          : p.expected ? `${p.status.toLowerCase()}, expected ${fmt.utc(p.expected)}` : p.status.toLowerCase();
        const sats = [...new Set((p.images || []).map(i => i.sensor).filter(Boolean))].join(', ');
        const bits = [when, p.maps ? `${p.maps} map${p.maps === 1 ? '' : 's'}` : null, sats ? `from ${sats}` : null].filter(Boolean).join(', ');
        const li = h('li', null, h('span', { class: 'hazards-ptype' }, p.label), h('span', { class: 'hazards-pmeta' }, bits));
        if (p.download) li.append(h('a', { href: p.download, class: 'hazards-plink' }, 'Download'));
        ul.append(li);
        const top = (p.stats || []).find(s => s.affected != null && /flood|burnt|burned|affected|extent/i.test(s.label));
        if (top) ul.append(h('li', { class: 'hazards-pstat' }, `${top.label}: ${fmt.num(top.affected, top.affected >= 100 ? 0 : 1)} ${top.unit || ''}`.trim()));
      }
      box.append(ul);
    }
    return box;
  }

  function cemsCard(d, entity) {
    const a = d.data;
    const open = !!a.open;
    const badges = [open ? { text: 'Open', tone: 'amber' } : { text: 'Closed', tone: 'grey' }, { text: a.category, tone: 'grey' }];
    if (a.charter_number) badges.push({ text: `Charter activation ${a.charter_number}`, tone: 'blue', title: 'The International Charter Space and Major Disasters was activated too' });
    const linked = (a.gdacs || []).find(g => g.match === 'linked');
    if (linked?.alert) badges.push({ text: `GDACS ${linked.alert.toLowerCase()}`, tone: { Green: 'green', Orange: 'amber', Red: 'red' }[linked.alert] || 'grey' });
    if (a.sensitive) badges.push({ text: 'Areas not published', tone: 'grey', title: 'Copernicus marks this activation as sensitive' });
    const countries = (a.countries || []).join(', ');
    const sections = [];
    if (a.reason) sections.push({ el: kit.expandable(a.reason, 520) });
    sections.push({ rows: [
      ['Event', a.event_time ? kit.when(a.event_time) : null], ['Activated', kit.when(a.activation_time)],
      ['Last update', a.last_update ? kit.when(a.last_update) : null], ['Countries', countries || null],
      ['Type', [a.category, a.subcategory ? a.subcategory.toLowerCase() : null].filter(Boolean).join(', ')], ['Requested by', a.requester || null],
      ['Activation', a.code],
    ] });
    if (a.stats?.length) sections.push({ title: 'Summary figures', rows: a.stats.map(s => [s.label, `${fmt.num(s.value, s.value >= 100 ? 0 : 1)}${s.unit ? ` ${s.unit}` : ''}`]) });
    if (a.aois?.length) sections.push({ title: `Areas and map products (${a.aois.length})`, el: productsEl(a) });
    else if (!a.detail) sections.push({ text: 'The list of areas and map products could not be loaded.' });
    if (a.gdacs?.length) {
      sections.push({ title: 'GDACS', list: a.gdacs.map(g => ({ title: g.entity.title,
        subtitle: g.match === 'linked' ? `Linked by Copernicus EMS${g.alert ? `, ${g.alert.toLowerCase()} alert` : ''}` : `Same hazard nearby, ${fmt.km(g.distance_km)} away${g.alert ? `, ${g.alert.toLowerCase()} alert` : ''}`,
        entity: g.entity })) });
    }
    const start = a.event_time || a.activation_time;
    const days = start ? Math.max(1, (now() - start) / 86400) : 3;      // "after" reaches today for recent events
    const cardKey = `emergency_mapping:${entity.id}`;
    const actions = [];
    if (a.has_extent) actions.push({ text: 'Show mapped area', primary: true, onClick: (card) => showExtent(card, a) });
    if (a.bbox) actions.push({ text: 'Zoom to the areas', onClick: () => ctx.map2.fitBbox(a.bbox, { maxZoom: 12 }) });
    if (ctx.dom.validLatLon(a.lat, a.lon)) actions.push(kit.compareAction(a.lat, a.lon, 11, imageryDates(start, 10, Math.min(30, days + 1))));
    const delivered = `${fmt.int(a.delivered || 0)}`;
    return {
      title: a.name, subtitle: clip(`${a.category}${a.subcategory ? `, ${a.subcategory.toLowerCase()}` : ''}${countries ? `, ${countries}` : ''}`, 120),
      badges,
      block: [
        { label: 'Activated', value: a.activation_time ? agoShort(a.activation_time) : '–', unit: a.activation_time ? 'ago' : '' },
        { label: 'Areas', value: fmt.int(a.n_aois || 0) },
        { label: 'Products', value: delivered, unit: `of ${fmt.int(a.n_products || 0)} delivered` },
        { label: 'Maps', value: fmt.int(a.maps || 0) },
      ],
      sections,
      links: [{ text: 'Activation page', href: a.page }, a.report ? { text: 'Story map', href: a.report } : null,
        a.products_zip && a.delivered ? { text: 'All products (zip)', href: a.products_zip } : null,
        a.charter_url ? { text: 'International Charter activation', href: a.charter_url } : null,
        { text: 'Terms of use', href: a.terms }].filter(Boolean),
      actions,
      entity: d.entity && ctx.dom.validLatLon(d.entity.lat, d.entity.lon) ? d.entity : (ctx.dom.validLatLon(entity.lat, entity.lon) ? entity : null),
      foot: `${a.credit}. Times are UTC.`,
      after: (card) => {
        const feats = (a.aois || []).filter(x => x.geometry).map(x => ({ type: 'Feature', geometry: x.geometry, properties: { name: x.name } }));
        if (feats.length) kit.select(card.key, feats, CEMS_COLOR[a.cat] || CEMS_COLOR.other);
        if (extentFor && extentFor !== cardKey) clearExtent();
      },
    };
  }

  extras.builders = extras.builders || {};
  extras.builders.cems = cemsCard;
  return { layer };
}
