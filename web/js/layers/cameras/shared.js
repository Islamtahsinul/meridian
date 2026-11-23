// Shared bits for the cameras module: colours, labels, formatting and the "My grid" list.

export const CAM = '#efe3c2';        // Cameras group colour (still images)
export const LIVE = '#ffb08a';       // live or recorded video
export const PAGE = '#8b97b0';       // page-only cameras (no in-app image)
export const INK = '#111a2c';

export const KIND_LABEL = { image: 'Still image', video: 'Video clip', hls: 'Live video', mjpeg: 'Live video', page: 'Source page only' };
export const TAG_LABEL = {
  traffic: 'Traffic', urban: 'City', coastal: 'Coast', mountain: 'Mountain', weather: 'Weather', port: 'Port',
  airport: 'Airport', bridge: 'Bridge', tunnel: 'Tunnel', border: 'Border', wildfire: 'Wildfire lookout', river: 'River',
  volcano: 'Volcano', polar: 'Polar station',
};
export const COMPASS_WORD = { N: 'north', NE: 'north-east', E: 'east', SE: 'south-east', S: 'south', SW: 'south-west', W: 'west', NW: 'north-west' };

// a camera outline for cards and buttons (stroke icon, 20x20 viewBox)
export const CAMERA_PATH = 'M3 6.5h3l1.3-2h5.4l1.3 2h3v9.5H3V6.5Zm7 2.3a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z';
export const GRID_PATH = 'M3.5 3.5h5.5V9H3.5zM11 3.5h5.5V9H11zM3.5 11H9v5.5H3.5zM11 11h5.5v5.5H11z';

export const isLiveKind = (k) => k === 'video' || k === 'hls' || k === 'mjpeg';

export function cadence(seconds) {
  const s = Math.max(1, Math.round(+seconds || 0));
  if (s < 90) return { value: String(s), unit: 's', text: `${s} s` };
  if (s < 5400) return { value: String(Math.round(s / 60)), unit: 'min', text: `${Math.round(s / 60)} min` };
  return { value: String(Math.round(s / 3600)), unit: 'h', text: `${Math.round(s / 3600)} h` };
}

export function ageParts(ms) {
  if (!Number.isFinite(ms)) return { value: '–', unit: '' };
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 90) return { value: String(s), unit: 's' };
  if (s < 5400) return { value: String(Math.round(s / 60)), unit: 'min' };
  if (s < 86400 * 2) return { value: String(Math.round(s / 3600)), unit: 'h' };
  return { value: String(Math.round(s / 86400)), unit: 'days' };
}

export function imgUrl(ctx, id, params = {}) {
  return ctx.api.url(`/api/cameras/img/${encodeURIComponent(id)}`, { ...params, _t: Date.now() });
}

export function entityOf(cam) {
  return { kind: 'camera', id: cam.id, title: cam.name || cam.title || cam.id, subtitle: cam.subtitle || '',
    lat: cam.lat, lon: cam.lon, layer: 'cameras', zoom: 15 };
}

// ------------------------------------------------------------------ "My grid" (cameras pinned to the grid)
const GRID_KEY = 'cameras.grid.mine';
const MAX_MINE = 64;

export function gridList(ctx) {
  const list = ctx.settings.get(GRID_KEY, []);
  return Array.isArray(list) ? list.filter(x => x && typeof x.id === 'string').slice(0, MAX_MINE) : [];
}

export function inGrid(ctx, id) {
  return gridList(ctx).some(x => x.id === id);
}

export function toggleGrid(ctx, cam) {
  const list = gridList(ctx);
  const i = list.findIndex(x => x.id === cam.id);
  if (i >= 0) list.splice(i, 1);
  else list.unshift({ id: cam.id, title: cam.name || cam.title || cam.id });
  ctx.settings.set(GRID_KEY, list.slice(0, MAX_MINE));
  ctx.bus.emit('cameras:grid-changed', { id: cam.id, added: i < 0 });
  return i < 0;
}

export function removeFromGrid(ctx, id) {
  ctx.settings.set(GRID_KEY, gridList(ctx).filter(x => x.id !== id));
  ctx.bus.emit('cameras:grid-changed', { id, added: false });
}

// ------------------------------------------------------------------ direct pictures (Windy)
// Some sources' terms say the browser must show each picture from the source's own, short-lived
// link (never proxied), linked to the source and credited. The server looks the links up with its
// key (/api/cameras/direct, reused for 5 min); here, lookups made within a few milliseconds of each
// other (a grid page, What's here) go out as one request.
const DIRECT_TTL_MS = 4 * 60 * 1000;
const directCache = new Map();     // id -> {t, promise}
let directQueue = new Map();       // id -> [{resolve, reject}]
let directTimer = null;

async function flushDirect(ctx) {
  directTimer = null;
  const batch = [...directQueue.entries()].slice(0, 50);
  for (const [id] of batch) directQueue.delete(id);
  if (directQueue.size) directTimer = setTimeout(() => flushDirect(ctx), 10);
  if (!batch.length) return;
  let items = {};
  let failure = null;
  try {
    const res = await ctx.api.get('/api/cameras/direct', { ids: batch.map(([id]) => id).join(',') });
    items = res.items || {};
  } catch (err) { failure = err; }
  for (const [id, waiters] of batch) {
    const info = items[id];
    for (const w of waiters) {
      if (info) w.resolve(info);
      else w.reject(failure || new Error('No picture link for this camera.'));
    }
  }
}

export function directInfo(ctx, id, { fresh = false } = {}) {
  const hit = directCache.get(id);
  if (!fresh && hit && Date.now() - hit.t < DIRECT_TTL_MS) return hit.promise;
  const promise = new Promise((resolve, reject) => {
    const list = directQueue.get(id) || [];
    list.push({ resolve, reject });
    directQueue.set(id, list);
    if (!directTimer) directTimer = setTimeout(() => flushDirect(ctx), 30);
  });
  directCache.set(id, { t: Date.now(), promise });
  promise.catch(() => { if (directCache.get(id)?.promise === promise) directCache.delete(id); });
  if (directCache.size > 600) directCache.delete(directCache.keys().next().value);
  return promise;
}

// "Webcams provided by Windy.com, add a webcam": the credit line such sources ask for, as links
export function courtesyLine(ctx, credit, extraClass = '') {
  if (!Array.isArray(credit) || !credit.length) return null;
  const parts = [];
  credit.forEach((c, i) => {
    if (!c || !c.text) return;
    if (parts.length) parts.push(', ');
    parts.push(c.href ? ctx.h('a', { href: c.href }, c.text) : c.text);
  });
  return parts.length ? ctx.h('span', { class: `cameras-courtesy${extraClass ? ` ${extraClass}` : ''}` }, parts) : null;
}
