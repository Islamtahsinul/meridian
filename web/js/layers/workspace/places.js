// My places: the user's named places (home, work, family...), kept in Meridian's database on this
// computer through /api/workspace/places. They are never sent to the assistant, MCP tools or any
// website. Each can be shown on the map (layer workspace.places), found by name in the search box,
// flown to, and offered to Alerts as a place watchlist through Alerts' own API (POST
// /api/alerts/watchlists): Alerts then keeps its own copy.
//
// Places use their own entity kind, my_place (id = the place's number), so search results and deep links
// (e=my_place:3) open their card without touching the core's geocoder "place" results. The card itself
// carries no core entity: that would add the favourite star and "Add to case file", and favourites are
// searched by the assistant and MCP tools (search_entities). It has its own Fly to instead, and no
// "What's nearby": that card asks OpenStreetMap and the weather source about the point.

import { PLACE_RADII, PLACE_TYPES } from './logic.js';

const SRC = 'ov-workspace-places';
const L_RING = 'ov-workspace-places-ring';
const L_DOT = 'ov-workspace-places-dot';
const L_LABEL = 'ov-workspace-places-label';
const COLOR = '#ebe6d6';
const ALERT_RULES = ['quakes', 'storms', 'warnings', 'volcanoes'];   // the same starting set Alerts uses
const ICON = '\u2302';

export function typeLabel(type) {
  return (PLACE_TYPES.find(t => t.id === type) || PLACE_TYPES[PLACE_TYPES.length - 1]).label;
}

