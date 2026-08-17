/**
 * admin/telegram.js — Telegram bot management for the Admin UI.
 *
 * Exports initTelegram({ showToast, showConfirm }) → { refresh }
 */

import { adminFetch } from '/js/admin/auth.js';
import { closeEditorPage, openEditorPage } from '/js/admin/editor-page.js?v=1';

// ── API helpers ──────────────────────────────────────────────────────────────

async function apiFetch(path, options = {}) {
  const res = await adminFetch(path, options);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const body = await res.json(); if (body.detail) detail = body.detail; } catch (_) {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

const api = {
  listBots:    ()            => apiFetch('/api/telegram/bots/'),
  getBot:      (id)          => apiFetch(`/api/telegram/bots/${id}`),
  createBot:   (body)        => apiFetch('/api/telegram/bots/', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  updateBot:   (id, body)    => apiFetch(`/api/telegram/bots/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  deleteBot:   (id)          => apiFetch(`/api/telegram/bots/${id}`, { method: 'DELETE' }),
  enableBot:   (id)          => apiFetch(`/api/telegram/bots/${id}/enable`, { method: 'POST' }),
  disableBot:  (id)          => apiFetch(`/api/telegram/bots/${id}/disable`, { method: 'POST' }),
  testBot:     (id)          => apiFetch(`/api/telegram/bots/${id}/test`, { method: 'POST' }),
  listChars:   ()            => apiFetch('/api/characters/'),
};

// ── DOM refs ─────────────────────────────────────────────────────────────────

const listEl    = document.getElementById('tg-bot-list');
const addBtn    = document.getElementById('tg-add-bot-btn');

const dialog      = document.getElementById('tg-dialog');
const dialogTitle = document.getElementById('tg-dialog-title');
const dialogFb    = document.getElementById('tg-dialog-feedback');
const closeBtn    = document.getElementById('tg-dialog-close');
const cancelBtn   = document.getElementById('tg-dialog-cancel');
const saveBtn     = document.getElementById('tg-dialog-save');

const nameInput   = document.getElementById('tg-bot-name');
const tokenInput  = document.getElementById('tg-bot-token');
const charSelect  = document.getElementById('tg-bot-char');
const enabledChk  = document.getElementById('tg-bot-enabled');
const tokenHint   = document.getElementById('tg-token-hint');
const modeSelect  = document.getElementById('tg-bot-mode');
const whRow       = document.getElementById('tg-webhook-row');
const whSecretRow = document.getElementById('tg-webhook-secret-row');
const whUrlInput  = document.getElementById('tg-bot-webhook-url');
const whSecretInp = document.getElementById('tg-bot-webhook-secret');
const whPreview   = document.getElementById('tg-webhook-preview');

// ── State ────────────────────────────────────────────────────────────────────

let _editId = null;
let _showToast = () => {};
let _showConfirm = () => Promise.resolve(false);

// ── Rendering ─────────────────────────────────────────────────────────────────

function _statusBadge(bot) {
  if (!bot.enabled) return '<span class="badge badge-disabled">Disabled</span>';
  return '<span class="badge badge-running">Enabled</span>';
}

function _renderBot(bot) {
  const div = document.createElement('div');
  div.className = 'connector-card';
  div.dataset.id = bot.id;

  const usernameHtml = bot.telegram_username
    ? `<span class="conn-meta">@${_esc(bot.telegram_username)}</span>`
    : '';

  const modeHtml = bot.update_mode === 'webhook'
    ? '<span class="conn-meta">webhook</span>'
    : '';

  const whErr = bot.webhook_last_error
    ? `<div class="conn-error" title="${_esc(bot.webhook_last_error)}">⚠ webhook: ${_esc(bot.webhook_last_error.slice(0, 80))}</div>`
    : '';

  const lastErr = bot.last_error
    ? `<div class="conn-error" title="${_esc(bot.last_error)}">⚠ ${_esc(bot.last_error.slice(0, 80))}</div>`
    : '';

  div.innerHTML = `
    <div class="conn-main">
      <div class="conn-left">
        <span class="conn-name">${_esc(bot.name)}</span>
        ${usernameHtml}
        ${modeHtml}
        ${_statusBadge(bot)}
        ${lastErr}
        ${whErr}
      </div>
      <div class="conn-actions">
        <button class="btn btn-sm btn-secondary tg-edit-btn">Edit</button>
        <button class="btn btn-sm btn-secondary tg-test-btn">Test</button>
        ${bot.enabled
          ? '<button class="btn btn-sm btn-secondary tg-disable-btn">Disable</button>'
          : '<button class="btn btn-sm btn-primary tg-enable-btn">Enable</button>'}
        <button class="btn btn-sm btn-danger tg-delete-btn">Delete</button>
      </div>
    </div>
  `;

  div.querySelector('.tg-edit-btn').addEventListener('click', () => _openEdit(bot));
  div.querySelector('.tg-test-btn').addEventListener('click', () => _testBot(bot.id));
  div.querySelector('.tg-delete-btn').addEventListener('click', () => _deleteBot(bot.id, bot.name));

  const toggleBtn = div.querySelector('.tg-enable-btn, .tg-disable-btn');
  if (toggleBtn) {
    toggleBtn.addEventListener('click', () => bot.enabled ? _disableBot(bot.id) : _enableBot(bot.id));
  }

  return div;
}

async function _refresh() {
  listEl.innerHTML = '<div class="loading-row">Loading…</div>';
  try {
    const bots = await api.listBots();
    listEl.innerHTML = '';
    if (!bots.length) {
      listEl.innerHTML = '<div class="loading-row muted">No Telegram bots configured.</div>';
      return;
    }
    bots.forEach(b => listEl.appendChild(_renderBot(b)));
  } catch (e) {
    listEl.innerHTML = `<div class="loading-row error">Failed to load: ${_esc(e.message)}</div>`;
  }
}

// ── Dialog helpers ────────────────────────────────────────────────────────────

async function _populateCharSelect(selectedId = '') {
  charSelect.innerHTML = '<option value="">Loading…</option>';
  try {
    const chars = await api.listChars();
    charSelect.innerHTML = '';
    chars.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.id;
      opt.textContent = c.name || c.id;
      if (c.id === selectedId) opt.selected = true;
      charSelect.appendChild(opt);
    });
    if (!chars.length) {
      charSelect.innerHTML = '<option value="">— No characters —</option>';
    }
  } catch (_) {
    charSelect.innerHTML = '<option value="">— Error loading characters —</option>';
  }
}

/** Public base URL of this installation, detected from the admin page. */
function _detectBaseUrl() {
  const { protocol, host } = window.location;
  // Telegram only accepts HTTPS webhooks; suggest https:// even on a local
  // http:// admin page so the operator sees what is actually required.
  return `${protocol === 'http:' ? 'https:' : protocol}//${host}`;
}

function _randomSecret() {
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
}

function _updateWebhookPreview() {
  const base = (whUrlInput.value || '').trim().replace(/\/+$/, '');
  whPreview.textContent = `${base || 'https://…'}/api/telegram/webhook/<bot-id>`;
}

function _syncModeFields() {
  const isWebhook = modeSelect.value === 'webhook';
  whRow.style.display = isWebhook ? '' : 'none';
  whSecretRow.style.display = isWebhook ? '' : 'none';
  if (isWebhook && !whUrlInput.value.trim()) whUrlInput.value = _detectBaseUrl();
  _updateWebhookPreview();
}

function _openDialog(title, bot = null) {
  dialogTitle.textContent = title;
  dialogFb.textContent = '';
  nameInput.value = bot?.name || '';
  tokenInput.value = '';
  tokenHint.style.display = bot ? '' : 'none';
  // New bots are enabled by default; editing keeps the bot's current state.
  enabledChk.checked = bot?.enabled ?? true;
  modeSelect.value = bot?.update_mode || 'polling';
  // Prefill the public URL with the domain this admin UI is served from.
  whUrlInput.value = bot?.webhook_url || _detectBaseUrl();
  whSecretInp.value = '';
  whSecretInp.placeholder = bot?.webhook_secret_set
    ? 'Leave blank to keep existing secret'
    : 'Leave blank to auto-generate';
  _syncModeFields();
  _editId = bot?.id || null;
  _populateCharSelect(bot?.character_id || '');
  openEditorPage(dialog);
  nameInput.focus();
}

function _closeDialog() {
  closeEditorPage(dialog);
  _editId = null;
}

function _openAdd() {
  _openDialog('Add Telegram Bot');
}

function _openEdit(bot) {
  _openDialog('Edit Telegram Bot', bot);
}

async function _save() {
  const name = nameInput.value.trim();
  const token = tokenInput.value.trim();
  const character_id = charSelect.value;
  const enabled = enabledChk.checked;
  const update_mode = modeSelect.value;
  const webhook_url = whUrlInput.value.trim().replace(/\/+$/, '');

  if (!name) { dialogFb.textContent = 'Name is required.'; return; }
  if (!_editId && !token) { dialogFb.textContent = 'Token is required for new bots.'; return; }
  if (!character_id) { dialogFb.textContent = 'Please select a character.'; return; }
  if (update_mode === 'webhook') {
    if (!webhook_url) { dialogFb.textContent = 'A public base URL is required in webhook mode.'; return; }
    if (!/^https:\/\//.test(webhook_url)) {
      dialogFb.textContent = 'The public base URL must start with https:// — Telegram refuses plain HTTP.';
      return;
    }
  }
  const secret = whSecretInp.value.trim();

  saveBtn.disabled = true;
  dialogFb.textContent = 'Saving…';
  try {
    if (_editId) {
      const body = { name, character_id, enabled, update_mode, webhook_url };
      if (token) body.token = token;  // only send if non-empty
      if (secret) body.webhook_secret = secret;
      await api.updateBot(_editId, body);
    } else {
      await api.createBot({
        name, token, character_id, enabled, update_mode, webhook_url,
        webhook_secret: update_mode === 'webhook' ? (secret || _randomSecret()) : '',
      });
    }
    _closeDialog();
    _showToast(_editId ? 'Bot updated.' : 'Bot created.', false);
    await _refresh();
  } catch (e) {
    dialogFb.textContent = e.message;
  } finally {
    saveBtn.disabled = false;
  }
}

// ── Actions ──────────────────────────────────────────────────────────────────

async function _testBot(id) {
  _showToast('Testing connection…', false);
  try {
    const result = await api.testBot(id);
    if (result.last_error) {
      _showToast(`Test failed: ${result.last_error}`, true);
    } else {
      _showToast(`Connected as @${result.telegram_username}`, false);
    }
    await _refresh();
  } catch (e) {
    _showToast(`Test error: ${e.message}`, true);
  }
}

async function _enableBot(id) {
  try {
    await api.enableBot(id);
    _showToast('Bot enabled.', false);
    await _refresh();
  } catch (e) {
    _showToast(`Error: ${e.message}`, true);
  }
}

async function _disableBot(id) {
  try {
    await api.disableBot(id);
    _showToast('Bot disabled.', false);
    await _refresh();
  } catch (e) {
    _showToast(`Error: ${e.message}`, true);
  }
}

async function _deleteBot(id, name) {
  const ok = await _showConfirm(`Delete Telegram bot "${name}"? This cannot be undone.`);
  if (!ok) return;
  try {
    await api.deleteBot(id);
    _showToast('Bot deleted.', false);
    await _refresh();
  } catch (e) {
    _showToast(`Error: ${e.message}`, true);
  }
}

// ── Utility ───────────────────────────────────────────────────────────────────

function _esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Init ──────────────────────────────────────────────────────────────────────

export function initTelegram({ showToast, showConfirm }) {
  _showToast = showToast;
  _showConfirm = showConfirm;

  addBtn.addEventListener('click', _openAdd);
  closeBtn.addEventListener('click', _closeDialog);
  cancelBtn.addEventListener('click', _closeDialog);
  saveBtn.addEventListener('click', _save);
  modeSelect.addEventListener('change', _syncModeFields);
  whUrlInput.addEventListener('input', _updateWebhookPreview);

  return { refresh: _refresh };
}
