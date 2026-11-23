// Shared state and API calls for the Alerts front end (backend: meridian/modules/alerts).

export function createState(ctx) {
  const { api } = ctx;
  const listeners = new Set();
  const s = {
    presets: [],
    watchlists: [],
    settings: null,
    counts: { unread: 0, inbox: 0, latest_id: 0, watchlists: 0 },
    loaded: false,
    on(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    emit(what) { for (const fn of [...listeners]) { try { fn(what); } catch (err) { console.error('alerts listener failed', err); } } },
    preset(id) { return s.presets.find(p => p.id === id) || null; },
    watchlist(id) { return s.watchlists.find(w => w.id === id) || null; },

    async loadAll() {
      const [p, w, st] = await Promise.all([api.get('/api/alerts/presets'), api.get('/api/alerts/watchlists'), api.get('/api/alerts/settings')]);
      s.presets = p.presets || [];
      s.watchlists = w.watchlists || [];
      s.settings = st;
      s.loaded = true;
      s.emit('all');
    },
    async loadWatchlists() {
      s.watchlists = (await api.get('/api/alerts/watchlists')).watchlists || [];
      s.emit('watchlists');
      return s.watchlists;
    },
    async loadCounts() {
      const c = await api.get('/api/alerts/counts');
      const changed = c.unread !== s.counts.unread || c.latest_id !== s.counts.latest_id || c.latest_at !== s.counts.latest_at;
      s.counts = c;
      if (changed) s.emit('counts');
      return c;
    },
    setCounts(c) {
      if (!c || typeof c.unread !== 'number') return;
      s.counts = { ...s.counts, ...c };
      s.emit('counts');
    },

    // -- watchlists and rules
    async create(body) {
      const r = await api.post('/api/alerts/watchlists', { action: 'create', ...body });
      await s.loadWatchlists();
      return r.watchlist;
    },
    async update(id, change) {
      const r = await api.post('/api/alerts/watchlists', { action: 'update', id, ...change });
      replace(r.watchlist);
      return r.watchlist;
    },
    async remove(id) {
      const r = await api.post('/api/alerts/watchlists', { action: 'delete', id });
      await s.loadWatchlists();
      await s.loadCounts().catch(() => {});
      return r;
    },
    async setRule(watchlistId, preset, change) {
      const r = await api.post('/api/alerts/rules', { action: 'set', watchlist_id: watchlistId, preset, ...change });
      replace(r.watchlist);
      return r.watchlist;
    },
    matches(id, limit = 8) { return api.get(`/api/alerts/watchlists/${encodeURIComponent(id)}/matches`, { limit }); },

    // -- inbox
    inbox(params) { return api.get('/api/alerts/inbox', params); },
    async inboxAction(body) {
      const r = await api.post('/api/alerts/inbox', body);
      s.setCounts(r);
      return r;
    },
    markRead(ids) { return s.inboxAction({ action: 'read', ids }); },
    markUnread(ids) { return s.inboxAction({ action: 'unread', ids }); },
    readAll() { return s.inboxAction({ action: 'read_all' }); },
    clear() { return s.inboxAction({ action: 'clear' }); },
    test() { return s.inboxAction({ action: 'test' }); },
    async check() {
      const r = await api.post('/api/alerts/check', {});
      s.setCounts(r);
      return r;
    },
    status() { return api.get('/api/alerts/status'); },

    // -- settings
    async saveSettings(change) {
      s.settings = await api.post('/api/alerts/settings', change);
      s.emit('settings');
      return s.settings;
    },
  };

  function replace(w) {
    if (!w) return;
    const i = s.watchlists.findIndex(x => x.id === w.id);
    if (i >= 0) s.watchlists[i] = w; else s.watchlists.push(w);
    s.emit('watchlists');
  }

  return s;
}
