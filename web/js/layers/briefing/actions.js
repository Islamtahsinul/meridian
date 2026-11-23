// "Save as case file" and "Download Markdown" for a briefing.

import { utcShort } from './render.js';

const CASE_ITEMS_MAX = 30;

function entitiesOf(doc) {
  const out = [];
  const seen = new Set();
  const add = (row, sec) => {
    const e = row && row.entity;
    if (!e || !e.kind || e.id == null) return;
    const key = `${e.kind}|${e.id}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({ entity: e, payload: { briefing: doc.id, section: sec.region, title: row.title, line: row.line || '',
      severity: row.severity ?? null, source: row.source || '', time: row.time ?? null, unverified: !!row.unverified } });
  };
  const secs = doc.sections || [];
  for (const sec of secs) if (sec.id === 'headline') sec.items.forEach(it => add(it, sec));
  for (const sec of secs) {
    if (sec.id === 'headline') continue;
    (sec.items || []).forEach(it => add(it, sec));
    (sec.trends || []).filter(t => t.notable).forEach(t => add(t, sec));
  }
  return out.slice(0, CASE_ITEMS_MAX);
}

export async function saveAsCasefile(ctx, doc) {
  const title = `Briefing, ${utcShort(doc.time)}`;
  const rows = entitiesOf(doc);
  try {
    const id = await ctx.casefiles.create(title);
    const parts = [String(doc.text || '')];
    if (doc.ai && doc.ai.text) {
      const who = doc.ai.provider_label || doc.ai.provider || 'the AI assistant';
      parts.push(`AI narrative (written by ${who}; it may contain mistakes):\n\n${doc.ai.text}`);
    }
    parts.push(`Briefing ${doc.id}. Conflict and news items are machine-coded by GDELT and unverified.`);
    await ctx.api.post('/api/core/casefiles', { action: 'notes', id, notes: parts.join('\n\n').slice(0, 100000) });
    let added = 0;
    for (const r of rows) {
      try { await ctx.casefiles.addItem(id, { ...r.entity, title: r.entity.title || r.payload.title }, r.payload); added += 1; } catch { /* skip one bad item */ }
    }
    ctx.ui.toast(`Saved as case file “${title}” with ${added} item${added === 1 ? '' : 's'}.`, 'green', 5000);
  } catch (err) {
    ctx.ui.toast(`The case file could not be saved: ${err.message || 'unknown error'}`, 'red');
  }
}

export async function downloadMarkdown(ctx, doc) {
  try {
    const text = await ctx.api.get(`/api/briefing/${encodeURIComponent(doc.id)}/markdown`);
    const name = `meridian-briefing-${String(doc.id).replace('T', '-').replace(/Z.*$/, '')}.md`;
    ctx.dom.download(name, typeof text === 'string' ? text : JSON.stringify(text), 'text/markdown');
  } catch (err) {
    ctx.ui.toast(`The Markdown file could not be made: ${err.message || 'unknown error'}`, 'red');
  }
}
