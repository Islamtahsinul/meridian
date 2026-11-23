// Cards: the right-hand stack of entity details (CONTRACT.md section 10.7).

import { bus } from './bus.js';
import { h, clear, fmt, validLatLon, toMs } from './dom.js';
import { api } from './api.js';
import { flyTo, highlight, clearHighlight } from './map.js';

const MAX_CARDS = 4;
const cards = new Map();   // key -> card
let root = null;
let deps = null;           // {favourites, casefiles, entities, whatshere}

export function mount(el, dependencies) {
  root = el;
  deps = dependencies;
  // Favourites can change elsewhere (the Favourites window): keep every open card's star in step.
  bus.on('favourites:changed', () => { for (const card of cards.values()) if (card.spec.entity) renderCard(card, { keepMedia: true }); });
}

/**
 * spec: {
 *   key, kind?, title, subtitle?, icon?, accent?, badges?: [{text, tone}],
 *   block?: [{label, value, unit?}],                      // the data block strip
 *   media?: {type: 'image'|'hls'|'video'|'iframe'|'mjpeg', src, refreshMs?, alt?, credit?, poster?, stamp?},
 *   sections?: [ {title?, rows: [[label, value]]} | {title?, text} | {title?, el} |
 *                {title?, list: [{title, subtitle?, detail?, entity?, onClick?}]} |
 *                {title?, nearby: {lat, lon, radius_km?, kinds?, exclude?}} ],
 *   links?: [{text, href}], actions?: [{text, onClick, primary?}],
 *   entity?: {kind, id, title, subtitle?, lat, lon, layer?},   // enables fly to / favourite / case file / nearby
 *   live?: {everyMs, update: async (card) => newSpec | null},
 *   foot?: string, onClose?: () => void, pinned?: bool
 * }
 */
