// UI primitives (CONTRACT.md section 10.8): toasts, menus, modals, drawers, toolbar buttons, shortcuts.

import { h, clear, svgIcon } from './dom.js';

// ------------------------------------------------------------------ toasts
const MAX_TOASTS = 6;

/** toast(message, tone?, ms?, {title?, action?: {text, onClick}}). Tones: red amber green blue. */
export function toast(message, tone = '', ms = 3500, { title = null, action = null } = {}) {
  const root = document.getElementById('toasts');
  const btn = action && action.text ? h('button', { class: 'toast-action', type: 'button' }, action.text) : null;
  const text = title ? h('span', { class: 'toast-text' }, h('strong', { class: 'toast-title' }, title), message ? h('span', { class: 'toast-body' }, message) : null) : message;
  const el = h('div', { class: `toast ${tone || ''}${btn ? ' has-action' : ''}` }, text, btn);
  if (btn) btn.addEventListener('click', () => { el.remove(); try { action.onClick?.(); } catch (err) { console.error(err); } });
  root.append(el);
  while (root.children.length > MAX_TOASTS) root.firstElementChild.remove();
  setTimeout(() => el.remove(), ms);
  return el;
}

// ------------------------------------------------------------------ menus
let openMenuEl = null;

function closeMenu() {
  if (openMenuEl) { openMenuEl.remove(); openMenuEl = null; }
}

/** items: [{text, note?, checked?, onClick, disabled?} | {separator: true} | {label: 'text'}] */
export function openMenu(anchor, items, { align = 'right' } = {}) {
  closeMenu();
  const menu = h('div', { class: 'menu', role: 'menu' });
  for (const it of items) {
    if (it.separator) { menu.append(h('hr')); continue; }
    if (it.label) { menu.append(h('div', { class: 'menu-note' }, it.label)); continue; }
    const btn = h('button', { type: 'button', role: it.checked === undefined ? 'menuitem' : 'menuitemcheckbox',
      'aria-checked': it.checked === undefined ? null : String(!!it.checked), disabled: it.disabled || null,
      title: it.note || null }, it.text);
    btn.addEventListener('click', () => { closeMenu(); it.onClick?.(); });
    menu.append(btn);
  }
  document.body.append(menu);
  const r = anchor.getBoundingClientRect();
  const w = menu.offsetWidth;
  const left = align === 'right' ? Math.max(8, r.right - w) : Math.min(window.innerWidth - w - 8, r.left);
  menu.style.left = `${left}px`;
  const below = r.bottom + 6;
  const above = r.top - 6 - menu.offsetHeight;
  menu.style.top = `${below + menu.offsetHeight > window.innerHeight - 8 && above >= 8 ? above : Math.max(8, Math.min(below, window.innerHeight - menu.offsetHeight - 8))}px`;
  openMenuEl = menu;
  menu.querySelector('button')?.focus();
  setTimeout(() => {
    const off = (e) => {
      if (!menu.contains(e.target)) { closeMenu(); document.removeEventListener('pointerdown', off, true); }
    };
    document.addEventListener('pointerdown', off, true);
  }, 0);
  menu.addEventListener('keydown', (e) => {
    const btns = [...menu.querySelectorAll('button:not([disabled])')];
    const i = btns.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { e.preventDefault(); btns[(i + 1) % btns.length]?.focus(); }
    if (e.key === 'ArrowUp') { e.preventDefault(); btns[(i - 1 + btns.length) % btns.length]?.focus(); }
    if (e.key === 'Escape') { closeMenu(); anchor.focus(); }
  });
  return { close: closeMenu, el: menu };
}

/** A context menu at page coordinates. */
export function contextMenu(x, y, items, header) {
  closeMenu();
  const menu = h('div', { class: 'ctx-menu', role: 'menu' });
  if (header) menu.append(h('div', { class: 'ctx-coords' }, header));
  for (const it of items) {
    const b = h('button', { type: 'button', role: 'menuitem' }, it.text);
    b.addEventListener('click', () => { closeMenu(); it.onClick?.(); });
    menu.append(b);
  }
  document.body.append(menu);
  menu.style.left = `${Math.min(x, window.innerWidth - menu.offsetWidth - 8)}px`;
  menu.style.top = `${Math.min(y, window.innerHeight - menu.offsetHeight - 8)}px`;
  openMenuEl = menu;
  menu.querySelector('button')?.focus();
  setTimeout(() => {
    const off = (e) => { if (!menu.contains(e.target)) { closeMenu(); document.removeEventListener('pointerdown', off, true); } };
    document.addEventListener('pointerdown', off, true);
  }, 0);
  menu.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMenu(); });
}

// ------------------------------------------------------------------ modals
const modalStack = [];

