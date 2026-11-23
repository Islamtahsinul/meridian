// Bootstrap (CONTRACT.md section 10.1): build the map and the shell, then load every module's
// /js/layers/<module>.js and call its register(ctx).

import { bus } from './bus.js';
import * as dom from './dom.js';
import { api } from './api.js';
import * as mapx from './map.js';
import { layers } from './layers.js';
import * as layerPanel from './layers.js';
import * as cards from './cards.js';
import { entities } from './entities.js';
import { search } from './search.js';
import * as searchMod from './search.js';
import { ui } from './ui.js';
import { favourites, casefiles, openFavourites, openCasefiles, registerSavedLayers } from './saved.js';
import { whatshere } from './whatshere.js';
import * as status from './status.js';
import { draw } from './draw.js';
import { notify, mountSwitch as mountNotifySwitch } from './notify.js';
import { time } from './time.js';
import * as deeplink from './deeplink.js';

const aiActions = new Map();
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function startClock() {
  // The top-bar clock shows the time the map shows: UTC now, or the replay time (ctx.time) in amber.
  const el = document.getElementById('clock');
  const tick = () => {
    const replay = time.mode() === 'replay';
    const now = new Date(time.now());
    dom.clear(el);
    el.append(dom.fmt.clock(now), dom.h('small', null, replay ? `UTC, ${now.getUTCDate()} ${MONTHS[now.getUTCMonth()]}` : 'UTC'));
    el.classList.toggle('replay', replay);
    el.setAttribute('datetime', now.toISOString());
    el.setAttribute('aria-label', replay ? 'Replay time, UTC' : 'Coordinated Universal Time');
    el.title = replay ? `Replay time, ${dom.fmt.utc(now)}` : `Your local time: ${now.toLocaleTimeString()}`;
  };
  tick();
  setInterval(tick, 1000);
  bus.on('time:changed', tick);
}

function wireShell() {
  const app = document.getElementById('app');
  const btn = document.getElementById('toggle-left');
  const small = () => window.matchMedia('(max-width: 860px)').matches;
  const saved = localStorage.getItem('meridian.left') === 'closed';
  if (saved && !small()) { app.classList.add('left-collapsed'); btn.setAttribute('aria-expanded', 'false'); }
  btn.addEventListener('click', () => {
    if (small()) {
      app.classList.toggle('left-open');
      btn.setAttribute('aria-expanded', String(app.classList.contains('left-open')));
    } else {
      const collapsed = app.classList.toggle('left-collapsed');
      btn.setAttribute('aria-expanded', String(!collapsed));
      localStorage.setItem('meridian.left', collapsed ? 'closed' : 'open');
    }
    setTimeout(() => mapx.getMap()?.resize(), 50);
  });
  document.getElementById('open-layer-table').addEventListener('click', status.openLayerTable);
  document.getElementById('open-sources').addEventListener('click', status.openStatus);
}

function registerCoreActions(ctx) {
  // Map actions the AI assistant (and anything else) can run: ctx.ai.run({type, ...}).
  ctx.ai.register('fly_to', ({ lat, lon, zoom }) => mapx.flyTo(+lat, +lon, zoom != null ? +zoom : undefined));
  ctx.ai.register('fit_bounds', ({ west, south, east, north }) => mapx.fitBbox([+west, +south, +east, +north]));
  ctx.ai.register('set_layers', async ({ on = [], off = [] }) => {
    // Only layers that exist, at most 8 on and 20 off per action: limits what prompt-injected
    // tool data could do to the map.
    const known = (list, max) => [...new Set((Array.isArray(list) ? list : []).map(String))].filter(id => layers.get(id)).slice(0, max);
    for (const id of known(off, 20)) await layers.setOn(id, false);
    for (const id of known(on, 8)) await layers.setOn(id, true);
  });
  ctx.ai.register('set_basemap', ({ id }) => mapx.setBasemap(id));
  ctx.ai.register('set_projection', ({ projection, value }) => mapx.setProjection(projection ?? value));
  ctx.ai.register('set_terrain', ({ on }) => mapx.setTerrain(!!on));
  ctx.ai.register('open_entity', ({ entity, type, ...rest }) => entities.open(entity || rest));
  ctx.ai.register('whats_here', ({ lat, lon }) => whatshere.open(+lat, +lon));
  ctx.ai.register('highlight', ({ lat, lon }) => mapx.highlight(+lat, +lon));
}

