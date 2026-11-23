// The "Watchlist areas" layer in the Saved group: each watchlist's area through ctx.draw.show.

export const LAYER_ID = 'alerts.areas';

export function areasLayer(ctx, state) {
  const shown = new Set();
  let on = false;

  function render() {
    const want = new Map();
    if (on) {
      for (const w of state.watchlists) {
        if (w.show_on_map && w.area?.geometry && w.area.type !== 'world') want.set(`alerts:${w.id}`, w);
      }
    }
    for (const id of [...shown]) {
      if (!want.has(id)) { ctx.draw.hide(id); shown.delete(id); }
    }
    for (const [id, w] of want) {
      try {
        ctx.draw.show(id, w.area.geometry, { color: w.color, label: w.enabled ? w.name : `${w.name} (paused)` });
        shown.add(id);
      } catch (err) {
        console.warn(`alerts: watchlist ${w.id} cannot be drawn`, err);
      }
    }
    layer.setCount(on ? want.size : null);
    if (on && state.loaded) {
      if (state.watchlists.length) layer.setStatus('ok');
      else layer.setStatus('warn', 'No watchlists yet. Open Alerts in the toolbar to draw one.');
    }
  }

  const layer = ctx.layers.register({
    id: LAYER_ID, group: 'Saved', title: 'Watchlist areas', symbol: 'area', color: '#6cb6ff',
    description: 'The areas your alerts watch. Open Alerts in the toolbar to add or change them.',
    source: 'Your watchlists',
    async enable() {
      on = true;
      if (!state.loaded) await state.loadWatchlists();
      render();
    },
    async disable() {
      on = false;
      render();
    },
  });

  state.on((what) => { if (what === 'watchlists' || what === 'all') render(); });
  return { layer, render };
}