/** modal({title, body: Node, wide?, full?, foot?: Node, onClose?}) -> {close, el, body} */
export function modal({ title, body, wide = false, full = false, foot = null, onClose = null }) {
  const root = document.getElementById('modal-root');
  const closeBtn = h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Close' }, svgIcon('M5 5l10 10M15 5 5 15'));
  const bodyEl = h('div', { class: 'modal-body' }, body);
  const box = h('div', { class: `modal${wide ? ' wide' : ''}${full ? ' full' : ''}`, role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
    h('div', { class: 'modal-head' }, h('h2', null, title), closeBtn), bodyEl, foot ? h('div', { class: 'modal-foot' }, foot) : null);
  const backdrop = h('div', { class: 'modal-backdrop' }, box);
  const previous = document.activeElement;
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    backdrop.remove();
    const i = modalStack.indexOf(handle);
    if (i >= 0) modalStack.splice(i, 1);
    if (!modalStack.length) document.getElementById('app')?.removeAttribute('inert');
    try { onClose?.(); } catch (err) { console.error(err); }
    previous?.focus?.();
  };
  closeBtn.addEventListener('click', close);
  backdrop.addEventListener('pointerdown', (e) => { if (e.target === backdrop) close(); });
  root.append(backdrop);
  const handle = { close, el: box, body: bodyEl, setBody(node) { clear(bodyEl); bodyEl.append(node); } };
  modalStack.push(handle);
  document.getElementById('app')?.setAttribute('inert', '');
  closeBtn.focus();
  return handle;
}

export function confirmBox(message, { ok = 'OK', cancel = 'Cancel', danger = false } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const okBtn = h('button', { class: `btn ${danger ? 'danger' : 'primary'}`, type: 'button' }, ok);
    const cancelBtn = h('button', { class: 'btn', type: 'button' }, cancel);
    const m = modal({ title: 'Please confirm', body: h('p', null, message), foot: h('div', { style: { display: 'flex', gap: '8px' } }, cancelBtn, okBtn),
      onClose: () => { if (!done) resolve(false); } });
    okBtn.addEventListener('click', () => { done = true; m.close(); resolve(true); });
    cancelBtn.addEventListener('click', () => { done = true; m.close(); resolve(false); });
    okBtn.focus();
  });
}

export function promptBox(title, { label = '', value = '', ok = 'Save' } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const input = h('input', { type: 'text', value });
    const okBtn = h('button', { class: 'btn primary', type: 'button' }, ok);
    const form = h('div', { class: 'field' }, h('label', null, label), input);
    const m = modal({ title, body: form, foot: okBtn, onClose: () => { if (!done) resolve(null); } });
    const submit = () => { done = true; m.close(); resolve(input.value.trim()); };
    okBtn.addEventListener('click', submit);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
    input.focus();
    input.select();
  });
}

// ------------------------------------------------------------------ drawers (floating tool panels over the map)
const drawers = new Map();

/** drawer({id, title, el, width?, onClose?}) -> {close, el, body}. One per id; calling again focuses it. */
export function drawer({ id, title, el, width = 420, onClose = null, tools = null }) {
  if (drawers.has(id)) return drawers.get(id);
  const root = document.getElementById('drawers');
  const closeBtn = h('button', { class: 'icon-btn', type: 'button', 'aria-label': `Close ${title}` }, svgIcon('M5 5l10 10M15 5 5 15'));
  const body = h('div', { class: 'drawer-body' }, el);
  const box = h('section', { class: 'drawer', style: { width: `min(${width}px, calc(100vw - 20px))` }, 'aria-label': title },
    h('div', { class: 'drawer-head' }, h('h2', null, title), tools, closeBtn), body);
  root.append(box);
  const opener = document.activeElement;
  const handle = {
    el: box, body,
    close() {
      const hadFocus = box.contains(document.activeElement);
      box.remove();
      drawers.delete(id);
      try { onClose?.(); } catch (err) { console.error(err); }
      if (hadFocus && opener && document.contains(opener)) opener.focus?.();
    },
  };
  closeBtn.addEventListener('click', () => handle.close());
  drawers.set(id, handle);
  closeBtn.focus({ preventScroll: true });
  return handle;
}

export function getDrawer(id) { return drawers.get(id) || null; }

// ------------------------------------------------------------------ toolbar buttons
const toolButtons = [];

/** toolbarButton({id, title, label?, icon: svg path d, onClick, order?}) -> button element */
export function toolbarButton({ id, title, label = null, icon, onClick, order = 50 }) {
  const bar = document.getElementById('toolbar');
  const btn = h('button', { class: 'icon-btn', type: 'button', id: `tool-${id}`, title, 'aria-label': title, 'aria-pressed': 'false' },
    icon ? svgIcon(icon) : null, label ? h('span', { class: 'label' }, label) : null);
  btn.addEventListener('click', (e) => onClick?.(e, btn));
  toolButtons.push({ order, btn });
  toolButtons.sort((a, b) => a.order - b.order);
  for (const t of toolButtons) bar.append(t.btn);
  return btn;
}

// ------------------------------------------------------------------ keyboard shortcuts
const shortcuts = new Map();

/** shortcut('g', 'Toggle globe', fn). Ignored while typing in an input. */
export function shortcut(key, description, fn) {
  shortcuts.set(key, { description, fn });
}

export function listShortcuts() {
  return [...shortcuts.entries()].map(([key, v]) => ({ key, description: v.description }));
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (openMenuEl) { closeMenu(); return; }
    const top = modalStack[modalStack.length - 1];
    if (top) { top.close(); return; }
    const inDrawer = e.target?.closest?.('.drawer');
    if (inDrawer) {
      const d = [...drawers.values()].find(x => x.el === inDrawer);
      if (d) { d.close(); return; }
    }
  }
  const t = e.target;
  if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const s = shortcuts.get(e.key);
  if (s) { e.preventDefault(); s.fn(e); }
});

export const ui = { toast, openMenu, contextMenu, modal, confirm: confirmBox, prompt: promptBox, drawer, getDrawer, toolbarButton, shortcut, listShortcuts };
