// Ask Meridian: the assistant drawer. A question goes to /api/ai/chat as a background job with the
// current map view; the drawer shows live tool progress, then the answer (safe Markdown, DOM only),
// clickable entity chips, the map changes it made (with undo) and a collapsible list of tools used.
// Stop cancels the job on the server, so no further model or tool calls are made.

import { renderMarkdown } from './ai/markdown.js';
import { validateActions, runActions, describeActions, captureView, restoreView, cleanEntity } from './ai/actions.js';

const STORE_KEY = 'meridian.ai.chat.v1';
const MAX_KEEP = 40;
const MAX_QUESTION = 4000;
const SETUP = 'Add ANTHROPIC_API_KEY to the .env file and restart Meridian, or start Ollama.';
const ICON = 'M4 4.5h12a1.5 1.5 0 0 1 1.5 1.5v6.5a1.5 1.5 0 0 1-1.5 1.5H9.5L6 16.8V14H4a1.5 1.5 0 0 1-1.5-1.5V6A1.5 1.5 0 0 1 4 4.5ZM6.9 9.3h.01M10 9.3h.01M13.1 9.3h.01';
const SEND_ICON = 'M4 10h11M10.5 5l5 5-5 5';
const STOP_ICON = 'M6.5 6.5h7v7h-7z';
const SUGGESTIONS = [
  { text: "What's happening in the Strait of Hormuz?", group: 'Ships' },
  { text: 'Show military aircraft over the Baltic', group: 'Flights' },
  { text: 'Any strong earthquakes today?', group: 'Events' },
  { text: 'Which cameras are near Heathrow?', group: 'Cameras' },
];
const TOOL_WORDS = {
  search_entities: 'Searching Meridian', find_place: 'Finding the place', whats_here: 'Looking around the point',
  recent_events: 'Checking recent events', list_layers: 'Checking the layers', fly_to: 'Moving the map',
  fit_bounds: 'Fitting the map', set_layers: 'Choosing layers', set_basemap: 'Choosing a basemap',
  set_projection: 'Choosing the projection', set_terrain: 'Setting terrain', open_entity: 'Opening a card',
  highlight: 'Marking a point', whats_here_card: "Opening What's here?",
};
const STOPPED = {
  max_rounds: 'Stopped after the most tool rounds one answer may use.',
  time_budget: 'Answered early to stay within the time limit.',
  max_tokens: 'The answer hit its length limit.',
  context: 'The conversation is too long: start a new chat.',
};

let seq = 0;
const uid = () => `m${Date.now().toString(36)}${(seq++).toString(36)}`;