async function main() {
  startClock();
  wireShell();
  let manifest = { modules: [], layers: [], keys: [] };
  try { manifest = await api.get('/api/core/manifest'); } catch (err) { ui.toast('The Meridian server is not answering.', 'red', 8000); }
  const map = await mapx.createMap(document.getElementById('map'));
  layerPanel.mountPanel(document.getElementById('layer-groups'), document.getElementById('layer-filter'), document.getElementById('layers-off'));
  await favourites.load();
  cards.mount(document.getElementById('cards'), { favourites, casefiles, entities, whatshere });
  searchMod.mount(document.getElementById('search'));
  whatshere.mount();
  mapx.buildViewControls(document.getElementById('view-controls'), { openMenu: ui.openMenu });

  const ctx = {
    map,
    maplibregl: window.maplibregl,
    manifest,
    api,
    bus,
    dom,
    h: dom.h,
    fmt: dom.fmt,
    layers,
    cards: { open: cards.open, close: cards.close, closeAll: cards.closeAll, get: cards.get, loadHls: cards.loadHls },
    entities,
    search,
    whatshere,
    favourites,
    casefiles,
    ui,
    map2: {
      addSource: mapx.addSource, addLayer: mapx.addLayer, removeLayer: mapx.removeLayer, removeSource: mapx.removeSource,
      setVisible: mapx.setVisible, setData: mapx.setData, emptyFC: mapx.emptyFC, icon: mapx.icon, onClick: mapx.onClick,
      onHover: mapx.onHover, flyTo: mapx.flyTo, fitBbox: mapx.fitBbox, viewBounds: mapx.viewBounds, highlight: mapx.highlight,
      clearHighlight: mapx.clearHighlight, setBasemap: mapx.setBasemap, setProjection: mapx.setProjection,
      setTerrain: mapx.setTerrain, viewState: mapx.viewState, reducedMotion: mapx.reducedMotion, SLOTS: mapx.SLOTS, BASEMAPS: mapx.BASEMAPS,
    },
    ai: {
      register(name, fn) { aiActions.set(name, fn); },
      actions() { return [...aiActions.keys()]; },
      async run(action) {
        const fn = aiActions.get(action?.type);
        if (!fn) throw new Error(`Unknown map action ${action?.type}`);
        return fn(action);
      },
    },
    settings: {
      get(key, def = null) { try { const v = localStorage.getItem('meridian.s.' + key); return v == null ? def : JSON.parse(v); } catch { return def; } },
      set(key, value) { try { localStorage.setItem('meridian.s.' + key, JSON.stringify(value)); } catch { /* ignore */ } },
    },
    moduleOk(name) { return !!manifest.modules.find(m => m.name === name && m.ok && m.backend); },
    // CONTRACT 15.3: drawing, notifications and the replay clock.
    draw: { start: draw.start, cancel: draw.cancel, active: draw.active, show: draw.show, hide: draw.hide, contains: draw.contains },
    notify: { permission: notify.permission, request: notify.request, enabled: notify.enabled, setEnabled: notify.setEnabled, send: notify.send },
    time: { mode: time.mode, now: time.now, setReplay: time.setReplay, live: time.live },
    // Deep links (web/js/core/deeplink.js, docs/modules/workspace.md): map=, l= and e= in the URL hash.
    deeplink: { link: deeplink.linkFor, copy: deeplink.copyLink, parse: deeplink.parseHash, build: deeplink.buildHash },
  };
  window.__meridian = ctx;   // test and debugging handle (tools/browser_check.mjs uses it)
  try { deeplink.init({ map, layers, entities, ui, dom, bus }); } catch (err) { console.error('deep links failed to start', err); }
  notify.configure({ toast: ui.toast, openEntity: (entity) => entities.open(entity) });
  try {
    mountNotifySwitch(document.getElementById('statusbar'), document.getElementById('status-summary'), { toast: ui.toast });
  } catch (err) { console.error('the notifications switch failed to start', err); }
  registerCoreActions(ctx);
  registerSavedLayers();

  ui.toolbarButton({ id: 'favourites', title: 'Favourites', icon: 'M10 2.8l2.2 4.6 5 .7-3.6 3.5.9 5-4.5-2.4-4.5 2.4.9-5L2.8 8.1l5-.7L10 2.8Z', onClick: openFavourites, order: 80 });
  ui.toolbarButton({ id: 'casefiles', title: 'Case files', icon: 'M3 5.5h5l1.5 2H17v8.5H3V5.5Z', onClick: openCasefiles, order: 81 });
  ui.toolbarButton({ id: 'help', title: 'Keyboard shortcuts', icon: 'M7.5 7.5a2.5 2.5 0 1 1 3.5 2.3c-.7.3-1 .9-1 1.6v.6M10 14.5v.5', onClick: status.openShortcuts, order: 99 });
  ui.shortcut('g', 'Switch between flat map and globe', () => mapx.setProjection(mapx.viewState().projection === 'globe' ? 'mercator' : 'globe'));
  ui.shortcut('l', 'Show or hide the layer panel', () => document.getElementById('toggle-left').click());
  ui.shortcut('t', '3D terrain on or off', () => mapx.setTerrain(!mapx.viewState().terrain));
  ui.shortcut('?', 'Keyboard shortcuts', status.openShortcuts);

  // Module front ends, in manifest order. One failing module never stops the others.
  const failures = [];
  for (const mod of manifest.modules) {
    if (mod.css) document.head.append(dom.h('link', { rel: 'stylesheet', href: mod.css }));
  }
  for (const mod of manifest.modules) {
    if (!mod.js) continue;
    try {
      const m = await import(mod.js);
      if (typeof m.register !== 'function') throw new Error('no register(ctx) export');
      await m.register(ctx);
    } catch (err) {
      console.error(`module ${mod.name} failed to start in the browser`, err);
      failures.push(mod.name);
    }
  }
  if (failures.length) ui.toast(`Some modules failed to start: ${failures.join(', ')}. See Sources and status.`, 'red', 8000);
  status.startStatusPolling();
  deeplink.start().catch((err) => console.error('the deep link could not be applied', err));
  bus.emit('app:ready', { ctx });
  document.body.dataset.ready = '1';
}

main().catch((err) => {
  console.error('Meridian failed to start', err);
  document.body.dataset.ready = 'error';
  const hint = document.getElementById('map-hint');
  if (hint) { hint.hidden = false; hint.textContent = `Meridian failed to start: ${err?.message || err}`; }
});
