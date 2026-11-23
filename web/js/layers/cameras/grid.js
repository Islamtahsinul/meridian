// The camera grid: a full-screen wall of 2x2, 3x3 or 4x4 live tiles from "My grid", favourite
// cameras or the cameras in the current map view. Each tile refreshes at its camera's cadence
// (while the page is visible); click a tile for its card. Choices persist in ctx.settings.

import { CAMERA_PATH, entityOf, gridList, removeFromGrid } from './shared.js';
import { createView } from './view.js';

const MODES = [
  { id: 'mine', label: 'My grid' },
  { id: 'favourites', label: 'Favourites' },
  { id: 'view', label: 'In map view' },
];
const SIZES = [2, 3, 4];
const KEY = 'cameras.grid';


export function openGrid(ctx, { mode = null } = {}) {
  const { h } = ctx;
  const saved = ctx.settings.get(KEY, {}) || {};
  const st = {
    mode: mode || (MODES.some(m => m.id === saved.mode) ? saved.mode : (gridList(ctx).length ? 'mine' : 'view')),
    size: SIZES.includes(saved.size) ? saved.size : 3,
    page: 0,
  };
  let tiles = [];
  let items = [];
  let total = 0;
  let seq = 0;
  let closed = false;

  const persist = () => ctx.settings.set(KEY, { mode: st.mode, size: st.size });
  const seg = (choices, current, onPick, label) => h('div', { class: 'seg cameras-seg', role: 'group', 'aria-label': label },
    choices.map(c => {
      const b = h('button', { type: 'button', 'aria-pressed': String(c.id === current) }, c.label);
      b.addEventListener('click', () => onPick(c.id));
      return b;
    }));

  const bar = h('div', { class: 'cameras-gridbar' });
  const wall = h('div', { class: 'cameras-wall' });
  const root = h('div', { class: 'cameras-grid' }, bar, wall);

  function clearTiles() {
    for (const t of tiles) t.view?.destroy();
    tiles = [];
    ctx.dom.clear(wall);
  }

  function renderBar() {
    ctx.dom.clear(bar);
    const per = st.size * st.size;
    const pages = Math.max(1, Math.ceil(items.length / per));
    if (st.page >= pages) st.page = pages - 1;
    const from = items.length ? st.page * per + 1 : 0;
    const to = Math.min(items.length, (st.page + 1) * per);
    const prev = h('button', { type: 'button', class: 'icon-btn', title: 'Previous page', 'aria-label': 'Previous page', disabled: st.page <= 0 || null },
      ctx.dom.svgIcon('M12.5 4.5 7 10l5.5 5.5'));
    const next = h('button', { type: 'button', class: 'icon-btn', title: 'Next page', 'aria-label': 'Next page', disabled: st.page >= pages - 1 || null },
      ctx.dom.svgIcon('M7.5 4.5 13 10l-5.5 5.5'));
    prev.addEventListener('click', () => { st.page -= 1; render(); });
    next.addEventListener('click', () => { st.page += 1; render(); });
    const range = `${from}–${to}`;
    const count = !items.length ? '' : st.mode === 'view' && total > items.length
      ? `${range} of the nearest ${ctx.fmt.int(items.length)}, ${ctx.fmt.int(total)} in view`
      : `${range} of ${ctx.fmt.int(items.length)}`;
    bar.append(
      seg(MODES, st.mode, (id) => { st.mode = id; st.page = 0; persist(); load(); }, 'Cameras to show'),
      seg(SIZES.map(s => ({ id: s, label: `${s}×${s}` })), st.size, (s) => { st.size = s; st.page = 0; persist(); render(); }, 'Grid size'),
      h('span', { class: 'cameras-gridcount' }, count),
      h('span', { class: 'cameras-pager' }, prev, next),
      h('span', { class: 'cameras-gridnote' }, 'Tiles refresh at each camera’s pace. Latest frames only.'));
  }

  function empty(text, action) {
    clearTiles();
    wall.classList.add('cameras-wall-empty');
    wall.append(h('div', { class: 'empty cameras-empty' }, ctx.dom.svgIcon(CAMERA_PATH, { size: 28 }), h('p', null, text), action || null));
  }

  function render() {
    if (closed) return;
    renderBar();
    clearTiles();
    wall.classList.remove('cameras-wall-empty');
    wall.style.setProperty('--n', st.size);
    if (!items.length) {
      if (st.mode === 'mine') return empty('Your grid is empty. Open any camera and press “Add to grid”, or show favourites or the cameras in view instead.');
      if (st.mode === 'favourites') return empty('No favourite cameras yet. Open a camera and press the star on its card.');
      return empty('No cameras in this part of the map. Move or zoom the map out, then open the grid again.');
    }
    const per = st.size * st.size;
    const slice = items.slice(st.page * per, (st.page + 1) * per);
    slice.forEach((cam, i) => wall.append(tile(cam, i)));
    const hint = st.mode === 'mine' ? 'Add more with “Add to grid” on any camera card.'
      : st.mode === 'favourites' ? 'Star more cameras to fill the grid.' : '';
    for (let i = slice.length; i < per; i++) {
      wall.append(h('div', { class: 'cameras-tile cameras-tile-blank', 'aria-hidden': hint && i === slice.length ? null : 'true' },
        hint && i === slice.length ? h('p', { class: 'cameras-blankhint' }, hint) : null));
    }
  }

  function tile(cam, i) {
    if (cam.missing) {
      const gone = h('div', { class: 'cameras-tile cameras-tile-gone' }, h('div', { class: 'cameras-cap' },
        h('span', { class: 'cameras-capname' }, cam.title || cam.id), h('span', { class: 'cameras-capsub' }, 'No longer listed')));
      if (st.mode === 'mine') {
        const rm = h('button', { type: 'button', class: 'cameras-tilex', title: 'Remove from grid', 'aria-label': 'Remove from grid' }, '×');
        rm.addEventListener('click', () => { removeFromGrid(ctx, cam.id); load(); });
        gone.append(rm);
      }
      return gone;
    }
    const t = { cam, view: null };
    // a direct camera's picture links to its source (its terms), so its caption opens the card
    const el = h('div', { class: `cameras-tile${cam.direct ? ' cameras-tile-direct' : ''}`, role: 'button', tabindex: '0',
      title: `${cam.name}${cam.subtitle ? `, ${cam.subtitle}` : ''}${cam.direct ? '. Click the name for its card, the picture for its source.' : ''}` });
    const cap = h('div', { class: 'cameras-cap' },
      h('span', { class: 'cameras-capname' }, cam.name),
      h('span', { class: 'cameras-capsub' }, cam.subtitle || ''));
    el.append(cap);
    if (cam.kind === 'hls' || cam.kind === 'mjpeg') el.append(h('span', { class: 'cameras-pill', title: 'Live video on its card' }, 'Live'));
    // stagger first loads so a 4x4 grid does not ask for 16 images at once
    setTimeout(() => {
      if (closed || !el.isConnected) return;
      t.view = createView(ctx, { ...cam, attribution: '' }, { tile: true });
      el.prepend(t.view.el);
    }, i * 90);
    const open = () => { m.close(); ctx.entities.open(entityOf(cam)); };
    el.addEventListener('click', (e) => { if (!e.target.closest('.cameras-tilex, a')) open(); });
    el.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
    if (st.mode === 'mine') {
      const rm = h('button', { type: 'button', class: 'cameras-tilex', title: 'Remove from grid', 'aria-label': `Remove ${cam.name} from grid` }, '×');
      rm.addEventListener('click', (e) => { e.stopPropagation(); removeFromGrid(ctx, cam.id); items = items.filter(x => x.id !== cam.id); render(); });
      el.append(rm);
    }
    tiles.push(t);
    return el;
  }

  async function byIds(ids) {
    if (!ids.length) return [];
    const res = await ctx.api.get('/api/cameras/list', { ids: ids.slice(0, 100).join(',') });
    return res.cameras || [];
  }

  async function load() {
    const my = ++seq;
    renderBar();
    clearTiles();
    wall.append(h('p', { class: 'dim cameras-loadingtext' }, 'Loading cameras…'));
    try {
      let list = [];
      total = 0;
      if (st.mode === 'mine') {
        const mine = gridList(ctx);
        list = await byIds(mine.map(x => x.id));
        list = list.map(c => (c.missing ? { ...c, title: mine.find(x => x.id === c.id)?.title } : c));
      } else if (st.mode === 'favourites') {
        const favs = ctx.favourites.list('camera');
        list = await byIds(favs.map(f => f.ref));
        list = list.map(c => (c.missing ? { ...c, title: favs.find(f => f.ref === c.id)?.title } : c));
      } else {
        const [w, s, e, n] = ctx.map2.viewBounds();
        const c = ctx.map.getCenter();
        const res = await ctx.api.get('/api/cameras/list', { bbox: `${w},${s},${e},${n}`, lat: c.lat.toFixed(5), lon: c.lng.toFixed(5), limit: 144 });
        list = res.cameras || [];
        total = res.total || list.length;
      }
      if (my !== seq || closed) return;
      items = list;
      render();
    } catch (err) {
      if (my !== seq || closed) return;
      items = [];
      empty(err?.status === 503 ? 'The camera list is still being built. Try again in a minute.' : 'Cameras could not be loaded right now.');
    }
  }

  const m = ctx.ui.modal({ title: 'Camera grid', body: root, full: true, onClose: () => { closed = true; clearTiles(); offFav(); offGrid(); } });
  m.el.classList.add('cameras-grid-modal');
  m.el.addEventListener('keydown', (e) => {
    if (e.target.closest('input, select, textarea')) return;
    const per = st.size * st.size;
    if (e.key === 'ArrowRight' && (st.page + 1) * per < items.length) { st.page += 1; render(); }
    if (e.key === 'ArrowLeft' && st.page > 0) { st.page -= 1; render(); }
  });
  const offFav = ctx.favourites.onChange(() => { if (st.mode === 'favourites' && !closed) load(); });
  const offGrid = ctx.bus.on('cameras:grid-changed', () => { if (st.mode === 'mine' && !closed && !tiles.length) load(); });
  load();
  return m;
}
