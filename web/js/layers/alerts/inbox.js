// Inbox tab: fired alerts newest first, read and unread, digests that expand, mark read and clear.

import { age, toneOf } from './logic.js';

export function inboxView(ctx, state, { onNewWatchlist }) {
  const { h, dom, ui } = ctx;
  const root = h('div', { class: 'alerts-inbox' });
  let items = [];
  let nextBefore = null;            // {id, t}: the last item's place in the order
  let signature = '';
  let filter = ctx.settings.get('alerts.inboxFilter', 'all');
  let loaded = false;
  let error = null;
  const open = new Map();          // digest id -> folded alerts (or 'loading')

  async function load({ quiet = false } = {}) {
    if (!quiet && !loaded) { dom.clear(root).append(h('p', { class: 'alerts-empty' }, 'Loading…')); }
    try {
      const limit = quiet ? Math.min(200, Math.max(30, items.length)) : 30;   // a quiet refresh keeps older pages
      const r = await state.inbox({ limit, unread: filter === 'unread' ? 1 : null });
      const sig = (r.alerts || []).map(a => `${a.id}:${a.read ? 1 : 0}:${a.updated_at}`).join(',');
      items = r.alerts || [];
      nextBefore = r.next_before ? { id: r.next_before, t: r.next_before_t } : null;
      state.setCounts(r);
      const same = quiet && loaded && !error && sig === signature;
      signature = sig;
      error = null;
      loaded = true;
      if (same) return;                          // nothing new: keep the list (and keyboard focus) as it is
    } catch (err) {
      error = err?.message || 'no answer';
    }
    render();
  }

  async function older() {
    if (!nextBefore) return;
    try {
      const r = await state.inbox({ limit: 30, before: nextBefore.id, before_t: nextBefore.t, unread: filter === 'unread' ? 1 : null });
      const have = new Set(items.map(a => a.id));
      items = items.concat((r.alerts || []).filter(a => !have.has(a.id)));
      nextBefore = r.next_before ? { id: r.next_before, t: r.next_before_t } : null;
      signature = '';
    } catch (err) {
      ui.toast(`Older alerts are unavailable right now (${err?.message || 'no answer'}).`, 'red');
    }
    render();
  }

  function setRead(a, read) {
    a.read = read;
    render();
    (read ? state.markRead([a.id]) : state.markUnread([a.id])).catch(() => ui.toast('That change was not saved.', 'red'));
  }

  async function toggleDigest(a) {
    if (open.has(a.id)) { open.delete(a.id); render(); return; }
    open.set(a.id, 'loading');
    render();
    try {
      const r = await state.inbox({ digest: a.id, limit: 100 });
      open.set(a.id, r.alerts || []);
    } catch {
      open.set(a.id, []);
    }
    render();
  }

  function activate(a) {
    if (!a.read) setRead(a, true);
    if (a.digest) { toggleDigest(a); return; }
    if (a.entity) ctx.entities.open(a.entity);
  }

  function row(a, { nested = false } = {}) {
    const tone = a.reason === 'test' ? 'grey' : toneOf(a.severity);
    const sev = a.digest ? `+${a.count}` : a.reason === 'test' ? 'T' : ctx.fmt.num(a.severity, 1);
    const meta = [a.area && !nested ? a.area : null, age(a.time)].filter(Boolean).join(', ');
    const main = h('button', { type: 'button', class: 'alerts-item', title: a.entity ? 'Open on the map' : (a.digest ? 'Show the alerts held back' : '') },
      h('span', { class: `alerts-sev ${tone}`, 'aria-label': a.digest ? `${a.count} alerts` : a.reason === 'test' ? 'Test' : `Severity ${a.severity}` }, sev),
      h('span', { class: 'alerts-text' },
        h('span', { class: 'alerts-title' }, a.title),
        a.why ? h('span', { class: 'alerts-why' }, a.why) : null,
        h('span', { class: 'alerts-meta' }, meta, a.digest ? h('span', { class: 'alerts-more' }, open.has(a.id) ? 'Hide them' : 'Show them') : null)));
    main.addEventListener('click', () => activate(a));
    const dot = nested ? null : h('button', { type: 'button', class: 'alerts-dot', 'aria-label': a.read ? 'Mark as unread' : 'Mark as read',
      title: a.read ? 'Mark as unread' : 'Mark as read', 'aria-pressed': String(!a.read) });
    dot?.addEventListener('click', () => setRead(a, !a.read));
    const li = h('li', { class: `alerts-row${a.read ? '' : ' unread'}${a.digest ? ' digest' : ''}${nested ? ' nested' : ''}` }, main, dot);
    if (a.digest && open.has(a.id)) {
      const folded = open.get(a.id);
      const sub = h('ul', { class: 'alerts-list alerts-folded' });
      if (folded === 'loading') sub.append(h('li', { class: 'alerts-empty small' }, 'Loading…'));
      else if (!folded.length) sub.append(h('li', { class: 'alerts-empty small' }, 'Nothing to show.'));
      else for (const f of folded) sub.append(row(f, { nested: true }));
      li.append(sub);
    }
    return li;
  }

  function bar() {
    const c = state.counts || {};
    const seg = h('div', { class: 'seg alerts-seg', role: 'group', 'aria-label': 'Show' });
    for (const [id, label] of [['all', 'All'], ['unread', 'Unread']]) {
      const b = h('button', { type: 'button', 'aria-pressed': String(filter === id) }, label);
      b.addEventListener('click', () => { if (filter === id) return; filter = id; ctx.settings.set('alerts.inboxFilter', id); loaded = false; load(); });
      seg.append(b);
    }
    const readAll = h('button', { type: 'button', class: 'text-btn', disabled: !c.unread || null }, 'Mark all read');
    readAll.addEventListener('click', async () => {
      try { await state.readAll(); for (const a of items) a.read = true; render(); } catch { ui.toast('That change was not saved.', 'red'); }
    });
    const clearBtn = h('button', { type: 'button', class: 'text-btn', disabled: !c.inbox || null }, 'Clear');
    clearBtn.addEventListener('click', async () => {
      const ok = await ui.confirm('Clear every alert from the inbox? The daily briefing can still use them for 60 days.', { ok: 'Clear', danger: true });
      if (!ok) return;
      try { await state.clear(); items = []; nextBefore = null; render(); } catch { ui.toast('The inbox was not cleared.', 'red'); }
    });
    const count = c.inbox ? `${ctx.fmt.int(c.unread)} unread of ${ctx.fmt.int(c.inbox)}` : 'Inbox';
    return h('div', { class: 'alerts-bar' }, h('span', { class: 'alerts-count tnum' }, count), seg, h('span', { class: 'alerts-spacer' }), readAll, clearBtn);
  }

  function empty() {
    if (!state.watchlists.length) {
      const b = h('button', { type: 'button', class: 'btn primary' }, 'New watchlist');
      b.addEventListener('click', () => onNewWatchlist());
      return h('div', { class: 'alerts-empty' }, h('p', null, 'No watchlists yet. Draw an area to start.'), b);
    }
    if (filter === 'unread') return h('div', { class: 'alerts-empty' }, h('p', null, 'Nothing unread.'));
    return h('div', { class: 'alerts-empty' }, h('p', null, 'No alerts yet. Meridian checks your watchlists every two minutes and lists anything new here.'));
  }

  function render() {
    dom.clear(root);
    root.append(bar());
    if (error && !items.length) {
      root.append(h('p', { class: 'alerts-empty' }, `The inbox is unavailable right now (${error}).`));
      return;
    }
    if (!items.length) { root.append(empty()); return; }
    const list = h('ul', { class: 'alerts-list', 'aria-label': 'Alerts' });
    for (const a of items) list.append(row(a));
    root.append(list);
    if (nextBefore) {
      const more = h('button', { type: 'button', class: 'text-btn alerts-older' }, 'Show older alerts');
      more.addEventListener('click', older);
      root.append(more);
    }
  }

  return { el: root, load, render };
}
