// Settings tab: desktop notifications (ctx.notify), what pops up, quiet hours, volume, the assistant,
// a test alert and the engine's last check.

const SEVERITIES = [[0, 'Every alert'], [3, 'Severity 3 and more (notable)'], [6, 'Severity 6 and more (serious)'], [9, 'Severity 9 and more (extreme)']];
const RATES = [5, 10, 20, 50];

export function settingsView(ctx, state) {
  const { h, dom, ui, fmt } = ctx;
  const root = h('div', { class: 'alerts-settings' });
  let status = null;

  async function save(change) {
    try {
      await state.saveSettings(change);
    } catch (err) {
      ui.toast(`That setting was not saved: ${err?.message || err}`, 'red');
    }
    render();
  }

  function sw(checked, label, onChange, key) {
    const input = h('input', { type: 'checkbox', checked: checked || null, dataset: { focusKey: key } });
    input.addEventListener('change', () => onChange(input.checked));
    return h('label', { class: 'alerts-switch-row' }, h('span', null, label), h('span', { class: 'switch' }, input, h('span')));
  }

  function select(options, value, label, onChange, key) {
    const sel = h('select', { 'aria-label': label, dataset: { focusKey: key } },
      options.map(([v, text]) => h('option', { value: String(v), selected: String(v) === String(value) || null }, text)));
    sel.addEventListener('change', () => onChange(sel.value));
    return h('label', { class: 'alerts-select-row' }, h('span', null, label), sel);
  }

  function desktop() {
    const p = ctx.notify.permission();
    const box = h('section', { class: 'alerts-set' }, h('h3', null, 'Desktop notifications'));
    if (p === 'unsupported') {
      box.append(h('p', { class: 'alerts-note' }, 'This browser cannot show desktop notifications. Alerts still pop up inside Meridian.'));
    } else if (p === 'denied') {
      box.append(h('p', { class: 'alerts-note' }, 'Desktop notifications are blocked for this site. Allow them in the browser’s site settings, then reload.'));
    } else if (p === 'default') {
      const b = h('button', { type: 'button', class: 'btn primary', dataset: { focusKey: 'allow' } }, 'Allow desktop notifications');
      b.addEventListener('click', async () => {
        const r = await ctx.notify.request();
        ui.toast(r === 'granted' ? 'Desktop notifications are on.' : 'Desktop notifications were not allowed. Alerts still pop up inside Meridian.', r === 'granted' ? 'green' : 'amber');
        render();
      });
      box.append(h('p', { class: 'alerts-note' }, 'They show while Meridian is in the background. Inside Meridian, alerts pop up at the bottom of the map.'), b);
    } else {
      box.append(sw(ctx.notify.enabled(), 'Show desktop notifications', (on) => { ctx.notify.setEnabled(on); render(); }, 'desktop'),
        h('p', { class: 'alerts-note faint' }, 'The same switch sits in the status bar. They show while Meridian is in the background.'));
    }
    return box;
  }

  function render() {
    const focusKey = root.contains(document.activeElement) ? document.activeElement?.dataset?.focusKey : null;
    dom.clear(root);
    const s = state.settings;
    if (!s) { root.append(h('p', { class: 'alerts-empty' }, 'Loading…')); return; }
    const n = s.notify || {};
    const q = s.quiet_hours || {};

    const pop = h('section', { class: 'alerts-set' }, h('h3', null, 'Pop-ups'),
      sw(n.on !== false, 'Tell me about new alerts', (on) => save({ notify: { on } }), 'notify-on'),
      n.on !== false ? select(SEVERITIES, n.min_severity ?? 0, 'Which ones', (v) => save({ notify: { min_severity: +v } }), 'notify-min') : null,
      h('p', { class: 'alerts-note faint' }, 'Every alert lands in the inbox either way.'));

    const from = h('input', { type: 'time', value: q.start || '22:00', 'aria-label': 'Quiet from', dataset: { focusKey: 'q-from' } });
    const to = h('input', { type: 'time', value: q.end || '07:00', 'aria-label': 'Quiet until', dataset: { focusKey: 'q-to' } });
    from.addEventListener('change', () => { if (/^\d\d:\d\d$/.test(from.value)) save({ quiet_hours: { start: from.value } }); });
    to.addEventListener('change', () => { if (/^\d\d:\d\d$/.test(to.value)) save({ quiet_hours: { end: to.value } }); });
    const extreme = h('input', { type: 'checkbox', checked: q.allow_extreme !== false || null, dataset: { focusKey: 'q-extreme' } });
    extreme.addEventListener('change', () => save({ quiet_hours: { allow_extreme: extreme.checked } }));
    const quiet = h('section', { class: 'alerts-set' }, h('h3', null, 'Quiet hours'),
      sw(!!q.on, 'Stay quiet at night', (on) => save({ quiet_hours: { on } }), 'q-on'),
      q.on ? h('div', { class: 'alerts-times' }, h('label', null, h('span', null, 'From'), from), h('label', null, h('span', null, 'Until'), to)) : null,
      q.on ? h('label', { class: 'alerts-check-row' }, extreme, h('span', null, 'Still tell me about extreme alerts (severity 9 and more)')) : null,
      h('p', { class: 'alerts-note faint' }, 'Uses this computer’s clock. Alerts still collect in the inbox.'));

    const volume = h('section', { class: 'alerts-set' }, h('h3', null, 'Volume'),
      select(RATES.map(r => [r, `${r} an hour`]), s.rate_limit, 'Alerts per watchlist', (v) => save({ rate_limit: +v }), 'rate'),
      h('p', { class: 'alerts-note faint' }, 'Past that, the rest are kept and summed up in one “more alerts” item each hour, most severe first.'));

    const assistant = h('section', { class: 'alerts-set' }, h('h3', null, 'Assistant'),
      sw(s.ai_share === true, 'Let the assistant and MCP tools read my alerts', (on) => save({ ai_share: on }), 'ai'),
      h('p', { class: 'alerts-note faint' }, 'It sees alert titles, area names and event positions, not the shapes of your areas.'));

    const testBtn = h('button', { type: 'button', class: 'btn', dataset: { focusKey: 'test' } }, 'Send a test alert');
    testBtn.addEventListener('click', async () => {
      try { await state.test(); ctx.bus.emit('alerts:poll', {}); } catch (err) { ui.toast(`No test alert: ${err?.message || err}`, 'red'); }
    });
    const checkBtn = h('button', { type: 'button', class: 'btn', dataset: { focusKey: 'check' } }, 'Check now');
    checkBtn.addEventListener('click', async () => {
      checkBtn.disabled = true;
      try {
        const r = await state.check();
        const res = r.result || {};
        ui.toast(res.ok === false ? `The check failed: ${res.error || 'unknown error'}` : res.skipped ? 'A check is already running.'
          : `Checked ${fmt.int(res.scanned)} events: ${fmt.int(res.alerts)} new alert${res.alerts === 1 ? '' : 's'}.`, res.ok === false ? 'red' : '');
        ctx.bus.emit('alerts:poll', {});
        await loadStatus();
      } catch (err) {
        ui.toast(err?.message || 'The check did not run.', 'amber');
      }
      checkBtn.disabled = false;
    });
    const e = status?.engine;
    const last = e?.at ? (e.ok === false ? `The last check failed at ${fmt.clock(e.at)} UTC: ${e.error || 'unknown error'}.`
      : `Last check at ${fmt.clock(e.at)} UTC: ${fmt.int(e.scanned)} events, ${fmt.int(e.alerts)} new alert${e.alerts === 1 ? '' : 's'}.`)
      : 'Meridian checks every two minutes, a minute or two after it starts.';
    const tools = h('section', { class: 'alerts-set' }, h('h3', null, 'Check and test'),
      h('div', { class: 'alerts-actions' }, testBtn, checkBtn), h('p', { class: 'alerts-note faint tnum' }, last));

    root.append(desktop(), pop, quiet, volume, assistant, tools);
    if (focusKey) root.querySelector(`[data-focus-key="${CSS.escape(focusKey)}"]`)?.focus({ preventScroll: true });
  }

  async function loadStatus() {
    try { status = await state.status(); } catch { status = null; }
    render();
  }

  ctx.bus.on('notify:changed', () => { if (root.isConnected) render(); });
  return { el: root, render, load: loadStatus };
}
