// Briefing renderer: one briefing document (GET /api/briefing/{id}) into DOM nodes. Text only ever
// becomes text nodes (ctx.h); items with an entity are buttons that open it on the map.

const SEV = [[9, 'extreme', 'red'], [6, 'serious', 'amber'], [3, 'notable', 'blue'], [0, 'routine', 'grey']];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

export function sevInfo(s) {
  const v = Number(s) || 0;
  const [, word, tone] = SEV.find(([min]) => v >= min);
  return { value: v, word, tone };
}

const pad = (n) => String(n).padStart(2, '0');

/** "23 Nov 07:02 UTC" */
export function utcShort(t) {
  if (t == null) return '';
  const d = new Date(Number(t) * 1000);
  if (Number.isNaN(d.getTime())) return '';
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}

/** "Mon 23 Nov, 07:02 UTC" */
export function utcDay(t) {
  const d = new Date(Number(t) * 1000);
  if (Number.isNaN(d.getTime())) return '';
  return `${DAYS[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}, ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}

export function periodText(p) {
  if (!p) return '';
  const a = new Date(p.from * 1000);
  const b = new Date(p.to * 1000);
  const sameDay = a.getUTCFullYear() === b.getUTCFullYear() && a.getUTCMonth() === b.getUTCMonth() && a.getUTCDate() === b.getUTCDate();
  const from = utcShort(p.from).replace(' UTC', '');
  const to = sameDay ? `${pad(b.getUTCHours())}:${pad(b.getUTCMinutes())} UTC` : utcShort(p.to);
  return `${from} to ${to}`;
}

function clickable(ctx, entity) {
  if (!entity || !entity.kind || entity.id == null) return false;
  return ctx.dom.validLatLon(entity.lat, entity.lon) || !!ctx.entities.meta(entity.kind);
}

function rowShell(ctx, entity, cls, children, onOpen) {
  const { h } = ctx;
  if (clickable(ctx, entity)) {
    const b = h('button', { type: 'button', class: `${cls} briefing-open`, title: `Show ${entity.title || 'this'} on the map` }, children);
    b.addEventListener('click', () => onOpen(entity));
    return h('li', null, b);
  }
  return h('li', null, h('div', { class: cls }, children));
}

export function itemRow(ctx, it, { onOpen, showRegion = false } = {}) {
  const { h } = ctx;
  const sev = sevInfo(it.severity);
  const meta = [];
  if (showRegion && it.region) meta.push(it.region);
  if (it.time) meta.push(utcShort(it.time));
  if (it.source) meta.push(it.source);
  const badges = [];
  if (it.unverified) badges.push(h('span', { class: 'badge amber', title: 'Machine-coded from news reports by GDELT: not verified' }, 'Unverified'));
  if (it.new) badges.push(h('span', { class: 'badge blue', title: 'First seen in this period' }, 'New'));
  const isNote = it.kind === 'note';
  const sevEl = isNote ? h('span', { class: 'briefing-sev sev-grey', 'aria-hidden': 'true' }, '–')
    : h('span', { class: `briefing-sev sev-${sev.tone}`, title: `Severity ${fmtSev(sev.value)} of 10, ${sev.word}`,
      'aria-label': `Severity ${fmtSev(sev.value)}, ${sev.word}` }, fmtSev(sev.value));
  const children = [sevEl, h('span', { class: 'briefing-item-main' },
    h('span', { class: 'briefing-item-title' }, it.title || ''),
    it.line ? h('span', { class: 'briefing-item-line' }, it.line) : null,
    (meta.length || badges.length) ? h('span', { class: 'briefing-item-meta' }, meta.join(', '), badges.length ? ' ' : '', badges) : null)];
  return rowShell(ctx, it.entity, 'briefing-item', children, onOpen);
}

function fmtSev(v) {
  return Number.isInteger(v) ? String(v) : v.toFixed(1).replace(/\.0$/, '');
}

export function trendRow(ctx, tr, { onOpen } = {}) {
  const { h, fmt } = ctx;
  const badges = [];
  if (tr.notable) badges.push(h('span', { class: 'badge amber', title: 'Unlikely to be chance (Poisson test against the previous period)' }, 'Notable change'));
  if (tr.unverified) badges.push(h('span', { class: 'badge grey', title: 'Counts of machine-coded news reports' }, 'Unverified'));
  const value = tr.value_text != null ? String(tr.value_text)
    : typeof tr.value === 'number' ? (Number.isInteger(tr.value) ? fmt.int(tr.value) : fmt.num(tr.value, 1)) : '';
  const children = [
    h('span', { class: 'briefing-trend-value tnum' }, value),
    h('span', { class: 'briefing-item-main' },
      h('span', { class: 'briefing-item-title' }, tr.title || ''),
      tr.line ? h('span', { class: 'briefing-item-line' }, tr.line) : null,
      h('span', { class: 'briefing-item-meta' }, tr.source || '', badges.length ? ' ' : '', badges)),
  ];
  return rowShell(ctx, tr.entity, `briefing-trend${tr.notable ? ' is-notable' : ''}`, children, onOpen);
}

/** The AI narrative with [Section] tags turned into buttons that jump to that section. */
export function narrativeBlock(ctx, ai, sectionsByName, { onJump } = {}) {
  const { h } = ctx;
  const who = [ai.provider_label || (ai.provider === 'anthropic' ? 'Claude' : ai.provider === 'ollama' ? 'Ollama' : 'the AI assistant'), ai.model ? `(${ai.model})` : null].filter(Boolean).join(' ');
  const paras = String(ai.text || '').split(/\n{2,}/).map(p => p.trim()).filter(Boolean);
  const body = paras.map((p) => {
    const parts = [];
    let last = 0;
    for (const m of p.matchAll(/\[([^\]\n]{2,48})\]/g)) {
      parts.push(p.slice(last, m.index));
      const sec = sectionsByName.get(m[1].trim().toLowerCase());
      if (sec) {
        const b = h('button', { type: 'button', class: 'briefing-cite', title: `Go to ${sec.region}` }, sec.region);
        b.addEventListener('click', () => onJump?.(sec.id));
        parts.push(b);
      } else {
        parts.push(m[0]);
      }
      last = m.index + m[0].length;
    }
    parts.push(p.slice(last));
    return h('p', null, parts);
  });
  return h('section', { class: 'briefing-ai', 'aria-label': 'AI narrative' },
    h('div', { class: 'briefing-label' }, 'AI narrative'),
    body,
    h('p', { class: 'briefing-ai-note' }, `Written by ${who} from the items below. It may contain mistakes, so check the items and their sources.`));
}

export function sectionBlock(ctx, sec, { open = true, onOpen, onToggle } = {}) {
  const { h, fmt } = ctx;
  const items = sec.items || [];
  const trends = sec.trends || [];
  const n = items.length + trends.length;
  const worst = items.reduce((m, it) => Math.max(m, it.kind === 'note' ? 0 : Number(it.severity) || 0), 0);
  const sum = h('summary', { class: 'briefing-sec-head' },
    h('span', { class: 'briefing-sec-title' }, sec.region),
    h('span', { class: 'briefing-sec-count tnum', title: `${n} entr${n === 1 ? 'y' : 'ies'}` }, n ? fmt.int(n) : ''),
    worst >= 6 ? h('span', { class: `briefing-sec-worst sev-${sevInfo(worst).tone}`, title: `Most severe item: ${fmtSev(worst)} of 10` }, sevInfo(worst).word) : null);
  const det = h('details', { class: `briefing-sec briefing-sec-${sec.group || 'regions'}`, id: `briefing-sec-${sec.id}`, 'data-section': sec.id });
  det.open = open;
  det.append(sum);
  if (sec.summary && sec.id !== 'headline') det.append(h('p', { class: 'briefing-sec-summary' }, sec.summary));
  if (trends.length) {
    det.append(h('div', { class: 'briefing-sub' }, 'What changed'));
    det.append(h('ul', { class: 'briefing-list' }, trends.map(tr => trendRow(ctx, tr, { onOpen }))));
  }
  if (items.length) {
    if (trends.length) det.append(h('div', { class: 'briefing-sub' }, sec.id === 'areas' ? 'Alerts' : 'Most severe'));
    det.append(h('ul', { class: 'briefing-list' }, items.map(it => itemRow(ctx, it, { onOpen, showRegion: sec.id === 'headline' }))));
  }
  if (!n && (sec.note || !sec.summary)) det.append(h('p', { class: 'briefing-empty' }, sec.note || 'Nothing to report.'));
  det.addEventListener('toggle', () => onToggle?.(sec.id, det.open));
  return det;
}

export function sourcesBlock(ctx, doc) {
  const { h } = ctx;
  const srcs = doc.sources || [];
  return h('div', { class: 'briefing-foot' },
    h('p', null, srcs.length ? `Sources: ${srcs.join(', ')}.` : 'Built from Meridian’s event history.'),
    h('p', null, 'Conflict and news items are machine-coded from news reports by GDELT: unverified, and one widely repeated story can inflate a count. Severity runs from 0 (routine) to 10 (extreme). Times are UTC.'));
}
