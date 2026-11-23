// Alerts: watchlists (named areas with rules) and the alerts they fire. The toolbar button carries the
// unread count and opens a drawer with Inbox, Watchlists and Settings. Every 45 seconds the page asks the
// server for new alerts and announces them with ctx.notify (CONTRACT.md 15.3). Backend:
// meridian/modules/alerts; browser parts in ./alerts/*.js.

import { createState } from './alerts/state.js';
import { inboxView } from './alerts/inbox.js';
import { watchlistsView } from './alerts/watchlists.js';
import { settingsView } from './alerts/settings.js';
import { areasLayer } from './alerts/map.js';
import { POLL_MS, plan, toneOf, tabTarget } from './alerts/logic.js';

const BELL = 'M10 2.6a4.7 4.7 0 0 0-4.7 4.7v3.1L3.6 13.6h12.8l-1.7-3.2V7.3A4.7 4.7 0 0 0 10 2.6ZM7.9 15.7a2.1 2.1 0 0 0 4.2 0';
const TABS = [['inbox', 'Inbox'], ['watchlists', 'Watchlists'], ['settings', 'Settings']];
const LAST_KEY = 'alerts.lastNotified';

export async function register(ctx) {
  const { h, dom, ui } = ctx;
  const state = createState(ctx);
  const badge = h('span', { class: 'alerts-badge', hidden: true, 'aria-hidden': 'true' });
  const btn = ui.toolbarButton({ id: 'alerts', title: 'Alerts and watchlists', label: 'Alerts', icon: BELL, order: 15, onClick: () => toggle() });
  btn.append(badge);

  if (!ctx.moduleOk('alerts')) {
    console.warn('alerts: the backend module is not loaded; watchlists and alerts are unavailable');
    btn.title = 'Alerts are unavailable: the alerts module is not running';
    btn.addEventListener('click', () => ui.toast('Alerts are unavailable: the alerts module is not running on the server.', 'amber'));
    return;
  }

  const layer = areasLayer(ctx, state);
  let drawer = null;
  let tab = ctx.settings.get('alerts.tab', 'inbox');
  if (!TABS.some(([id]) => id === tab)) tab = 'inbox';

  const inbox = inboxView(ctx, state, { onNewWatchlist: () => { show('watchlists'); } });
  const watchlists = watchlistsView(ctx, state, {
    onSaved: () => { if (!ctx.layers.isOn(layer.layer.id)) ctx.layers.setOn(layer.layer.id, true); },
    onDrawing: (on) => drawer?.el.classList.toggle('alerts-drawing-now', on),
  });
  const settings = settingsView(ctx, state);
  const tabBar = h('div', { class: 'alerts-tabs', role: 'tablist', 'aria-label': 'Alerts' });
  const panel = h('div', { class: 'alerts-panel', role: 'tabpanel', id: 'alerts-tabpanel' });
  // WAI-ARIA tabs: one Tab stop (the selected tab); arrow keys, Home and End move and select.
  tabBar.addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const i = tabTarget(e.key, TABS.findIndex(([id]) => id === tab), TABS.length);
    if (i == null) return;
    e.preventDefault();
    show(TABS[i][0]);
    tabBar.querySelector(`#alerts-tab-${TABS[i][0]}`)?.focus();
  });
  const root = h('div', { class: 'alerts-drawer' }, tabBar, panel);

  function renderTabs() {
    const hadFocus = tabBar.contains(document.activeElement);
    dom.clear(tabBar);
    for (const [id, label] of TABS) {
      const extra = id === 'inbox' && state.counts.unread ? h('span', { class: 'alerts-tab-count tnum' }, ctx.fmt.int(state.counts.unread))
        : id === 'watchlists' && state.watchlists.length ? h('span', { class: 'alerts-tab-count tnum faint' }, ctx.fmt.int(state.watchlists.length)) : null;
      const b = h('button', { type: 'button', role: 'tab', class: 'alerts-tab', 'aria-selected': String(id === tab), id: `alerts-tab-${id}`,
        'aria-controls': 'alerts-tabpanel', tabindex: id === tab ? '0' : '-1' }, label, extra);
      b.addEventListener('click', () => show(id));
      tabBar.append(b);
    }
    panel.setAttribute('aria-labelledby', `alerts-tab-${tab}`);
    if (hadFocus) tabBar.querySelector(`#alerts-tab-${tab}`)?.focus();   // a count update must not drop focus
  }

  function show(id) {
    tab = id;
    ctx.settings.set('alerts.tab', id);
    if (!drawer) { open(); return; }
    renderTabs();
    dom.clear(panel);
    panel.scrollTop = 0;
    if (id === 'inbox') { panel.append(inbox.el); inbox.load(); }
    else if (id === 'watchlists') { panel.append(watchlists.el); watchlists.render(); state.loadWatchlists().catch(() => {}); }
    else { panel.append(settings.el); settings.render(); settings.load(); }
  }

  function open() {
    if (drawer) return;
    drawer = ui.drawer({ id: 'alerts', title: 'Alerts', el: root, width: 440, onClose: () => {
      drawer = null;
      watchlists.reset();
      btn.setAttribute('aria-pressed', 'false');
    } });
    btn.setAttribute('aria-pressed', 'true');
    show(tab);
    poll();
  }

  function toggle() {
    if (drawer) drawer.close(); else open();
  }

  function renderBadge() {
    const n = state.counts.unread || 0;
    badge.hidden = !n;
    badge.textContent = n > 99 ? '99+' : String(n);
    btn.setAttribute('aria-label', n ? `Alerts, ${n} unread` : 'Alerts');
    btn.title = n ? `Alerts and watchlists: ${n} unread` : 'Alerts and watchlists';
  }

  state.on((what) => {
    if (what === 'counts') { renderBadge(); if (drawer) renderTabs(); }
    if (drawer && (what === 'watchlists' || what === 'all')) {
      renderTabs();
      if (tab === 'watchlists') watchlists.render();
    }
    if (drawer && what === 'settings' && tab === 'settings') settings.render();
  });

  // ------------------------------------------------------------------ polling and notifications
  let lastNotified = ctx.settings.get(LAST_KEY, null);
  let polling = false;

  function announce(alerts) {
    const p = plan(alerts, state.settings);
    for (const a of p.each) {
      try {
        ctx.notify.send({ title: a.title, body: a.why || a.area, tag: `alerts:${a.id}`, tone: toneOf(a.severity), entity: a.entity || undefined });
      } catch (err) { console.warn('alerts: notification failed', err); }
    }
    if (p.more) {
      const maxId = Math.max(...alerts.map(a => a.id));
      try {
        ctx.notify.send({ title: `${p.more} more new alert${p.more === 1 ? '' : 's'}`, body: 'Open Alerts in the toolbar to see them all.',
          tag: `alerts:more:${maxId}`, tone: toneOf(p.top?.severity) });
      } catch (err) { console.warn('alerts: notification failed', err); }
    }
  }

  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const c = await state.loadCounts();
      const latest = c.latest_id || 0;
      if (lastNotified == null || lastNotified > latest) {
        lastNotified = latest;                       // first visit (or a fresh database): nothing old pops up
        ctx.settings.set(LAST_KEY, lastNotified);
      } else if (latest > lastNotified) {
        const r = await state.inbox({ after: lastNotified, limit: 50 });
        if (!state.settings) { try { await state.loadAll(); } catch { /* announce with defaults */ } }
        announce(r.alerts || []);
        lastNotified = latest;
        ctx.settings.set(LAST_KEY, lastNotified);
      }
      if (drawer && tab === 'inbox') inbox.load({ quiet: true });
    } catch {
      // the server is away for a moment: try again on the next tick
    } finally {
      polling = false;
    }
  }

  ctx.bus.on('alerts:poll', () => poll());
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') poll(); });
  setInterval(poll, POLL_MS);

  renderBadge();
  // Not awaited: the other modules' front ends should not wait for ours.
  state.loadAll().catch((err) => console.warn('alerts: could not load watchlists', err)).finally(() => poll());
}