export async function register(ctx) {
  const { h, ui, api, dom, fmt } = ctx;
  const state = { messages: load(), pending: null, status: null, statusAt: 0, statusError: null };
  let drawer = null;
  let els = null;
  let undo = null;   // {msgId, snapshot, highlight} for the latest answer that moved the map

  const button = ui.toolbarButton({ id: 'ai', title: 'Ask Meridian (A)', label: 'Ask', icon: ICON, order: 10, onClick: () => (drawer ? close() : open(true)) });
  ui.shortcut('a', 'Ask Meridian a question', () => open(true));

  // ---------------------------------------------------------------- storage
  function load() {
    try {
      const rows = JSON.parse(sessionStorage.getItem(STORE_KEY) || '[]');
      return Array.isArray(rows) ? rows.filter(m => m && (m.role === 'user' || m.role === 'assistant') && typeof m.content === 'string') : [];
    } catch { return []; }
  }
  function save() {
    let rows = state.messages.slice(-MAX_KEEP);
    try {
      let text = JSON.stringify(rows);
      while (text.length > 400000 && rows.length > 2) { rows = rows.slice(2); text = JSON.stringify(rows); }
      sessionStorage.setItem(STORE_KEY, text);
    } catch { /* storage full or blocked: keep the chat in memory only */ }
    state.messages = state.messages.slice(-MAX_KEEP);
  }

  // ---------------------------------------------------------------- server status
  async function refreshStatus(force = false) {
    if (!force && state.status && Date.now() - state.statusAt < 20000) return state.status;
    try {
      state.status = await api.get('/api/ai/status', force ? { fresh: 1 } : null, { timeoutMs: 8000 });
      state.statusError = null;
    } catch (err) {
      state.statusError = err;
    }
    state.statusAt = Date.now();
    paintHead();
    return state.status;
  }

  const ready = () => !!state.status?.ready;

  // ---------------------------------------------------------------- the drawer
  function open(focus = false) {
    if (!drawer) {
      els = build();
      drawer = ui.drawer({ id: 'ai', title: 'Ask Meridian', el: els.root, width: 452, tools: els.headTools, onClose: () => {
        drawer = null;
        els = null;
        button.setAttribute('aria-pressed', 'false');
      } });
      drawer.el.classList.add('ai-drawer');
      button.setAttribute('aria-pressed', 'true');
      renderLog();
      paintHead();
      refreshStatus().then(() => { if (els) { renderLog(); paintComposer(); } });
    }
    if (focus) els?.input.focus();
  }

  function close() { drawer?.close(); }

  function build() {
    const log = h('div', { class: 'ai-log', role: 'log', 'aria-live': 'polite', 'aria-label': 'Conversation' });
    const input = h('textarea', { class: 'ai-input', rows: 1, maxlength: MAX_QUESTION, placeholder: 'Ask about the map',
      'aria-label': 'Ask a question about the map', autocomplete: 'off', spellcheck: 'true' });
    const send = h('button', { class: 'ai-send', type: 'button', title: 'Send (Enter)', 'aria-label': 'Send' }, dom.svgIcon(SEND_ICON));
    const stop = h('button', { class: 'ai-stop', type: 'button', title: 'Stop (Esc)', 'aria-label': 'Stop', hidden: true }, dom.svgIcon(STOP_ICON), h('span', null, 'Stop'));
    const foot = h('p', { class: 'ai-foot' });
    const composer = h('div', { class: 'ai-composer' }, input, send, stop);
    const providerTag = h('span', { class: 'ai-provider' });
    const newChat = h('button', { class: 'text-btn ai-new', type: 'button', title: 'Clear this conversation' }, 'New chat');
    const headTools = h('div', { class: 'ai-head-tools' }, providerTag, newChat);
    const root = h('div', { class: 'ai-chat' }, log, h('div', { class: 'ai-bottom' }, composer, foot));

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
      else if (e.key === 'Escape' && state.pending) { e.preventDefault(); stopPending(); }
    });
    input.addEventListener('input', () => autosize(input));
    send.addEventListener('click', () => submit());
    stop.addEventListener('click', () => stopPending());
    newChat.addEventListener('click', () => clearChat());
    return { root, log, input, send, stop, foot, providerTag, headTools, newChat };
  }

  function autosize(input) {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 132)}px`;
  }

  function paintHead() {
    if (!els) return;
    const s = state.status;
    dom.clear(els.providerTag);
    let tone = 'off';
    let label = 'Checking…';
    let title = '';
    if (state.statusError && !s) { tone = 'bad'; label = 'Unavailable'; title = 'The assistant module is not answering.'; }
    else if (s?.ready) { tone = 'ok'; label = s.provider_label || s.provider; title = `${s.provider_label || s.provider}: ${s.model}`; }
    else if (s) { tone = 'off'; label = 'Not set up'; title = s.message || SETUP; }
    els.providerTag.className = `ai-provider ${tone}`;
    els.providerTag.title = title;
    els.providerTag.append(h('i', { 'aria-hidden': 'true' }), label);
    paintComposer();
  }

  function paintComposer() {
    if (!els) return;
    const busy = !!state.pending;
    const canAsk = ready() && ctx.moduleOk('ai');
    els.send.hidden = busy;
    els.stop.hidden = !busy;
    els.input.disabled = !canAsk && !busy;
    els.send.disabled = !canAsk;
    els.input.placeholder = canAsk ? (state.messages.length ? 'Ask a follow-up' : 'Ask about the map') : 'Set up a provider to ask questions';
    els.newChat.disabled = !state.messages.length && !busy;
    const s = state.status;
    dom.clear(els.foot);
    if (s?.ready) {
      els.foot.append(s.provider === 'ollama'
        ? `Answers come from Meridian's open data and ${s.model} on this computer, and can be wrong.`
        : `Answers come from Meridian's open data and ${s.model}, and can be wrong. Questions and map context are sent to Anthropic.`);
    } else {
      els.foot.append("Answers come from Meridian's open data and can be wrong.");
    }
  }

  // ---------------------------------------------------------------- rendering
  function renderLog() {
    if (!els) return;
    const stick = nearBottom();
    dom.clear(els.log);
    els.root.classList.toggle('is-empty', !state.messages.length && !state.pending);
    if (!ctx.moduleOk('ai')) {
      els.log.append(h('div', { class: 'ai-empty' }, h('div', { class: 'ai-setup bad' },
        h('h3', null, 'The assistant is not available'),
        h('p', null, "The ai module didn't start on the Meridian server. Sources and status has the details."))));
      paintComposer();
      return;
    }
    if (!state.messages.length && !state.pending) els.log.append(emptyState());
    for (const msg of state.messages) els.log.append(msg.role === 'user' ? userNode(msg) : botNode(msg));
    if (state.pending) els.log.append(pendingNode(state.pending));
    paintComposer();
    if (stick || state.pending) scrollDown();
  }

  function nearBottom() {
    const el = els?.log;
    return !el || el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }
  function scrollDown() { requestAnimationFrame(() => { if (els) els.log.scrollTop = els.log.scrollHeight; }); }

  function emptyState() {
    const s = state.status;
    const box = h('div', { class: 'ai-empty' });
    if (s && !s.ready) {
      box.append(setupNode(s));
      return box;
    }
    box.append(h('p', { class: 'ai-intro' }, 'Ask about anything on the map. Meridian looks it up in its live layers and open data, and moves the map to show you.'));
    const grid = h('div', { class: 'ai-suggest', role: 'list' });
    for (const sgg of SUGGESTIONS) {
      const b = h('button', { type: 'button', role: 'listitem', style: { '--gc': ctx.layers.groupColor(sgg.group) }, disabled: s ? null : true }, sgg.text);
      b.addEventListener('click', () => submit(sgg.text));
      grid.append(b);
    }
    box.append(grid);
    return box;
  }

  function setupNode(s) {
    const check = h('button', { class: 'btn', type: 'button' }, 'Check again');
    check.addEventListener('click', async () => {
      check.disabled = true;
      check.textContent = 'Checking…';
      await refreshStatus(true);
      renderLog();
    });
    const disabled = s.code === 'DISABLED';
    const urlRefused = s.code === 'OLLAMA_URL_REFUSED';
    return h('div', { class: 'ai-setup' },
      h('h3', null, disabled ? 'The assistant is switched off' : 'Set up the assistant'),
      h('p', null, s.message || SETUP),
      disabled || urlRefused ? null : h('ul', null,
        h('li', null, h('strong', null, 'Claude: '), 'put ', h('code', null, 'ANTHROPIC_API_KEY=your-key'), ' in the .env file next to server.py and restart Meridian. ',
          h('a', { href: 'https://platform.claude.com/settings/keys' }, 'Get a key')),
        h('li', null, h('strong', null, 'Ollama (free, on this computer): '), 'install it, run ', h('code', null, 'ollama pull qwen3:8b'), ' and keep it running. ',
          h('a', { href: 'https://ollama.com/download' }, 'Download Ollama'))),
      s.warning ? h('p', { class: 'ai-warn' }, s.warning) : null,
      h('div', { class: 'ai-setup-foot' }, check, h('span', { class: 'faint' }, 'Ollama is found without a restart.')));
  }

  function userNode(msg) {
    return h('div', { class: 'ai-msg ai-user', dataset: { id: msg.id } }, h('div', { class: 'ai-bubble' }, msg.content));
  }

  function botNode(msg) {
    const wrap = h('div', { class: `ai-msg ai-bot${msg.status === 'error' ? ' is-error' : ''}`, dataset: { id: msg.id } });
    const meta = msg.meta || {};
    if (msg.status === 'stopped') {
      wrap.append(h('p', { class: 'ai-stopped' }, 'Stopped. Nothing more was asked of the assistant.'));
      if (meta.trace?.length) wrap.append(traceNode(msg));
      return wrap;
    }
    if (msg.status === 'error') {
      const err = meta.error || {};
      const retry = h('button', { class: 'btn', type: 'button' }, 'Try again');
      retry.addEventListener('click', () => retryFrom(msg));
      const setup = err.code === 'NOT_CONFIGURED' || err.code === 'DISABLED';
      wrap.append(h('div', { class: 'ai-error' }, h('p', null, err.message || msg.content || 'Something went wrong.'),
        setup ? null : h('div', { class: 'ai-error-foot' }, retry)));
      if (meta.trace?.length) wrap.append(traceNode(msg));
      return wrap;
    }
    wrap.append(h('div', { class: 'ai-md' }, renderMarkdown(msg.content, { h, safeUrl: dom.safeUrl })));
    if (meta.entities?.length) wrap.append(chipsNode(meta.entities));
    if (meta.map) {
      const line = h('div', { class: 'ai-mapline' }, h('span', { class: 'ai-mapline-text' }, meta.map));
      if (meta.undone) line.append(h('span', { class: 'faint' }, 'Map put back.'));
      else if (undo && undo.msgId === msg.id) {
        const b = h('button', { class: 'text-btn', type: 'button', title: 'Put the map back as it was before this answer' }, 'Undo');
        b.addEventListener('click', async () => {
          const u = undo;
          undo = null;
          b.remove();
          try {
            await restoreView(u.snapshot, ctx);
            for (const key of u.cards || []) if (!u.snapshot.cards.has(key)) ctx.cards.close(key);
            if (u.highlight) ctx.map2.clearHighlight();
            meta.undone = true;
            save();
            line.append(h('span', { class: 'faint' }, 'Map put back.'));
          } catch (err) { console.warn('ai: undo failed', err); }
        });
        line.append(b);
      }
      wrap.append(line);
    }
    if (meta.trace?.length) wrap.append(traceNode(msg));
    const bits = [];
    if (meta.model) bits.push(meta.provider === 'ollama' ? `Ollama, ${meta.model}` : meta.model);
    if (meta.elapsed_ms != null) bits.push(`${(meta.elapsed_ms / 1000).toFixed(1)} s`);
    if (meta.time) bits.push(`${fmt.clock(meta.time).slice(0, 5)} UTC`);
    const u = meta.usage || {};
    const tokens = u.input_tokens != null ? `${fmt.int(u.input_tokens)} input and ${fmt.int(u.output_tokens)} output tokens`
      + (u.cache_read_input_tokens ? `, ${fmt.int(u.cache_read_input_tokens)} read from cache` : '') : null;
    const foot = h('div', { class: 'ai-meta', title: tokens }, bits.join(', '));
    if (meta.fallback?.to) foot.append(h('div', null, `Answered by ${meta.fallback.to} after ${meta.fallback.from || 'the first model'} declined.`));
    if (meta.stopped && STOPPED[meta.stopped]) foot.append(h('div', null, STOPPED[meta.stopped]));
    if (meta.dropped) foot.append(h('div', null, `${meta.dropped} map change${meta.dropped > 1 ? 's were' : ' was'} skipped because ${meta.dropped > 1 ? 'they' : 'it'} didn't check out.`));
    wrap.append(foot);
    return wrap;
  }

  function chipsNode(list) {
    const row = h('div', { class: 'ai-chips', role: 'list', 'aria-label': 'Things in this answer' });
    for (const raw of list) {
      const entity = cleanEntity(raw, ctx);
      if (!entity) continue;
      const group = entity.layer ? ctx.layers.get(entity.layer)?.spec?.group : null;
      const b = h('button', { class: 'ai-chip', type: 'button', role: 'listitem', title: entity.subtitle || `Open ${entity.title || entity.id}`,
        style: { '--gc': group ? ctx.layers.groupColor(group) : 'var(--paper-faint)' } },
        h('i', { 'aria-hidden': 'true' }), h('span', { class: 'ai-chip-t' }, entity.title || entity.id), h('span', { class: 'ai-chip-k' }, entity.kind.replace(/_/g, ' ')));
      b.addEventListener('click', () => ctx.entities.open(entity));
      row.append(b);
    }
    return row;
  }

  function toolWord(name) {
    return TOOL_WORDS[name] || name.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());
  }

  function argText(args) {
    if (!args || typeof args !== 'object') return '';
    if (args._preview) return String(args._preview).slice(0, 80);
    const parts = [];
    for (const [k, v] of Object.entries(args)) {
      if (v == null || typeof v === 'object' && !Array.isArray(v)) continue;
      const val = Array.isArray(v) ? v.slice(0, 4).join(', ') : typeof v === 'number' ? String(+v.toFixed(4)) : String(v);
      parts.push(`${k.replace(/_/g, ' ')} ${val}`);
      if (parts.length >= 3) break;
    }
    return parts.join(', ').slice(0, 90);
  }

  function traceRows(trace) {
    const ol = h('ol', { class: 'ai-steps' });
    for (const t of trace) {
      ol.append(h('li', { class: t.ok ? '' : 'bad' },
        h('span', { class: 'ai-step-t' }, h('code', null, t.tool), t.module && t.module !== 'ai' ? h('span', { class: 'faint' }, ` ${t.module}`) : null),
        h('span', { class: 'ai-step-a' }, argText(t.args)),
        h('span', { class: 'ai-step-r' }, t.ok ? (t.summary || 'done') : (t.error || 'failed')),
        h('span', { class: 'ai-step-ms' }, t.ms != null ? `${t.ms < 1000 ? t.ms : (t.ms / 1000).toFixed(1)} ${t.ms < 1000 ? 'ms' : 's'}` : '')));
    }
    return ol;
  }

  function traceNode(msg) {
    const trace = msg.meta.trace;
    const failed = trace.filter(t => !t.ok).length;
    const details = h('details', { class: 'ai-trace', open: msg.traceOpen ? true : null },
      h('summary', null, `Tools used (${trace.length})`, failed ? h('span', { class: 'ai-bad' }, `, ${failed} failed`) : null),
      traceRows(trace));
    details.addEventListener('toggle', () => { msg.traceOpen = details.open; });
    return details;
  }

  function pendingNode(p) {
    const elapsed = h('span', { class: 'ai-elapsed' });
    const phase = h('span', { class: 'ai-phase' });
    const note = h('p', { class: 'ai-note', hidden: true });
    const steps = h('div', { class: 'ai-live' });
    const node = h('div', { class: 'ai-msg ai-bot ai-pending', 'aria-busy': 'true' },
      h('div', { class: 'ai-working' }, h('span', { class: 'ai-pulse', 'aria-hidden': 'true' }), phase, elapsed), note, steps);
    p.view = { node, elapsed, phase, note, steps };
    p.shown = -1;   // a fresh view (the drawer may have been closed and opened again)
    paintPending(p);
    return node;
  }

  function paintPending(p) {
    if (!p.view) return;
    const secs = (performance.now() - p.started) / 1000;
    p.view.elapsed.textContent = `${secs.toFixed(0)} s`;
    let text = 'Sending your question';
    if (p.job) {
      if (p.phase === 'tools') text = p.tool ? toolWord(p.tool) : 'Looking things up';
      else if (p.phase === 'answer') text = 'Writing the answer';
      else text = p.round > 1 ? 'Reading the results' : 'Thinking about your question';
    }
    p.view.phase.textContent = text;
    if (p.note) { p.view.note.hidden = false; p.view.note.textContent = p.note; } else p.view.note.hidden = true;
    if (p.trace.length !== p.shown) {
      p.shown = p.trace.length;
      dom.clear(p.view.steps);
      if (p.trace.length) p.view.steps.append(traceRows(p.trace));
      if (els && nearBottom()) scrollDown();
    }
  }

  // ---------------------------------------------------------------- asking
  function viewNow() {
    const map = ctx.map;
    const c = map.getCenter().wrap();
    const vs = ctx.map2.viewState();
    const layers = ctx.layers.list().map(l => ({ id: l.id, title: l.title, group: l.group, on: !!l.on,
      count: typeof l.count === 'number' || typeof l.count === 'string' ? l.count : null, status: l.status, message: l.message || null }));
    return { bounds: ctx.map2.viewBounds(), zoom: +map.getZoom().toFixed(2), center: { lat: +c.lat.toFixed(5), lon: +c.lng.toFixed(5) },
      layers_on: layers.filter(l => l.on).map(l => l.id), layers, basemap: vs.basemap, projection: vs.projection, terrain: !!vs.terrain };
  }

  /** The conversation for the server: answered turns plus the new question. */
  function history() {
    const out = [];
    const msgs = state.messages;
    for (let i = 0; i < msgs.length; i++) {
      const m = msgs[i];
      if (m.role === 'user') {
        const next = msgs[i + 1];
        if (i === msgs.length - 1 || (next && next.role === 'assistant' && next.status === 'ok')) out.push({ role: 'user', content: m.content });
      } else if (m.status === 'ok' && m.content) {
        out.push({ role: 'assistant', content: m.content });
      }
    }
    return out.slice(-30);
  }

  async function submit(textArg) {
    if (state.pending) return;
    open(false);
    const text = String(textArg ?? els?.input.value ?? '').trim().slice(0, MAX_QUESTION);
    if (!text) return;
    if (!ready()) {
      await refreshStatus(true);
      if (!ready()) { renderLog(); ui.toast(state.status?.message || SETUP, 'amber', 6000); return; }
    }
    if (els) { els.input.value = ''; autosize(els.input); }
    state.messages.push({ id: uid(), role: 'user', content: text, time: Date.now() });
    save();
    ask();
  }

  function retryFrom(msg) {
    if (state.pending) return;
    const i = state.messages.indexOf(msg);
    if (i < 0) return;
    state.messages.splice(i, 1);    // drop the failed answer; its question is asked again
    const last = state.messages[state.messages.length - 1];
    if (!last || last.role !== 'user') { renderLog(); return; }
    save();
    ask();
  }

  async function ask() {
    const p = { started: performance.now(), job: null, phase: 'model', round: 1, tool: null, note: null, trace: [], shown: -1, polls: 0, failures: 0, stopped: false };
    state.pending = p;
    renderLog();
    p.ticker = setInterval(() => paintPending(p), 1000);
    try {
      const res = await api.post('/api/ai/chat', { messages: history(), view: viewNow(), async: true }, { timeoutMs: 20000 });
      if (p.stopped) return;
      if (!res.job) { await finish(p, res.result || { ok: false, error: { code: 'NO_JOB', message: 'The assistant did not start.' } }); return; }
      p.job = res.job;
      poll(p);
    } catch (err) {
      fail(p, err);
    }
  }

  async function poll(p) {
    if (p.stopped || state.pending !== p) return;
    try {
      const snap = await api.get(`/api/ai/jobs/${encodeURIComponent(p.job)}`, null, { timeoutMs: 10000 });
      if (p.stopped || state.pending !== p) return;
      p.failures = 0;
      p.trace = snap.trace || [];
      p.phase = snap.phase;
      p.round = snap.round || 1;
      p.tool = snap.tool;
      p.note = (snap.notes || []).slice(-1)[0] || null;
      paintPending(p);
      if (snap.result) { await finish(p, snap.result); return; }
    } catch (err) {
      p.failures += 1;
      if (err?.status === 404 || p.failures >= 4) { fail(p, err); return; }
    }
    if (performance.now() - p.started > 150000) { fail(p, new Error('The assistant stopped answering. Try again.')); return; }
    p.polls += 1;
    p.timer = setTimeout(() => poll(p), p.polls < 6 ? 350 : p.polls < 20 ? 600 : 1000);
  }

  function settle(p) {
    clearInterval(p.ticker);
    clearTimeout(p.timer);
    if (state.pending === p) state.pending = null;
  }

  function stopPending() {
    const p = state.pending;
    if (!p) return;
    p.stopped = true;
    settle(p);
    if (p.job) api.post(`/api/ai/jobs/${encodeURIComponent(p.job)}/cancel`, {}).catch(() => { /* already finished */ });
    state.messages.push({ id: uid(), role: 'assistant', status: 'stopped', content: '', time: Date.now(), meta: { trace: p.trace } });
    save();
    renderLog();
    els?.input.focus();
  }

  function fail(p, err) {
    if (p.stopped || state.pending !== p) return;
    settle(p);
    const message = err?.status === 429 ? (err.message || 'The assistant is busy. Try again in a moment.')
      : err?.status === 404 ? 'The answer was lost (the server may have restarted). Try again.'
        : err?.name === 'TimeoutError' || err?.name === 'AbortError' ? 'The Meridian server took too long to reply. Try again.'
          : err instanceof TypeError ? "The Meridian server isn't answering. Is server.py still running?"
            : (err?.message || 'Something went wrong.');
    state.messages.push({ id: uid(), role: 'assistant', status: 'error', content: '', time: Date.now(),
      meta: { error: { code: err?.code || 'CLIENT', message }, trace: p.trace } });
    save();
    renderLog();
  }

  async function finish(p, result) {
    if (p.stopped || state.pending !== p) return;
    settle(p);
    const meta = { trace: result.trace || [], provider: result.provider, provider_label: result.provider_label, model: result.model,
      elapsed_ms: result.elapsed_ms, usage: result.usage, stopped: result.stopped, fallback: result.fallback, time: Date.now(),
      entities: Array.isArray(result.entities) ? result.entities.slice(0, 8) : [] };
    // A refusal notice is Meridian's wording, not the model's: show it, but keep it out of the history.
    const status = !result.ok ? 'error' : result.stopped === 'refusal' ? 'refused' : 'ok';
    const msg = { id: uid(), role: 'assistant', content: String(result.reply || ''), time: Date.now(), status, meta };
    if (!result.ok) {
      meta.error = result.error || { code: 'ERROR', message: result.reply || 'Something went wrong.' };
      if (meta.error.code === 'NOT_CONFIGURED' || meta.error.code === 'DISABLED') refreshStatus(true);
    }
    state.messages.push(msg);
    save();
    if (drawer) renderLog();
    else ui.toast(result.ok ? 'Meridian answered your question. Press A to read it.' : 'The assistant could not answer. Press A for details.', result.ok ? 'green' : 'red', 6000);
    if (!result.ok || !Array.isArray(result.actions) || !result.actions.length) return;
    // The answer is on screen; now show it on the map (checked again here: known actions, sane values, real layers).
    const { valid, dropped } = validateActions(result.actions, ctx);
    meta.dropped = dropped.length || undefined;
    if (dropped.length) console.warn('ai: skipped map actions', dropped);
    if (valid.length) {
      const snapshot = captureView(ctx);
      snapshot.cards = new Set(valid.filter(a => a.type === 'open_entity' && ctx.cards.get(`${a.kind}:${a.id}`)).map(a => `${a.kind}:${a.id}`));
      const outcome = await runActions(valid, ctx);
      if (outcome.done.length) {
        meta.map = describeActions(outcome.done, ctx);
        undo = { msgId: msg.id, snapshot, highlight: outcome.done.some(a => a.type === 'highlight'),
          cards: outcome.done.filter(a => a.type === 'open_entity').map(a => `${a.kind}:${a.id}`) };
      }
      if (outcome.failed.length) meta.dropped = (meta.dropped || 0) + outcome.failed.length;
    }
    save();
    if (drawer) renderLog();
  }

  function clearChat() {
    if (state.pending) stopPending();
    state.messages = [];
    undo = null;
    save();
    renderLog();
    els?.input.focus();
  }

  // Warm the status so the first open of the drawer shows the right state straight away.
  setTimeout(() => refreshStatus(), 1500);
}
