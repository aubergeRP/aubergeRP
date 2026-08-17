/**
 * admin/conversations.js — Conversation management for the Admin UI.
 *
 * Exports initConversations({ showToast, showConfirm }) -> { refresh }
 *
 * Features:
 *  - Table of every conversation (all owners)
 *  - History viewer in a modal
 *  - Inject a message (user / assistant / system) into a history
 *  - Clear a history, or delete the conversation entirely
 */

import { adminFetch } from '/js/admin/auth.js';

async function apiFetch(path, options = {}) {
  const res = await adminFetch(path, options);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body.detail) detail = body.detail;
    } catch (_) {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

const api = {
  list: () => apiFetch('/api/conversations/admin/all'),
  get: (id) => apiFetch(`/api/conversations/${id}`),
  inject: (id, role, content) => apiFetch(`/api/conversations/admin/${id}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role, content }),
  }),
  clear: (id) => apiFetch(`/api/conversations/admin/${id}/messages`, { method: 'DELETE' }),
  remove: (id) => apiFetch(`/api/conversations/admin/${id}`, { method: 'DELETE' }),
};

// DOM refs
const listEl        = document.getElementById('conversations-list');
const feedbackEl    = document.getElementById('conversations-feedback');
const refreshBtn    = document.getElementById('refresh-conversations-btn');
const filterEl      = document.getElementById('conversations-filter');
const dialog        = document.getElementById('conversation-dialog');
const dialogClose   = document.getElementById('conversation-dialog-close');
const dialogTitle   = document.getElementById('conversation-dialog-title');
const messagesEl    = document.getElementById('conversation-messages');
const injectForm    = document.getElementById('conversation-inject-form');
const injectRoleEl  = document.getElementById('conversation-inject-role');
const injectTextEl  = document.getElementById('conversation-inject-content');

let showToastFn   = () => {};
let showConfirmFn = () => Promise.resolve(false);

let conversations = [];
let openId = null;

export function initConversations({ showToast, showConfirm }) {
  showToastFn   = showToast;
  showConfirmFn = showConfirm;

  refreshBtn?.addEventListener('click', refresh);
  filterEl?.addEventListener('input', renderTable);
  listEl?.addEventListener('click', handleListClick);

  dialogClose?.addEventListener('click', closeDialog);
  dialog?.addEventListener('click', (e) => { if (e.target === dialog) closeDialog(); });
  injectForm?.addEventListener('submit', handleInject);

  return { refresh };
}

async function refresh() {
  if (!listEl) return;
  if (feedbackEl) feedbackEl.innerHTML = '';
  listEl.innerHTML = '<div class="loading-row">Loading…</div>';
  try {
    conversations = await api.list() || [];
    renderTable();
  } catch (err) {
    listEl.innerHTML = `<div class="error-banner">Cannot load conversations: ${escHtml(err.message)}</div>`;
  }
}

// ─── Table ───────────────────────────────────────────────────────────────────

function filtered() {
  const q = (filterEl?.value || '').trim().toLowerCase();
  if (!q) return conversations;
  return conversations.filter(c =>
    `${c.character_name} ${c.title} ${c.id}`.toLowerCase().includes(q));
}

function renderTable() {
  if (!listEl) return;
  const items = filtered();
  if (!items.length) {
    listEl.innerHTML = '<div class="loading-row">No conversation found.</div>';
    return;
  }
  const rows = items.map(renderRow).join('');
  listEl.innerHTML = `
    <table class="medias-table">
      <thead>
        <tr>
          <th>Character</th>
          <th>Title</th>
          <th>Messages</th>
          <th>Created</th>
          <th>Last activity</th>
          <th></th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderRow(conv) {
  return `
    <tr data-conv-id="${escAttr(conv.id)}">
      <td>${escHtml(conv.character_name)}</td>
      <td title="${escAttr(conv.id)}">${escHtml(conv.title)}</td>
      <td>${conv.message_count}</td>
      <td>${escHtml(formatDateTime(conv.created_at))}</td>
      <td>${escHtml(formatDateTime(conv.updated_at))}</td>
      <td class="col-actions">
        <button class="btn btn-secondary btn-sm" data-action="view-conv">History</button>
        <button class="btn btn-secondary btn-sm" data-action="clear-conv">Clear</button>
        <button class="btn btn-danger btn-sm" data-action="delete-conv">Delete</button>
      </td>
    </tr>`;
}

async function handleListClick(event) {
  const target = event.target;
  if (!(target instanceof Element)) return;
  const action = target.getAttribute('data-action');
  if (!action) return;
  const id = target.closest('tr')?.getAttribute('data-conv-id');
  if (!id) return;

  if (action === 'view-conv') {
    await openDialog(id);
    return;
  }

  if (action === 'clear-conv') {
    const ok = await showConfirmFn('Delete every message of this conversation? The conversation itself is kept.');
    if (!ok) return;
    try {
      await api.clear(id);
      showToastFn('History cleared.', false);
      if (openId === id) await openDialog(id);
      await refresh();
    } catch (err) {
      showToastFn(`Clear failed: ${err.message}`, true);
    }
    return;
  }

  if (action === 'delete-conv') {
    const ok = await showConfirmFn('Delete this conversation and all its messages?');
    if (!ok) return;
    try {
      await api.remove(id);
      showToastFn('Conversation deleted.', false);
      if (openId === id) closeDialog();
      await refresh();
    } catch (err) {
      showToastFn(`Delete failed: ${err.message}`, true);
    }
  }
}

// ─── History dialog ──────────────────────────────────────────────────────────

async function openDialog(id) {
  if (!dialog) return;
  openId = id;
  messagesEl.innerHTML = '<div class="loading-row">Loading…</div>';
  if (!dialog.open) dialog.showModal();
  try {
    const conv = await api.get(id);
    dialogTitle.textContent = `${conv.character_name} — ${conv.title}`;
    renderMessages(conv.messages || []);
  } catch (err) {
    messagesEl.innerHTML = `<div class="error-banner">Cannot load history: ${escHtml(err.message)}</div>`;
  }
}

function renderMessages(messages) {
  if (!messages.length) {
    messagesEl.innerHTML = '<div class="loading-row">History is empty.</div>';
    return;
  }
  messagesEl.innerHTML = messages.map(m => `
    <div class="conversation-msg conversation-msg-${escAttr(m.role)}">
      <div class="conversation-msg-meta">
        <span class="conversation-msg-role">${escHtml(m.role)}</span>
        <span>${escHtml(formatDateTime(m.timestamp))}</span>
      </div>
      <div class="conversation-msg-body">${escHtml(m.content)}</div>
    </div>`).join('');
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function closeDialog() {
  openId = null;
  if (dialog?.open) dialog.close();
  messagesEl.innerHTML = '';
  dialogTitle.textContent = '';
}

async function handleInject(event) {
  event.preventDefault();
  if (!openId) return;
  const content = injectTextEl.value.trim();
  if (!content) {
    showToastFn('Message content is empty.', true);
    return;
  }
  try {
    await api.inject(openId, injectRoleEl.value, content);
    injectTextEl.value = '';
    showToastFn('Message injected.', false);
    await openDialog(openId);
    await refresh();
  } catch (err) {
    showToastFn(`Injection failed: ${err.message}`, true);
  }
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function formatDateTime(value) {
  if (!value) return '-';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString();
}

function escHtml(input) {
  return String(input)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function escAttr(input) {
  return escHtml(input).replace(/`/g, '&#96;');
}