export function createPlaces(ctx, { backend }) {
  const { h, api, ui, fmt } = ctx;
  const listeners = new Set();
  const s = { places: [], max: 50, loaded: false, error: null, watched: null };

  const emit = () => { for (const fn of [...listeners]) { try { fn(); } catch (err) { console.error('workspace: places listener failed', err); } } };
  const byId = (id) => s.places.find(p => String(p.id) === String(id)) || null;

  async function load() {
    if (!backend) { s.error = 'The workspace module is not running on the server.'; emit(); return s.places; }
    try {
      const r = await api.get('/api/workspace/places');
      s.places = r.places || [];
      s.max = r.max || 50;
      s.loaded = true;
      s.error = null;
    } catch (err) {
      s.error = err?.message || 'The places could not be loaded.';
    }
    render();
    emit();
    return s.places;
  }

  async function checkWatched() {
    if (!ctx.moduleOk('alerts')) { s.watched = null; return; }
    try {
      const r = await api.get('/api/alerts/watchlists');
      s.watched = new Set((r.watchlists || []).map(w => w.id));
    } catch { s.watched = null; return; }
    // A watchlist deleted in Alerts: forget the link so the place can be offered again.
    for (const p of s.places) {
      if (p.alerts_watchlist_id && !s.watched.has(p.alerts_watchlist_id)) {
        try { await api.post('/api/workspace/places', { action: 'watchlist', id: p.id, watchlist_id: null }); p.alerts_watchlist_id = null; } catch { /* next time */ }
      }
    }
    emit();
  }

  const watching = (p) => !!p.alerts_watchlist_id && (!s.watched || s.watched.has(p.alerts_watchlist_id));

  // -- the map layer ---------------------------------------------------------------------------
  function features() {
    return { type: 'FeatureCollection', features: s.places.map(p => ({ type: 'Feature', geometry: { type: 'Point', coordinates: [p.lon, p.lat] },
      properties: { id: p.id, name: p.name, type: p.type } })) };
  }

  function render() {
    if (!ctx.map.getSource(SRC)) return;
    ctx.map2.setData(SRC, features());
    layer.setCount(s.places.length);
  }

  const layer = ctx.layers.register({
    id: 'workspace.places', group: 'Saved', title: 'My places', subtitle: 'Your own places, on this computer only',
    description: 'Your named places (home, work, family). They stay in Meridian\'s database on this computer and are never sent to the assistant, MCP tools or any website.',
    source: 'Your places, stored on this computer', color: COLOR, symbol: 'point', defaultOn: false,
    async enable() {
      ctx.map2.addSource(SRC, { type: 'geojson', data: features() });
      ctx.map2.addLayer({ id: L_RING, type: 'circle', source: SRC, paint: { 'circle-radius': 9, 'circle-color': 'rgba(0,0,0,0)',
        'circle-stroke-width': 1.5, 'circle-stroke-color': COLOR, 'circle-opacity': 0.9, 'circle-pitch-alignment': 'map' } }, 'points');
      ctx.map2.addLayer({ id: L_DOT, type: 'circle', source: SRC, paint: { 'circle-radius': 3.5, 'circle-color': COLOR,
        'circle-stroke-width': 1, 'circle-stroke-color': '#111a2c' } }, 'points');
      ctx.map2.addLayer({ id: L_LABEL, type: 'symbol', source: SRC, layout: { 'text-field': ['get', 'name'], 'text-font': ['Open Sans Regular'],
        'text-size': 12, 'text-offset': [0, 1.2], 'text-anchor': 'top', 'text-optional': true },
        paint: { 'text-color': COLOR, 'text-halo-color': '#111a2c', 'text-halo-width': 1.4 } }, 'top');
      if (!s.loaded) await load(); else render();
    },
    async disable() { ctx.map2.removeSource(SRC); },
  });

  ctx.map2.onClick([L_RING, L_DOT, L_LABEL], (f) => { const p = byId(f.properties?.id); if (p) openCard(p); });
  ctx.map2.onHover([L_RING, L_DOT, L_LABEL], (f) => (f.properties?.name ? `${f.properties.name} (my place)` : null));

  // -- cards, entities and search --------------------------------------------------------------
  function entityOf(p) {
    return { kind: 'my_place', id: String(p.id), title: p.name, subtitle: `My place, ${typeLabel(p.type).toLowerCase()}`,
      lat: p.lat, lon: p.lon, zoom: p.zoom || 11, layer: 'workspace.places' };
  }

  // No `entity` on the card (see the top of this file): the core would add a favourite star and "Add to
  // case file", and a starred place would reach search_entities with its exact position.
  function openCard(p) {
    const actions = [{ text: 'Fly to', onClick: () => flyTo(p) }];
    if (ctx.moduleOk('alerts') && !watching(p)) actions.push({ text: 'Watch in Alerts', onClick: () => watchInAlerts(p) });
    const card = ctx.cards.open({
      key: `my_place:${p.id}`, kind: 'my_place', title: p.name, subtitle: `My place, ${typeLabel(p.type).toLowerCase()}`,
      icon: ICON, accent: COLOR,
      badges: watching(p) ? [{ text: 'Watched in Alerts', tone: 'blue' }] : [],
      block: [{ label: 'Type', value: typeLabel(p.type) }, { label: 'Alerts radius', value: fmt.num(p.radius_km, 0), unit: 'km' }],
      sections: [{ rows: [['Position', fmt.latlon(p.lat, p.lon, 4)], ['Notes', p.notes || '']] },
        { title: 'Kept on this computer', text: 'Your places stay in Meridian\'s database here. They are not sent to the assistant, to MCP tools or to any website, so they can\'t be starred or put in a case file (the assistant can search favourites). Alerts keeps its own copy of a place you watch there.' },
        { title: 'Nearby', nearby: { lat: p.lat, lon: p.lon, radius_km: Math.min(50, Math.max(5, p.radius_km)), exclude: entityOf(p) } }],
      actions,
      foot: 'My places, Workspaces drawer',
      onClose: () => ctx.map2.clearHighlight(),   // the core then rings the next card's entity, if any
    });
    ctx.map2.highlight(p.lat, p.lon, COLOR);      // what the core does for cards with an entity
    return card;
  }

  ctx.entities.handler('my_place', async (entity) => {
    if (!s.loaded) await load();
    const p = byId(entity.id);
    if (p) return openCard(p);
    return ctx.cards.open({ key: `my_place:${entity.id}`, kind: 'my_place', title: entity.title || 'My place', subtitle: 'No longer in My places',
      icon: ICON, accent: COLOR, sections: [{ text: 'This place was deleted from My places, or it was saved on another computer.' }] });
  }, { group: 'Saved', icon: ICON });

  ctx.search.provider((q) => {
    const needle = q.trim().toLowerCase();
    if (!needle || !s.loaded) return [];
    return s.places.filter(p => p.name.toLowerCase().includes(needle) || typeLabel(p.type).toLowerCase() === needle)
      .slice(0, 6).map(p => ({ ...entityOf(p), score: p.name.toLowerCase().startsWith(needle) ? 0.95 : 0.8 }));
  });

  // -- changes -----------------------------------------------------------------------------------
  async function create(place) {
    const r = await api.post('/api/workspace/places', { action: 'create', place });
    await load();
    return r.place;
  }

  async function update(id, place) {
    const r = await api.post('/api/workspace/places', { action: 'update', id, place });
    await load();
    return r.place;
  }

  async function remove(id) {
    await api.post('/api/workspace/places', { action: 'delete', id });
    ctx.cards.close(`my_place:${id}`);
    await load();
  }

  function flyTo(p) {
    if (!ctx.layers.isOn('workspace.places')) ctx.layers.setOn('workspace.places', true);
    ctx.map2.flyTo(p.lat, p.lon, p.zoom || 11);
    ctx.map2.highlight(p.lat, p.lon, COLOR);
  }

  async function watchInAlerts(p) {
    if (!ctx.moduleOk('alerts')) { ui.toast('Alerts is not running on the server.', 'amber'); return null; }
    let presets = [];
    try { presets = (await api.get('/api/alerts/presets')).presets || []; } catch { presets = []; }
    const usable = new Set(presets.filter(x => x.available !== false).map(x => x.id));
    const rules = ALERT_RULES.filter(id => usable.has(id)).map(preset => ({ preset }));
    if (!rules.length) { ui.toast('Alerts can\'t watch for anything here right now: the hazards module is not running.', 'amber'); return null; }
    const names = rules.map(r => presets.find(x => x.id === r.preset)?.label || r.preset).join(', ').toLowerCase();
    const ok = await ui.confirm(`Alerts will get its own copy of this place: a watchlist called "${p.name}" covering ${fmt.num(p.radius_km, 0)} km around it, `
      + `watching for ${names}. You can change it in Alerts, Watchlists. If Alerts shares alerts with the assistant (Alerts, Settings), `
      + 'the watchlist\'s name and its alerts can reach the assistant and MCP tools.', { ok: 'Watch in Alerts' });
    if (!ok) return null;
    try {
      const r = await api.post('/api/alerts/watchlists', { action: 'create', name: p.name,
        area: { type: 'place', place: { name: p.name, lat: p.lat, lon: p.lon }, radius_km: p.radius_km }, rules });
      const wid = r?.id ?? r?.watchlist?.id;
      if (wid) await api.post('/api/workspace/places', { action: 'watchlist', id: p.id, watchlist_id: wid });
      ui.toast(`Watching ${p.name} in Alerts. New alerts arrive in its inbox.`, 'green');
      await load();
      await checkWatched();
      if (ctx.cards.get(`my_place:${p.id}`)) { const fresh = byId(p.id); if (fresh) openCard(fresh); }
      return wid;
    } catch (err) {
      ui.toast(`Alerts did not take the place: ${err?.message || err}`, 'red');
      return null;
    }
  }

  return {
    state: s, load, checkWatched, create, update, remove, flyTo, watchInAlerts, openCard, watching, byId, layer,
    radii: PLACE_RADII, types: PLACE_TYPES,
    on(fn) { listeners.add(fn); return () => listeners.delete(fn); },
  };
}
