// Map actions returned by the assistant: validated again here (known types only, numbers in range,
// layer ids that exist), run through ctx.ai.run, described in plain words, and undoable.

const KIND = /^[a-z][a-z0-9_]{1,39}$/;
// Run order: display settings, layers, cards, then the camera, so an explicit camera move wins over
// the move an entity card makes on its own.
const ORDER = ['set_basemap', 'set_projection', 'set_terrain', 'set_layers', 'open_entity', 'whats_here', 'fit_bounds', 'fly_to', 'highlight'];

const num = (v, lo, hi) => (typeof v === 'number' && Number.isFinite(v) && v >= lo && v <= hi ? v : null);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const latlonOk = (lat, lon) => num(lat, -90, 90) !== null && num(lon, -180, 180) !== null;

function layerIds(list, ctx) {
  if (!Array.isArray(list)) return [];
  return [...new Set(list.filter(id => typeof id === 'string' && id.length <= 80 && ctx.layers.get(id)))];
}

/** One action from the server -> {ok: true, action} (a clean copy) or {ok: false, reason}. */
export function validateAction(a, ctx) {
  if (!a || typeof a !== 'object' || !ORDER.includes(a.type)) return { ok: false, reason: 'unknown action' };
  if (!ctx.ai.actions().includes(a.type)) return { ok: false, reason: 'this map action is not available' };
  switch (a.type) {
    case 'fly_to': {
      if (!latlonOk(a.lat, a.lon)) return { ok: false, reason: 'position out of range' };
      const out = { type: 'fly_to', lat: a.lat, lon: a.lon };
      if (a.zoom != null) {
        if (num(a.zoom, 0, 24) === null) return { ok: false, reason: 'zoom out of range' };
        out.zoom = clamp(a.zoom, 1, 18);
      }
      return { ok: true, action: out };
    }
    case 'fit_bounds': {
      const { west, south, east, north } = a;
      if (num(west, -180, 180) === null || num(east, -180, 180) === null || num(south, -90, 90) === null
        || num(north, -90, 90) === null || south >= north) return { ok: false, reason: 'area out of range' };
      return { ok: true, action: { type: 'fit_bounds', west, south: clamp(south, -85, 85), east: east < west ? east + 360 : east, north: clamp(north, -85, 85) } };
    }
    case 'set_layers': {
      const on = layerIds(a.on, ctx);
      const off = layerIds(a.off, ctx).filter(id => !on.includes(id));
      if (!on.length && !off.length) return { ok: false, reason: 'no known layers' };
      return { ok: true, action: { type: 'set_layers', on, off } };
    }
    case 'set_basemap': {
      const ids = (ctx.map2.BASEMAPS || []).map(b => b.id);
      return ids.includes(a.id) ? { ok: true, action: { type: 'set_basemap', id: a.id } } : { ok: false, reason: 'unknown basemap' };
    }
    case 'set_projection':
      return ['globe', 'mercator'].includes(a.projection) ? { ok: true, action: { type: 'set_projection', projection: a.projection } }
        : { ok: false, reason: 'unknown projection' };
    case 'set_terrain':
      return typeof a.on === 'boolean' ? { ok: true, action: { type: 'set_terrain', on: a.on } } : { ok: false, reason: 'terrain needs on or off' };
    case 'highlight':
    case 'whats_here':
      return latlonOk(a.lat, a.lon) ? { ok: true, action: { type: a.type, lat: a.lat, lon: a.lon } } : { ok: false, reason: 'position out of range' };
    case 'open_entity': {
      const entity = cleanEntity(a, ctx);
      return entity ? { ok: true, action: { type: 'open_entity', ...entity } } : { ok: false, reason: 'not a valid entity' };
    }
    default:
      return { ok: false, reason: 'unknown action' };
  }
}

/** The canonical entity fields only (CONTRACT 5.6), or null. */
export function cleanEntity(e, ctx) {
  if (!e || typeof e !== 'object' || typeof e.kind !== 'string' || !KIND.test(e.kind)) return null;
  if (!(typeof e.id === 'string' || (typeof e.id === 'number' && Number.isFinite(e.id))) || String(e.id).trim() === '' || String(e.id).length > 200) return null;
  const out = { kind: e.kind, id: String(e.id) };
  for (const key of ['title', 'subtitle']) if (typeof e[key] === 'string' && e[key].trim()) out[key] = e[key].trim().slice(0, 200);
  if (latlonOk(e.lat, e.lon)) { out.lat = e.lat; out.lon = e.lon; }
  if (typeof e.layer === 'string' && ctx.layers.get(e.layer)) out.layer = e.layer;
  if (num(e.zoom, 0, 22) !== null) out.zoom = e.zoom;
  if (typeof e.time === 'string' || num(e.time, 0, 1e13) !== null) out.time = e.time;
  if (num(e.distance_km, 0, 40100) !== null) out.distance_km = e.distance_km;
  if (Array.isArray(e.bbox) && e.bbox.length === 4 && num(e.bbox[0], -180, 180) !== null && num(e.bbox[2], -180, 180) !== null
    && num(e.bbox[1], -90, 90) !== null && num(e.bbox[3], -90, 90) !== null && e.bbox[1] < e.bbox[3]) out.bbox = [...e.bbox];
  return out;
}