export function open(spec) {
  const key = spec.key || `${spec.kind || 'card'}:${spec.title}`;
  let card = cards.get(key);
  if (card) {
    card.spec = { ...spec, key };
    renderCard(card);
    root.prepend(card.el);
  } else {
    card = { key, spec: { ...spec, key }, el: h('article', { class: 'card', 'aria-label': spec.title }), timer: null, mediaTimer: null, hls: null, closed: false };
    cards.set(key, card);
    root.prepend(card.el);
    renderCard(card);
    trim();
  }
  startLive(card);
  const e = card.spec.entity;
  if (e && validLatLon(e.lat, e.lon)) highlight(+e.lat, +e.lon, /^#[0-9a-f]{6}$/i.test(card.spec.accent || '') ? card.spec.accent : '#6cb6ff');
  bus.emit('card:opened', { key, spec: card.spec });
  return handle(card);
}

function handle(card) {
  return {
    key: card.key,
    el: card.el,
    get spec() { return card.spec; },
    update(patch) { if (card.closed) return; card.spec = { ...card.spec, ...patch, key: card.key }; renderCard(card); },
    close() { close(card.key); },
    isOpen() { return !card.closed; },
  };
}

export function get(key) {
  const c = cards.get(key);
  return c ? handle(c) : null;
}

export function close(key) {
  const card = cards.get(key);
  if (!card) return;
  card.closed = true;
  stopLive(card);
  stopMedia(card);
  card.el.remove();
  cards.delete(key);
  try { card.spec.onClose?.(); } catch (err) { console.error(err); }
  if (!cards.size) clearHighlight();
  else {
    const top = cards.get(root.firstElementChild?.dataset?.key);
    const e = top?.spec?.entity;
    if (e && validLatLon(e.lat, e.lon)) highlight(+e.lat, +e.lon);
  }
  bus.emit('card:closed', { key });
}

export function closeAll() {
  for (const key of [...cards.keys()]) close(key);
}

function trim() {
  const els = [...root.children];
  for (const el of els.slice(MAX_CARDS)) {
    const key = el.dataset.key;
    const c = cards.get(key);
    if (c && !c.spec.pinned) close(key);
  }
}

function startLive(card) {
  stopLive(card);
  const live = card.spec.live;
  if (!live || !live.everyMs || !live.update) return;
  card.timer = setInterval(async () => {
    if (document.visibilityState !== 'visible' || card.closed) return;
    try {
      const next = await live.update(handle(card));
      if (next && !card.closed) {
        card.spec = { ...card.spec, ...next, key: card.key };
        renderCard(card, { keepMedia: true });
        const e = card.spec.entity;
        if (e && validLatLon(e.lat, e.lon) && root.firstElementChild === card.el) highlight(+e.lat, +e.lon);
      }
    } catch (err) {
      console.warn('card live update failed', err);
    }
  }, Math.max(2000, live.everyMs));
}

function stopLive(card) {
  if (card.timer) clearInterval(card.timer);
  card.timer = null;
}

function stopMedia(card) {
  if (card.mediaTimer) clearInterval(card.mediaTimer);
  card.mediaTimer = null;
  if (card.hls) { try { card.hls.destroy(); } catch { /* ignore */ } card.hls = null; }
}

// ------------------------------------------------------------------ rendering
function renderCard(card, { keepMedia = false } = {}) {
  const s = card.spec;
  const el = card.el;
  el.dataset.key = card.key;
  el.style.setProperty('--accent', s.accent || 'var(--paper-faint)');
  const oldMedia = keepMedia ? el.querySelector('.card-media') : null;
  const sameMedia = keepMedia && oldMedia && oldMedia.dataset.src === (s.media?.src || '');
  if (!sameMedia) stopMedia(card);
  clear(el);

  const tools = h('div', { class: 'card-tools' });
  const entity = s.entity;
  if (entity && deps?.favourites) {
    const fav = deps.favourites.isFav(entity.kind, String(entity.id));
    const favBtn = h('button', { type: 'button', class: fav ? 'fav-on' : '', title: fav ? 'Remove from favourites' : 'Add to favourites', 'aria-pressed': String(fav) }, fav ? '★' : '☆');
    favBtn.addEventListener('click', async () => {
      await deps.favourites.toggle(entity, s.favPayload || {});
      renderCard(card, { keepMedia: true });
    });
    tools.append(favBtn);
  }
  const pin = h('button', { type: 'button', title: s.pinned ? 'Unpin card' : 'Pin card (keeps it open)', 'aria-pressed': String(!!s.pinned) }, s.pinned ? '◆' : '◇');
  pin.addEventListener('click', () => { card.spec.pinned = !card.spec.pinned; renderCard(card, { keepMedia: true }); });
  const x = h('button', { type: 'button', title: 'Close card', 'aria-label': 'Close card' }, '×');
  x.addEventListener('click', () => close(card.key));
  tools.append(pin, x);

  el.append(h('header', { class: 'card-head' },
    h('div', { class: 'card-icon', 'aria-hidden': 'true' }, s.icon || '•'),
    h('div', { class: 'card-titles' }, h('h2', { class: 'card-title' }, s.title || 'Untitled'), s.subtitle ? h('p', { class: 'card-sub' }, s.subtitle) : null),
    tools));

  if (s.badges?.length) el.append(h('div', { class: 'badges' }, s.badges.filter(Boolean).map(b => h('span', { class: `badge ${b.tone || 'grey'}`, title: b.title || null }, b.text))));
  if (s.block?.length) {
    el.append(h('div', { class: 'data-block' }, s.block.filter(Boolean).map(b => h('div', null,
      h('span', { class: 'v' }, b.value == null || b.value === '' ? '–' : String(b.value), b.unit && b.value != null && b.value !== '' && b.value !== '–' ? h('small', null, b.unit) : null),
      h('span', { class: 'k' }, b.label)))));
  }
  if (s.media) el.append(sameMedia ? oldMedia : renderMedia(card, s.media));

  const body = h('div', { class: 'card-body' });
  for (const sec of s.sections || []) {
    if (!sec) continue;
    const box = h('section', { class: 'card-section' }, sec.title ? h('h3', null, sec.title) : null);
    if (sec.rows) {
      const dl = h('dl', { class: 'kv' });
      for (const row of sec.rows) {
        if (!row) continue;
        const [k, v] = row;
        if (v == null || v === '') continue;
        dl.append(h('dt', null, k), h('dd', null, v instanceof Node ? v : String(v)));
      }
      if (!dl.children.length) continue;
      box.append(dl);
    }
    if (sec.text) box.append(h('p', null, sec.text));
    if (sec.el) box.append(sec.el);
    if (sec.list) box.append(renderList(sec.list, sec.empty));
    if (sec.nearby) box.append(renderNearby(sec.nearby));
    body.append(box);
  }
  if (body.children.length) el.append(body);

  if (s.links?.length) el.append(h('div', { class: 'card-links' }, s.links.filter(l => l && l.href).map(l => h('a', { href: l.href }, l.text))));

  const actions = h('div', { class: 'card-actions' });
  for (const a of s.actions || []) {
    if (!a) continue;
    const b = h('button', { type: 'button', class: `btn${a.primary ? ' primary' : ''}` }, a.text);
    b.addEventListener('click', () => a.onClick?.(handle(card)));
    actions.append(b);
  }
  if (entity && validLatLon(entity.lat, entity.lon)) {
    const fly = h('button', { type: 'button', class: 'btn' }, 'Fly to');
    fly.addEventListener('click', () => flyTo(+entity.lat, +entity.lon, entity.zoom || 11));
    actions.append(fly);
    if (deps?.whatshere) {
      const near = h('button', { type: 'button', class: 'btn' }, "What's nearby");
      near.addEventListener('click', () => deps.whatshere.open(+entity.lat, +entity.lon, { exclude: entity }));
      actions.append(near);
    }
  }
  if (entity && deps?.casefiles) {
    const cf = h('button', { type: 'button', class: 'btn' }, 'Add to case file');
    cf.addEventListener('click', (ev) => deps.casefiles.addItemMenu(ev.currentTarget, entity, s.favPayload || {}));
    actions.append(cf);
  }
  if (actions.children.length) el.append(actions);
  if (s.foot) el.append(h('div', { class: 'card-foot' }, s.foot));
}

function renderList(items, empty = 'Nothing to show.') {
  if (!items.length) return h('p', { class: 'dim' }, empty);
  const ul = h('ul', { class: 'card-list' });
  for (const it of items) {
    const content = [it.detail ? h('span', { class: 'li-d' }, it.detail) : null, h('span', { class: 'li-t' }, it.title), it.subtitle ? h('span', { class: 'li-s' }, it.subtitle) : null];
    if (it.entity || it.onClick) {
      const b = h('button', { type: 'button' }, content);
      b.addEventListener('click', () => (it.onClick ? it.onClick() : deps.entities.open(it.entity)));
      ul.append(h('li', null, b));
    } else {
      ul.append(h('li', null, content));
    }
  }
  return ul;
}

const nearbyCache = new Map();   // key -> {t, promise}; re-renders reuse a lookup for 60 s

function nearbyLookup(params) {
  const key = JSON.stringify([(+params.lat).toFixed(3), (+params.lon).toFixed(3), params.radius_km, params.kind, params.limit]);
  const hit = nearbyCache.get(key);
  if (hit && Date.now() - hit.t < 60000) return hit.promise;
  const promise = api.get('/api/core/nearby', params);
  nearbyCache.set(key, { t: Date.now(), promise });
  promise.catch(() => nearbyCache.delete(key));
  if (nearbyCache.size > 50) nearbyCache.delete(nearbyCache.keys().next().value);
  return promise;
}

function renderNearby({ lat, lon, radius_km = 25, kinds = null, exclude = null, limit = 8 }) {
  const box = h('div', null, h('p', { class: 'dim' }, 'Looking around…'));
  nearbyLookup({ lat, lon, radius_km, kind: kinds, limit }).then((res) => {
    let items = res.results || [];
    if (exclude) items = items.filter(i => !(i.kind === exclude.kind && String(i.id) === String(exclude.id)));
    clear(box);
    box.append(renderList(items.slice(0, 12).map(i => ({ title: i.title, subtitle: i.subtitle, detail: i.distance_km != null ? fmt.km(i.distance_km) : '', entity: i })), 'Nothing else nearby.'));
  }).catch(() => { clear(box); box.append(h('p', { class: 'dim' }, 'Nearby search is unavailable.')); });
  return box;
}

function renderMedia(card, media) {
  const wrap = h('div', { class: 'card-media', dataset: { src: media.src || '' } });
  const credit = media.credit ? h('span', { class: 'credit' }, media.credit) : null;
  const stamp = h('span', { class: 'stamp' }, media.stamp || '');
  if (media.type === 'image' || media.type === 'mjpeg') {
    const img = h('img', { alt: media.alt || '', decoding: 'async', loading: 'eager', referrerpolicy: 'no-referrer' });
    const msg = h('div', { class: 'media-msg', hidden: true }, 'The image could not be loaded right now.');
    const load = () => {
      if (!media.src) return;
      const sep = media.src.includes('?') ? '&' : '?';
      img.src = media.type === 'image' && media.refreshMs ? `${media.src}${sep}_t=${Date.now()}` : media.src;
    };
    img.addEventListener('load', () => { msg.hidden = true; img.hidden = false; if (!media.stamp) stamp.textContent = `Loaded ${fmt.clock(Date.now())} UTC`; });
    img.addEventListener('error', () => { msg.hidden = false; img.hidden = true; });
    load();
    if (media.type === 'image' && media.refreshMs) card.mediaTimer = setInterval(() => { if (document.visibilityState === 'visible') load(); }, Math.max(5000, media.refreshMs));
    wrap.append(img, msg, credit, stamp);
  } else if (media.type === 'video' || media.type === 'hls') {
    const video = h('video', { controls: true, muted: true, autoplay: true, playsinline: true, poster: media.poster || null });
    video.muted = true;
    wrap.append(video, credit);
    if (media.type === 'hls' && !video.canPlayType('application/vnd.apple.mpegurl')) {
      loadHls().then((Hls) => {
        if (card.closed) return;
        if (!Hls || !Hls.isSupported()) { wrap.append(h('div', { class: 'media-msg' }, 'This browser cannot play this stream.')); return; }
        const hls = new Hls({ enableWorker: true, lowLatencyMode: true, maxBufferLength: 20 });
        card.hls = hls;
        hls.loadSource(media.src);
        hls.attachMedia(video);
        hls.on(Hls.Events.ERROR, (_e, data) => { if (data.fatal) { wrap.append(h('div', { class: 'media-msg' }, 'The live stream stopped. It may be offline.')); hls.destroy(); } });
      });
    } else {
      video.src = media.src;
    }
  } else if (media.type === 'iframe') {
    wrap.append(h('iframe', { src: media.src, title: media.alt || 'Embedded player', loading: 'lazy', referrerpolicy: 'no-referrer',
      sandbox: 'allow-scripts allow-same-origin allow-presentation', allow: 'autoplay; fullscreen' }));
  }
  return wrap;
}

let hlsPromise = null;
export function loadHls() {
  if (window.Hls) return Promise.resolve(window.Hls);
  if (!hlsPromise) {
    hlsPromise = new Promise((resolve) => {
      const s = document.createElement('script');
      s.src = '/vendor/hls.js/hls.min.js';
      s.onload = () => resolve(window.Hls);
      s.onerror = () => resolve(null);
      document.head.append(s);
    });
  }
  return hlsPromise;
}

export function count() { return cards.size; }

export const cardsApi = { open, close, closeAll, get, count, loadHls };

// keep toMs import used for callers formatting stamps
export { toMs };
