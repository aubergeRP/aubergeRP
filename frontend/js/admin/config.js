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
      if (Array.isArray(body.detail)) {
        // FastAPI 422 — surface which field failed instead of "[object Object]".
        detail = body.detail
          .map(e => `${(e.loc || []).slice(1).join('.')}: ${e.msg}`)
          .join(' — ');
      } else if (body.detail) {
        detail = body.detail;
      }
    } catch (_) {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

// ── Field table ──────────────────────────────────────────────────────────────
//
// Declarative map between DOM element ids and config paths, so adding a
// setting is a one-line change instead of two hand-written getElementById
// calls. `kind` drives both the read and the write conversion.
//   text  → string      int   → integer
//   float → number      bool  → checkbox
// Read-only fields are listed with `readOnly: true`: they are displayed but
// never sent back (the API ignores them anyway).

const FIELDS = [
  // section              id                              key                            kind
  ['user',            'cfg-user-name',                'name',                         'text'],

  ['gui',             'cfg-public-character-list',    'public_character_list',        'bool'],

  ['chat',            'cfg-ooc-protection',           'ooc_protection',               'bool'],
  ['chat',            'cfg-image-autonomy',           'image_autonomy',               'bool'],
  ['chat',            'cfg-context-window',           'context_window',               'int'],
  ['chat',            'cfg-summarization-threshold',  'summarization_threshold',      'float'],
  ['chat',            'cfg-image-autonomy-cooldown',  'image_autonomy_cooldown',      'int'],

  ['app',             'cfg-host',                     'host',                         'text'],
  ['app',             'cfg-port',                     'port',                         'int'],
  ['app',             'cfg-log-level',                'log_level',                    'text'],
  ['app',             'cfg-sentry-dsn',               'sentry_dsn',                   'text'],
  ['app',             'cfg-admin-token-ttl',          'admin_token_ttl_seconds',      'int'],
  ['app',             'cfg-data-dir',                 'data_dir',                     'text', true],

  ['scheduler',       'cfg-scheduler-enabled',        'enabled',                      'bool'],
  ['scheduler',       'cfg-scheduler-interval',       'interval_seconds',             'int'],
  ['scheduler',       'cfg-scheduler-cleanup-days',   'cleanup_older_than_days',      'int'],
  ['scheduler',       'cfg-health-check-enabled',     'health_check_enabled',         'bool'],
  ['scheduler',       'cfg-health-check-interval',    'health_check_interval_seconds','int'],

  ['observability',   'cfg-metrics-enabled',          'metrics_enabled',              'bool'],
].map(([section, id, key, kind, readOnly = false]) => ({ section, id, key, kind, readOnly }));

function readInto(cfg) {
  for (const { section, id, key, kind } of FIELDS) {
    const el = document.getElementById(id);
    const value = cfg[section]?.[key];
    if (kind === 'bool') el.checked = value === true;
    else el.value = value ?? '';
  }
}

function collect() {
  const body = {};
  for (const { section, id, key, kind, readOnly } of FIELDS) {
    if (readOnly) continue;
    const el = document.getElementById(id);
    let value;
    if (kind === 'bool') value = el.checked;
    else if (kind === 'int') value = parseInt(el.value, 10);
    else if (kind === 'float') value = parseFloat(el.value);
    else value = el.value.trim();
    // Let the server reject blanks in numeric fields rather than silently
    // substituting a default the admin never chose.
    if ((kind === 'int' || kind === 'float') && Number.isNaN(value)) value = null;
    (body[section] ??= {})[key] = value;
  }
  return body;
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
  readInto(cfg);

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

  const body = collect();
  body.user.name = body.user.name || 'User';
  body.active_connectors = {
    text:  document.getElementById('cfg-active-text').value,
    image: document.getElementById('cfg-active-image').value,
    text_summarization: document.getElementById('cfg-active-text-summarization').value,
    text_utility:       document.getElementById('cfg-active-text-utility').value,
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
