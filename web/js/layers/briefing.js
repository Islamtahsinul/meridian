// Daily briefing (browser half): a "Briefing" toolbar button with a small dot while a new briefing is
// unread, and a wide drawer with the briefing (see ./briefing/panel.js). Shortcut: b.

import { createPanel } from './briefing/panel.js';

const ICON = 'M5.5 3h6.5l3 3v11h-9.5zM12 3v3h3M7.8 9.2h5M7.8 11.7h5M7.8 14.2h3.2';
const POLL_MS = 5 * 60 * 1000;
const SEEN_KEY = 'briefing.seen';

export async function register(ctx) {
  if (!ctx.moduleOk('briefing')) return;
  const { h, ui, api } = ctx;
  let latestId = null;

  const panel = createPanel(ctx, { onShown: (doc) => markSeen(doc.id) });
  const btn = ui.toolbarButton({ id: 'briefing', title: 'Daily briefing (B)', label: 'Briefing', icon: ICON, order: 9,
    onClick: () => panel.toggle() });
  btn.classList.add('briefing-btn');
  const dot = h('span', { class: 'briefing-dot', hidden: true });
  btn.append(dot);
  ui.shortcut('b', 'Open the daily briefing', () => (panel.isOpen() ? panel.close() : panel.open()));

  function paintDot() {
    const unread = !!latestId && latestId !== ctx.settings.get(SEEN_KEY, null);
    dot.hidden = !unread;
    btn.title = unread ? 'Daily briefing (B): a new briefing is ready' : 'Daily briefing (B)';
    btn.setAttribute('aria-label', unread ? 'Daily briefing, new briefing ready' : 'Daily briefing');
  }

  function markSeen(id) {
    if (!id) return;
    if (!latestId || id >= latestId) latestId = id;
    if (id === latestId) ctx.settings.set(SEEN_KEY, id);
    paintDot();
  }

  async function check() {
    if (document.visibilityState !== 'visible') return;
    try {
      const r = await api.get('/api/briefing/list', { limit: 1 });
      latestId = r.briefings && r.briefings[0] ? r.briefings[0].id : null;
    } catch { return; }
    if (panel.isOpen() && panel.current() && panel.current().id === latestId) markSeen(latestId);
    paintDot();
  }

  check();
  setInterval(check, POLL_MS);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') check(); });
  ctx.bus.on('briefing:check', check);
}
