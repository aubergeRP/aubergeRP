import { adminFetch } from '/js/admin/auth.js';
/**
 * admin/config.js — Configuration panel for the Admin UI.
 *
 * Exports initConfig({ showToast }) → { refresh }
 */

// ── API helpers ──────────────────────────────────────────────────────────────

async function apiFetch(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const isWrite = ['POST', 'PUT', 'DELETE', 'PATCH'].includes(method);
  const res = isWrite ? await adminFetch(path, options) : await fetch(path, options);
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

// ── DOM refs ─────────────────────────────────────────────────────────────────

const feedbackEl      = document.getElementById('config-feedback');
const saveBtn         = document.getElementById('config-save-btn');
const cleanupBtn      = document.getElementById('cleanup-images-btn');
const cleanupFeedback = document.getElementById('cleanup-feedback');

// ── State ─────────────────────────────────────────────────────────────────────

let showToastFn = () => {};

// ── Init ──────────────────────────────────────────────────────────────────────

export function initConfig({ showToast }) {
  showToastFn = showToast;
  saveBtn.addEventListener('click', handleSave);
  cleanupBtn.addEventListener('click', handleCleanup);
  return { refresh };
}

// ── Load & render ─────────────────────────────────────────────────────────────

async function refresh() {
  feedbackEl.innerHTML = '';
  try {
    const [cfg, connectors] = await Promise.all([
      apiFetch('/api/config/'),
      apiFetch('/api/connectors/'),
    ]);
    renderForm(cfg, connectors);
  } catch (err) {
    feedbackEl.innerHTML = `<div class="error-banner">Cannot load configuration: ${escHtml(err.message)}</div>`;
  }
}

function renderForm(cfg, connectors) {
  document.getElementById('cfg-host').value      = cfg.app.host      || '';
  document.getElementById('cfg-port').value      = cfg.app.port      || 8123;
  document.getElementById('cfg-log-level').value = cfg.app.log_level || 'INFO';
  document.getElementById('cfg-user-name').value = cfg.user.name     || '';

  populateConnectorSelect('cfg-active-text',  connectors, 'text',  cfg.active_connectors.text);
  populateConnectorSelect('cfg-active-image', connectors, 'image', cfg.active_connectors.image);

  // Advanced: optional per-task text models — empty means "same as main model".
  populateConnectorSelect('cfg-active-text-summarization', connectors, 'text',
    cfg.active_connectors.text_summarization || '', 'Same as main model');
  populateConnectorSelect('cfg-active-text-utility', connectors, 'text',
    cfg.active_connectors.text_utility || '', 'Same as main model');
}

function populateConnectorSelect(selectId, connectors, type, activeId, emptyLabel = '(none)') {
  const sel = document.getElementById(selectId);
  sel.innerHTML = `<option value="">${escHtml(emptyLabel)}</option>`;
  connectors
    .filter(c => c.type === type)
    .forEach(c => {
      const opt = document.createElement('option');
      opt.value       = c.id;
      opt.textContent = c.name;
      if (c.id === activeId) opt.selected = true;
      sel.appendChild(opt);
    });
}

// ── Save ──────────────────────────────────────────────────────────────────────

async function handleSave() {
  feedbackEl.innerHTML = '';
  saveBtn.disabled    = true;
  saveBtn.textContent = 'Saving…';

  const port = parseInt(document.getElementById('cfg-port').value, 10);
  const body = {
    app: {
      host:      document.getElementById('cfg-host').value.trim(),
      port:      isNaN(port) ? 8123 : port,
      log_level: document.getElementById('cfg-log-level').value,
    },
    user: {
      name: document.getElementById('cfg-user-name').value.trim() || 'User',
    },
    active_connectors: {
      text:  document.getElementById('cfg-active-text').value,
      image: document.getElementById('cfg-active-image').value,
      text_summarization: document.getElementById('cfg-active-text-summarization').value,
      text_utility:       document.getElementById('cfg-active-text-utility').value,
    },
  };

  try {
    await apiFetch('/api/config/', {
      method:  'PUT',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });
    showToastFn('Configuration saved.', false);
  } catch (err) {
    feedbackEl.innerHTML = `<div class="error-banner">${escHtml(err.message)}</div>`;
  } finally {
    saveBtn.disabled    = false;
    saveBtn.textContent = 'Save';
  }
}

// ── Cleanup ───────────────────────────────────────────────────────────────────

async function handleCleanup() {
  cleanupFeedback.textContent = '';
  cleanupBtn.disabled = true;
  const days = parseInt(document.getElementById('cfg-cleanup-days').value, 10) || 30;
  try {
    const result = await apiFetch('/api/images/cleanup', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ older_than_days: days }),
    });
    cleanupFeedback.style.color = 'var(--color-success, green)';
    cleanupFeedback.textContent = `Cleanup complete: ${result.deleted} image(s) deleted.`;
  } catch (err) {
    cleanupFeedback.style.color = 'var(--color-error, red)';
    cleanupFeedback.textContent = `Cleanup failed: ${escHtml(err.message)}`;
  } finally {
    cleanupBtn.disabled = false;
  }
}

// ── Util ──────────────────────────────────────────────────────────────────────

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