/** -> {valid: [actions in run order], dropped: [{action, reason}]} */
export function validateActions(list, ctx) {
  const valid = [];
  const dropped = [];
  for (const a of Array.isArray(list) ? list.slice(0, 12) : []) {
    const r = validateAction(a, ctx);
    if (r.ok) valid.push(r.action); else dropped.push({ action: a, reason: r.reason });
  }
  valid.sort((x, y) => ORDER.indexOf(x.type) - ORDER.indexOf(y.type));
  return { valid, dropped };
}

/** Run validated actions in order. set_projection goes straight to ctx.map2 (see the module doc);
 *  layer changes start in the background so they never hold up the camera. */
export async function runActions(actions, ctx) {
  const done = new Set();
  const failed = [];
  const list = [...actions];
  // A card and a camera move to the same spot: one move, at the zoom that was asked for.
  const fly = list.find(a => a.type === 'fly_to');
  const card = list.find(a => a.type === 'open_entity' && latlonOk(a.lat, a.lon));
  if (fly && card && Math.abs(fly.lat - card.lat) < 0.05 && Math.abs(fly.lon - card.lon) < 0.05) {
    if (fly.zoom != null && card.zoom == null) card.zoom = fly.zoom;
    list.splice(list.indexOf(fly), 1);
    done.add(fly);
  }
  for (const a of list) {
    try {
      if (a.type === 'set_projection') ctx.map2.setProjection(a.projection);
      else if (a.type === 'set_layers') Promise.resolve(ctx.ai.run(a)).catch(err => console.warn('ai: layer change failed', err));
      else await ctx.ai.run(a);
      done.add(a);
    } catch (err) {
      console.warn('ai: map action failed', a.type, err);
      failed.push({ action: a, error: String(err?.message || err) });
    }
  }
  return { done: actions.filter(a => done.has(a)), failed };
}

function layerTitle(id, ctx) { return ctx.layers.get(id)?.spec?.title || id; }

function joinWords(items) {
  if (items.length <= 1) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

const TELL = ['fit_bounds', 'fly_to', 'set_basemap', 'set_projection', 'set_terrain', 'set_layers', 'highlight', 'open_entity', 'whats_here'];

/** Plain sentences for what the map did, camera first. */
export function describeActions(done, ctx) {
  const fmt = ctx.fmt;
  const out = [];
  for (const a of [...done].sort((x, y) => TELL.indexOf(x.type) - TELL.indexOf(y.type))) {
    switch (a.type) {
      case 'fly_to': out.push(`Moved the map to ${fmt.latlon(a.lat, a.lon, 2)}`); break;
      case 'fit_bounds': out.push('Fitted the map to the area'); break;
      case 'set_layers':
        if (a.on.length) out.push(`Turned on ${joinWords(a.on.map(id => layerTitle(id, ctx)))}`);
        if (a.off.length) out.push(`Turned off ${joinWords(a.off.map(id => layerTitle(id, ctx)))}`);
        break;
      case 'set_basemap': out.push(`Switched the basemap to ${(ctx.map2.BASEMAPS || []).find(b => b.id === a.id)?.title || a.id}`); break;
      case 'set_projection': out.push(a.projection === 'globe' ? 'Switched to the globe' : 'Switched to the flat map'); break;
      case 'set_terrain': out.push(a.on ? 'Turned 3D terrain on' : 'Turned 3D terrain off'); break;
      case 'highlight': out.push(`Marked ${fmt.latlon(a.lat, a.lon, 2)}`); break;
      case 'open_entity': out.push(`Opened ${a.title || a.id}`); break;
      case 'whats_here': out.push("Opened What's here?"); break;
      default: break;
    }
  }
  return out.map(s => `${s}.`).join(' ');
}

/** What the map looks like now, so the assistant's changes can be undone. */
export function captureView(ctx) {
  const map = ctx.map;
  const c = map.getCenter();
  return { center: [c.lng, c.lat], zoom: map.getZoom(), bearing: map.getBearing(), pitch: map.getPitch(),
    on: new Set(ctx.layers.list().filter(l => l.on).map(l => l.id)), view: ctx.map2.viewState() };
}

export async function restoreView(snapshot, ctx) {
  const now = ctx.map2.viewState();
  const before = snapshot.view;
  if (before.basemap !== now.basemap) ctx.map2.setBasemap(before.basemap);
  if (before.projection !== now.projection) ctx.map2.setProjection(before.projection);
  if (!!before.terrain !== !!now.terrain) ctx.map2.setTerrain(!!before.terrain);
  const onNow = new Set(ctx.layers.list().filter(l => l.on).map(l => l.id));
  for (const id of onNow) if (!snapshot.on.has(id)) await ctx.layers.setOn(id, false);
  for (const id of snapshot.on) if (!onNow.has(id)) await ctx.layers.setOn(id, true);
  const camera = { center: snapshot.center, zoom: snapshot.zoom, bearing: snapshot.bearing, pitch: snapshot.pitch };
  if (ctx.map2.reducedMotion()) ctx.map.jumpTo(camera); else ctx.map.flyTo({ ...camera, speed: 1.8, essential: true });
}
