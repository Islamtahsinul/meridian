// Workspaces (browser half): saved map states with built-in presets, My places, "Copy link to this
// view" and "Export what's on screen". A "Workspaces" toolbar button (shortcut w) opens a drawer with
// three tabs; the deep links themselves live in the core (web/js/core/deeplink.js, ctx.deeplink).
// Backend: meridian/modules/workspace. Browser parts: ./workspace/*.js. See docs/modules/workspace.md.

import { createPlaces } from './workspace/places.js';
import { createPanel } from './workspace/panel.js';

const ICON = 'M5.5 3.5h9a1 1 0 0 1 1 1v12.2l-5.5-3.3-5.5 3.3V4.5a1 1 0 0 1 1-1ZM7.5 7.5h5M7.5 10h3';

export async function register(ctx) {
  const { ui } = ctx;
  const backend = ctx.moduleOk('workspace');
  if (!backend) console.warn('workspace: the backend module is not loaded; saved workspaces and places are unavailable');
  const places = createPlaces(ctx, { backend });
  const panel = createPanel(ctx, { backend, places });

  const btn = ui.toolbarButton({ id: 'workspace', title: 'Workspaces, my places, links and exports (W)', label: 'Workspaces', icon: ICON,
    order: 78, onClick: () => panel.toggle() });
  panel.onOpenChange((open) => btn.setAttribute('aria-pressed', String(open)));
  const taken = new Set(ui.listShortcuts().map(s => s.key));
  if (!taken.has('w')) ui.shortcut('w', 'Workspaces, my places, links and exports', () => panel.toggle());

  if (backend) places.load().catch(() => {});   // for the search box and my_place cards

  // For tests and other modules: the current state, and applying one (same as the drawer does).
  ctx.workspace = { current: panel.current, apply: (state, opts) => panel.apply(state, opts), open: panel.show, places };
}
