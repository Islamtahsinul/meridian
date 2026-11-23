// The briefing drawer: latest or chosen briefing, previous briefings, "Generate now" with progress,
// the AI narrative (polled while it is being written), "Save as case file" and "Download Markdown".

import { itemRow, narrativeBlock, periodText, sectionBlock, sourcesBlock, utcDay, utcShort } from './render.js';
import { downloadMarkdown, saveAsCasefile } from './actions.js';

const WIDTH = 640;
const STATUS_POLL_MS = 1500;
const STATUS_POLL_MAX = 160;         // about 4 minutes
const AI_POLL_MS = 4000;
const AI_POLL_MAX = 45;
const COLLAPSED_KEY = 'briefing.collapsed';
const DEFAULT_COLLAPSED = ['space', 'cyber', 'markets'];
const PHASES = { history: 'Reading the event history…', 'live data': 'Asking the other modules for live data…',
  writing: 'Writing the briefing…', narrative: 'Writing the AI narrative…' };
const MORE_ICON = 'M3.9 10a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0M8.5 10a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0M13.1 10a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0';

export function createPanel(ctx, { onShown } = {}) {
  const { h, ui, api, dom } = ctx;
  let drawer = null;
  let els = null;
  let doc = null;               // the briefing on screen
  let list = [];                // previous briefings, newest first
  let generating = false;
  let aiTimer = null;
  let genTimer = null;
  const button = () => document.getElementById('tool-briefing');

  const collapsed = () => new Set(ctx.settings.get(COLLAPSED_KEY, DEFAULT_COLLAPSED) || []);
  function setCollapsed(id, isOpen) {
    const s = collapsed();
    if (isOpen) s.delete(id); else s.add(id);
    ctx.settings.set(COLLAPSED_KEY, [...s]);
  }

  // ---------------------------------------------------------------- the drawer
  function isOpen() { return !!drawer; }

  function open(id = null) {
    if (!drawer) {
      els = build();
      drawer = ui.drawer({ id: 'briefing', title: 'Briefing', el: els.root, width: WIDTH, tools: els.more, onClose() {
        clearTimeout(aiTimer); aiTimer = null;
        drawer = null; els = null;
        button()?.setAttribute('aria-pressed', 'false');
      } });
      drawer.el.classList.add('briefing-drawer');
      button()?.setAttribute('aria-pressed', 'true');
    }
    load(id);
  }

  function close() { drawer?.close(); }
  function toggle() { if (drawer) close(); else open(); }

  function build() {
    const select = h('select', { class: 'briefing-select', 'aria-label': 'Choose a briefing' });
    select.addEventListener('change', () => { if (select.value) load(select.value); });
    const gen = h('button', { class: 'btn primary briefing-gen', type: 'button', 'aria-haspopup': 'menu' }, 'Generate now');
    gen.addEventListener('click', () => ui.openMenu(gen, [
      { text: 'Last 24 hours', onClick: () => generate('24h') },
      { text: 'Since the previous briefing', onClick: () => generate('since_last') },
    ], { align: 'right' }));
    const progress = h('p', { class: 'briefing-progress', role: 'status', 'aria-live': 'polite', hidden: true });
    const more = h('button', { class: 'icon-btn', type: 'button', title: 'More', 'aria-label': 'More briefing actions', 'aria-haspopup': 'menu' }, dom.svgIcon(MORE_ICON, { stroke: false }));
    more.addEventListener('click', () => ui.openMenu(more, [
      { text: 'Save as case file', disabled: !doc, onClick: () => doc && saveAsCasefile(ctx, doc) },
      { text: 'Download Markdown', disabled: !doc, onClick: () => doc && downloadMarkdown(ctx, doc) },
    ]));
    const content = h('div', { class: 'briefing-content' });
    const bar = h('div', { class: 'briefing-bar' }, select, gen);
    const root = h('div', { class: 'briefing' }, bar, progress, content);
    return { root, select, gen, progress, content, more };
  }

  // ---------------------------------------------------------------- loading
  async function refreshList() {
    try { list = (await api.get('/api/briefing/list', { limit: 60 })).briefings || []; } catch { list = list || []; }
    paintSelect();
    return list;
  }

  function paintSelect() {
    if (!els) return;
    const { select } = els;
    dom.clear(select);
    if (!list.length) {
      select.append(h('option', { value: '' }, 'No briefings yet'));
      select.disabled = true;
      return;
    }
    select.disabled = false;
    list.forEach((b, i) => {
      const label = `${utcDay(b.time)}${b.trigger === 'scheduled' ? ', daily' : ', on request'}${i === 0 ? ' (latest)' : ''}`;
      select.append(h('option', { value: b.id }, label));
    });
    if (doc) select.value = doc.id;
  }

  async function load(id = null) {
    if (!els) return;
    const want = id || 'latest';
    if (!doc || want !== doc.id) paintMessage('Loading the briefing…');
    refreshList();
    let d;
    try {
      d = await api.get(`/api/briefing/${encodeURIComponent(want)}`);
    } catch (err) {
      if (!els) return;
      if (err.status === 404 && want === 'latest') return paintEmpty();
      return paintMessage(err.status === 404 ? 'That briefing is no longer kept.' : 'The briefing service is not answering right now.', true);
    }
    if (!els) return;
    show(d);
  }

  function show(d) {
    doc = d;
    clearTimeout(aiTimer); aiTimer = null;
    render();
    if (els) els.select.value = d.id;
    if (!list.length || list[0].id === d.id) onShown?.(d);
    if (d.ai_status === 'pending' && !d.ai) pollNarrative(d.id, 0);
  }

  function pollNarrative(id, n) {
    aiTimer = setTimeout(async () => {
      if (!els || !doc || doc.id !== id) return;
      let d;
      try { d = await api.get(`/api/briefing/${encodeURIComponent(id)}`); } catch { d = null; }
      if (!els || !doc || doc.id !== id) return;
      if (d && (d.ai || d.ai_status !== 'pending')) { doc = d; render(); return; }
      if (n + 1 < AI_POLL_MAX) pollNarrative(id, n + 1);
    }, AI_POLL_MS);
  }

  // ---------------------------------------------------------------- generating
  async function generate(period) {
    if (generating) return;
    generating = true;
    paintProgress('Starting…');
    try {
      await api.post('/api/briefing/generate', { period });
    } catch (err) {
      generating = false;
      paintProgress(null);
      ui.toast(err.message || 'The briefing could not be started.', 'red');
      return;
    }
    watch(0);
  }

  function watch(n) {
    clearTimeout(genTimer);
    genTimer = setTimeout(async () => {
      let st = null;
      try { st = await api.get('/api/briefing/status'); } catch { st = null; }
      if (st && st.running && st.phase !== 'narrative' && n < STATUS_POLL_MAX) {
        paintProgress(PHASES[st.phase] || 'Working…');
        return watch(n + 1);
      }
      generating = false;
      paintProgress(null);
      if (st && st.last && st.last.ok === false) {
        ui.toast(`The briefing failed: ${st.last.error || 'unknown error'}`, 'red', 8000);
        return;
      }
      if (els) { await refreshList(); load(st?.latest?.id || null); }
      else ui.toast('Your new briefing is ready.', 'green', 5000, { action: { text: 'Open', onClick: () => open() } });
      ctx.bus.emit('briefing:check', {});
    }, n === 0 ? 400 : STATUS_POLL_MS);
  }

  // ---------------------------------------------------------------- painting
  function paintProgress(text) {
    if (!els) return;
    els.gen.disabled = !!text;
    els.progress.hidden = !text;
    els.progress.textContent = text || '';
  }

  function paintMessage(text, retry = false) {
    if (!els) return;
    dom.clear(els.content);
    const p = h('p', { class: 'briefing-empty' }, text);
    els.content.append(p);
    if (retry) {
      const b = h('button', { class: 'btn', type: 'button' }, 'Try again');
      b.addEventListener('click', () => load(doc?.id || null));
      els.content.append(b);
    }
  }

  async function paintEmpty() {
    if (!els) return;
    dom.clear(els.content);
    doc = null;
    const when = h('p', { class: 'dim' }, 'Meridian makes one automatically once a day after the briefing time.');
    els.content.append(h('div', { class: 'briefing-intro' },
      h('p', null, 'No briefing yet.'),
      h('p', { class: 'dim' }, 'A briefing sums up what changed in the last day, region by region, from Meridian’s event history, with links into the map.'),
      when,
      h('p', { class: 'dim' }, 'Press Generate now to make one.')));
    let st = null;
    try { st = await api.get('/api/briefing/status'); } catch { return; }
    if (!st || !st.schedule) return;
    when.textContent = st.schedule.running
      ? `Meridian makes one automatically each day after ${st.schedule.briefing_time} (this computer’s time).`
      : 'Background tasks are off in this instance, so no daily briefing is made automatically.';
  }

  function render() {
    if (!els || !doc) return;
    const d = doc;
    const content = els.content;
    dom.clear(content);
    const shut = collapsed();
    const made = `Made ${utcShort(d.time)} (${ctx.fmt.ago(d.time)}), ${d.trigger === 'scheduled' ? 'daily' : 'on request'}`;
    content.append(h('div', { class: 'briefing-meta' },
      h('div', { class: 'briefing-period' }, h('strong', null, d.period?.label || 'Briefing'), `: ${periodText(d.period)}`),
      h('div', { class: 'dim' }, made),
      h('div', { class: 'dim tnum' }, `${ctx.fmt.int(d.stats?.events)} events recorded, ${ctx.fmt.int(d.stats?.serious)} serious or extreme`)));
    for (const note of d.coverage?.notes || []) content.append(h('p', { class: 'briefing-note' }, note));

    const byName = new Map((d.sections || []).map(s => [String(s.region).toLowerCase(), s]));
    if (d.ai && d.ai.text) {
      content.append(narrativeBlock(ctx, d.ai, byName, { onJump: jumpTo }));
    } else if (d.ai_status === 'pending') {
      content.append(h('p', { class: 'briefing-ai-wait', role: 'status' }, 'Writing the AI narrative…'));
    } else if (d.ai_status === 'failed') {
      content.append(h('p', { class: 'briefing-note' }, 'The AI narrative could not be written this time. The briefing below is complete without it.'));
    }
    for (const sec of d.sections || []) {
      content.append(sectionBlock(ctx, sec, { open: !shut.has(sec.id), onOpen: openEntity, onToggle: setCollapsed }));
    }
    content.append(sourcesBlock(ctx, d));
    const save = h('button', { class: 'btn', type: 'button' }, 'Save as case file');
    save.addEventListener('click', () => saveAsCasefile(ctx, d));
    const md = h('button', { class: 'btn', type: 'button' }, 'Download Markdown');
    md.addEventListener('click', () => downloadMarkdown(ctx, d));
    content.append(h('div', { class: 'briefing-actions' }, save, md,
      d.ai_status === 'off' ? h('span', { class: 'faint' }, 'Set up the Ask assistant (Claude or Ollama) to add an AI narrative.') : null));
  }

  function jumpTo(id) {
    const el = els?.content.querySelector(`[data-section="${CSS.escape(id)}"]`);
    if (!el) return;
    el.open = true;
    el.scrollIntoView({ block: 'start', behavior: ctx.map2.reducedMotion() ? 'auto' : 'smooth' });
    el.querySelector('summary')?.focus({ preventScroll: true });
  }

  function openEntity(entity) {
    ctx.entities.open(entity).catch?.((err) => console.error('briefing: could not open', err));
  }

  return { open, close, toggle, isOpen, current: () => doc, itemRow };
}
