// A small, safe Markdown renderer for the assistant's replies.
//
// parseMarkdown() turns text into a plain tree; renderMarkdown() builds DOM from that tree only
// through the h() it is given (ctx.h), so every piece of text becomes a text node and nothing is
// ever parsed as HTML (no HTML string APIs are used at all). Links must be absolute http(s) URLs
// that pass safeUrl(); anything else is shown as plain text.
//
// Supported: paragraphs and line breaks, bullet and numbered lists (nested by indentation),
// **bold**, __bold__, *italic*, _italic_, `code`, fenced code blocks, [links](https://...), bare
// https:// links, > quotes, --- rules and simple pipe tables. Headings show as bold lines.

const MAX_INPUT = 20000;
const MAX_DEPTH = 5;

const FENCE = /^\s{0,3}(`{3,}|~{3,})/;
const RULE = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
const HEADING = /^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$/;
const QUOTE = /^\s{0,3}>\s?/;
const ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const TABLE = /^\s*\|.*\|\s*$/;
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function isBlank(line) { return !line || !line.trim(); }

function startsBlock(line) {
  return FENCE.test(line) || RULE.test(line) || HEADING.test(line) || QUOTE.test(line) || ITEM.test(line);
}

/** Markdown text -> [{type: 'p'|'heading'|'ul'|'ol'|'code'|'quote'|'hr'|'table', ...}] */
export function parseMarkdown(src, depth = 0) {
  const text = String(src ?? '').replace(/\r\n?/g, '\n').replace(/\t/g, '    ').slice(0, MAX_INPUT);
  const lines = text.split('\n');
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (isBlank(line)) { i++; continue; }
    const fence = FENCE.exec(line);
    if (fence) {
      const mark = fence[1];
      const body = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(mark)) body.push(lines[i++]);
      i++;   // the closing fence (or the end)
      blocks.push({ type: 'code', text: body.join('\n') });
      continue;
    }
    if (RULE.test(line)) { blocks.push({ type: 'hr' }); i++; continue; }
    const heading = HEADING.exec(line);
    if (heading) { blocks.push({ type: 'heading', inlines: parseInline(heading[1]) }); i++; continue; }
    if (QUOTE.test(line)) {
      const inner = [];
      while (i < lines.length && QUOTE.test(lines[i])) inner.push(lines[i++].replace(QUOTE, ''));
      blocks.push(depth < MAX_DEPTH ? { type: 'quote', blocks: parseMarkdown(inner.join('\n'), depth + 1) }
        : { type: 'p', inlines: parseInline(inner.join('\n')) });
      continue;
    }
    if (ITEM.test(line)) {
      const [list, next] = parseList(lines, i, depth);
      blocks.push(list);
      i = next;
      continue;
    }
    if (TABLE.test(line)) {
      const rows = [];
      while (i < lines.length && TABLE.test(lines[i])) {
        if (!TABLE_RULE.test(lines[i])) rows.push(splitRow(lines[i]));
        i++;
      }
      blocks.push({ type: 'table', rows });
      continue;
    }
    const para = [line.trim()];
    i++;
    while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines[i]) && !TABLE.test(lines[i])) para.push(lines[i++].trim());
    blocks.push({ type: 'p', inlines: parseInline(para.join('\n')) });
  }
  return blocks;
}

function splitRow(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => parseInline(c.trim()));
}

function indentOf(line) { return line.length - line.trimStart().length; }

function parseList(lines, start, depth) {
  const first = ITEM.exec(lines[start]);
  const base = first[1].length;
  const ordered = /\d/.test(first[2]);
  const list = { type: ordered ? 'ol' : 'ul', start: ordered ? parseInt(first[2], 10) : 1, items: [] };
  let item = null;
  let i = start;
  while (i < lines.length) {
    const line = lines[i];
    if (isBlank(line)) {
      let j = i + 1;
      while (j < lines.length && isBlank(lines[j])) j++;
      const m = j < lines.length ? ITEM.exec(lines[j]) : null;
      if (m && m[1].length >= base && (m[1].length > base || /\d/.test(m[2]) === ordered)) { i = j; continue; }
      break;
    }
    const m = ITEM.exec(line);
    if (m) {
      const indent = m[1].length;
      if (indent < base) break;
      if (indent >= base + 2 && item && depth < MAX_DEPTH) {
        const [child, next] = parseList(lines, i, depth + 1);
        item.children.push(child);
        i = next;
        continue;
      }
      if (/\d/.test(m[2]) !== ordered) break;
      item = { lines: [m[3]], children: [] };
      list.items.push(item);
      i++;
      continue;
    }
    if (item && (indentOf(line) > base || !startsBlock(line))) {   // a continuation line of the item
      if (startsBlock(line) && indentOf(line) <= base) break;
      item.lines.push(line.trim());
      i++;
      continue;
    }
    break;
  }
  for (const it of list.items) { it.inlines = parseInline(it.lines.join('\n')); delete it.lines; }
  return [list, i];
}

// ------------------------------------------------------------------ inline
const ESCAPABLE = /[\\`*_[\]()#+\-.!>|~{}]/;

function wordChar(c) { return !!c && /[\p{L}\p{N}]/u.test(c); }

function canOpen(text, i, delim) {
  const next = text[i + delim.length];
  if (!next || /\s/.test(next)) return false;
  if (delim[0] === '_' && wordChar(text[i - 1])) return false;   // snake_case stays as it is
  return true;
}

function findClose(text, from, delim) {
  const c = delim[0];
  let j = text.indexOf(delim, from);
  while (j >= 0) {
    const prev = text[j - 1];
    const after = text[j + delim.length];
    const okPrev = prev && !/\s/.test(prev);
    const okAfter = c !== '_' || !wordChar(after);
    const single = delim.length === 1 && (text[j + 1] === c || (prev === c && j - 1 >= from));
    if (j > from && okPrev && okAfter && !single) return j;
    j = text.indexOf(delim, j + 1);
  }
  return -1;
}

function matchLink(text, i) {
  const close = text.indexOf(']', i + 1);
  if (close < 0 || text[close + 1] !== '(') return null;
  const label = text.slice(i + 1, close);
  if (!label.trim() || label.includes('\n') || label.includes('[')) return null;
  let depth = 0;
  let j = close + 2;
  for (; j < text.length; j++) {
    const c = text[j];
    if (c === '\n') return null;
    if (c === '(') depth++;
    else if (c === ')') { if (depth === 0) break; depth--; }
  }
  if (j >= text.length) return null;
  let href = text.slice(close + 2, j).trim();
  const titled = /^(\S+)\s+["'(].*["')]$/.exec(href);
  if (titled) href = titled[1];
  if (href.startsWith('<') && href.endsWith('>')) href = href.slice(1, -1);
  return { label, href, end: j + 1 };
}

function trimUrl(url) {
  let u = url.replace(/[.,;:!?'"]+$/, '');
  while (u.endsWith(')') && (u.match(/\(/g) || []).length < (u.match(/\)/g) || []).length) u = u.slice(0, -1);
  return u;
}

/** Inline Markdown -> [{type: 'text'|'br'|'code'|'strong'|'em'|'link', ...}] */
export function parseInline(text, depth = 0, inLink = false) {
  const s = String(text ?? '');
  const out = [];
  let buf = '';
  const flush = () => { if (buf) { out.push({ type: 'text', text: buf }); buf = ''; } };
  let i = 0;
  while (i < s.length) {
    const c = s[i];
    if (c === '\\' && i + 1 < s.length && ESCAPABLE.test(s[i + 1])) { buf += s[i + 1]; i += 2; continue; }
    if (c === '\n') { flush(); out.push({ type: 'br' }); i++; continue; }
    if (c === '`') {
      let run = 1;
      while (s[i + run] === '`') run++;
      const fence = '`'.repeat(run);
      const end = s.indexOf(fence, i + run);
      if (end > i) {
        flush();
        let code = s.slice(i + run, end).replace(/\n/g, ' ');
        if (/^ .*[^ ].* $/.test(code)) code = code.slice(1, -1);
        out.push({ type: 'code', text: code });
        i = end + run;
        continue;
      }
      buf += fence;
      i += run;
      continue;
    }
    if (c === '[' && !inLink && depth < MAX_DEPTH) {
      const link = matchLink(s, i);
      if (link) {
        flush();
        out.push({ type: 'link', href: link.href, children: parseInline(link.label, depth + 1, true) });
        i = link.end;
        continue;
      }
    }
    if ((c === '*' || c === '_') && depth < MAX_DEPTH) {
      const delim = s[i + 1] === c ? c + c : c;
      if (canOpen(s, i, delim)) {
        const end = findClose(s, i + delim.length, delim);
        if (end > 0) {
          flush();
          out.push({ type: delim.length === 2 ? 'strong' : 'em', children: parseInline(s.slice(i + delim.length, end), depth + 1, inLink) });
          i = end + delim.length;
          continue;
        }
      }
      buf += delim;
      i += delim.length;
      continue;
    }
    if (!inLink && (c === 'h' || c === 'H') && !wordChar(s[i - 1])) {
      const m = /^https?:\/\/[^\s<>"'`]+/i.exec(s.slice(i, i + 2048));
      if (m) {
        const url = trimUrl(m[0]);
        if (url.length > 8) {
          flush();
          out.push({ type: 'link', href: url, children: [{ type: 'text', text: url }], auto: true });
          i += url.length;
          continue;
        }
      }
    }
    buf += c;
    i++;
  }
  flush();
  return out;
}

// ------------------------------------------------------------------ DOM
function linkHref(url, safeUrl) {
  const u = safeUrl(url);
  return u && /^https?:\/\/[^/\s]/i.test(u) ? u : null;
}

function inlineNodes(nodes, h, safeUrl) {
  const out = [];
  for (const n of nodes) {
    if (n.type === 'text') out.push(n.text);
    else if (n.type === 'br') out.push(h('br'));
    else if (n.type === 'code') out.push(h('code', null, n.text));
    else if (n.type === 'strong') out.push(h('strong', null, inlineNodes(n.children, h, safeUrl)));
    else if (n.type === 'em') out.push(h('em', null, inlineNodes(n.children, h, safeUrl)));
    else if (n.type === 'link') {
      const href = linkHref(n.href, safeUrl);
      const kids = inlineNodes(n.children, h, safeUrl);
      out.push(href ? h('a', { href, title: href }, kids) : kids);
    }
  }
  return out;
}

function blockNode(b, h, safeUrl) {
  switch (b.type) {
    case 'p': return h('p', null, inlineNodes(b.inlines, h, safeUrl));
    case 'heading': return h('p', { class: 'ai-md-h' }, h('strong', null, inlineNodes(b.inlines, h, safeUrl)));
    case 'code': return h('pre', null, h('code', null, b.text));
    case 'hr': return h('hr');
    case 'quote': return h('blockquote', null, b.blocks.map(x => blockNode(x, h, safeUrl)));
    case 'ul':
    case 'ol':
      return h(b.type, b.type === 'ol' && b.start !== 1 ? { start: b.start } : null,
        b.items.map(it => h('li', null, inlineNodes(it.inlines, h, safeUrl), it.children.map(x => blockNode(x, h, safeUrl)))));
    case 'table': {
      if (!b.rows.length) return null;
      const [head, ...rest] = b.rows;
      return h('div', { class: 'ai-md-table' }, h('table', null,
        h('thead', null, h('tr', null, head.map(cell => h('th', null, inlineNodes(cell, h, safeUrl))))),
        h('tbody', null, rest.map(row => h('tr', null, row.map(cell => h('td', null, inlineNodes(cell, h, safeUrl))))))));
    }
    default: return null;
  }
}

/** Markdown text -> an array of DOM nodes built with h(). */
export function renderMarkdown(src, { h, safeUrl }) {
  return parseMarkdown(src).map(b => blockNode(b, h, safeUrl)).filter(Boolean);
}
