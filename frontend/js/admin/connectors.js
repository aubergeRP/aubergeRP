/**
 * admin/connectors.js — Connector management for the Admin UI.
 *
 * Exports initConnectors({ showToast, showConfirm }) → { refresh }
 */

import { adminFetch } from '/js/admin/auth.js';
import { closeEditorPage, openEditorPage } from '/js/admin/editor-page.js?v=1';

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

const api = {
  listConnectors:     ()       => apiFetch('/api/connectors/'),
  listBackends:       ()       => apiFetch('/api/connectors/backends'),
  createConnector:    (body)   => apiFetch('/api/connectors/', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  updateConnector:    (id, b)  => apiFetch(`/api/connectors/${id}`, { method: 'PUT',  headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) }),
  deleteConnector:    (id)     => apiFetch(`/api/connectors/${id}`, { method: 'DELETE' }),
  testConnector:      (id)     => apiFetch(`/api/connectors/${id}/test`, { method: 'POST' }),
  activateConnector:  (id)     => apiFetch(`/api/connectors/${id}/activate`, { method: 'POST' }),
};

// ── DOM refs ─────────────────────────────────────────────────────────────────

const listEl      = document.getElementById('connector-list');
const addBtn      = document.getElementById('add-connector-btn');

// Dialog elements
const dialog      = document.getElementById('connector-dialog');
const dialogTitle = document.getElementById('connector-dialog-title');
const dialogFeedback = document.getElementById('connector-dialog-feedback');
const closeBtn    = document.getElementById('connector-dialog-close');
const cancelBtn   = document.getElementById('connector-dialog-cancel');
const testBtn     = document.getElementById('connector-dialog-test');
const saveBtn     = document.getElementById('connector-dialog-save');
const nameInput   = document.getElementById('conn-name');
const typeSelect  = document.getElementById('conn-type');
const backendSelect = document.getElementById('conn-backend');
const configFields  = document.getElementById('conn-config-fields');
const clearKeyRow   = document.getElementById('conn-clear-key-row');
const clearKeyChk   = document.getElementById('conn-clear-key');
const nameError     = document.getElementById('conn-name-error');
const backendWarning = document.getElementById('conn-backend-warning');

// ── State ────────────────────────────────────────────────────────────────────

let backends = [];         // from GET /api/connectors/backends
let editingId = null;      // null = new, string = existing id
let existingApiKeySet = false;
let baselineConfig = {};
const testFeedbackById = new Map();
let showToastFn = () => {};
let showConfirmFn = () => Promise.resolve(false);

// ── Init ─────────────────────────────────────────────────────────────────────

export function initConnectors({ showToast, showConfirm }) {
  showToastFn   = showToast;
  showConfirmFn = showConfirm;

  addBtn.addEventListener('click', () => openDialog(null));
  closeBtn.addEventListener('click', closeDialog);
  cancelBtn.addEventListener('click', closeDialog);
  saveBtn.addEventListener('click', handleSave);
  testBtn.addEventListener('click', handleTest);
  typeSelect.addEventListener('change', onTypeChange);
  backendSelect.addEventListener('change', onBackendChange);

  // Close on backdrop click

  loadBackends();
  refresh();

  return { refresh };
}

// ── Load backends schema ──────────────────────────────────────────────────────

async function loadBackends() {
  try {
    backends = await api.listBackends();
    updateBackendOptions();
  } catch (_) {
    // If backends endpoint not available, use a default schema
    backends = [{
      id: 'openai_api',
      name: 'OpenAI-Compatible API',
      supported_types: ['text', 'image', 'video', 'audio'],
      config_schema: {
        common: {
          base_url: { type: 'string', required: true },
          api_key: { type: 'string', required: false },
          model: { type: 'string', required: true },
          timeout: { type: 'number', required: false },
          max_retries: { type: 'number', required: false },
          custom_json: { type: 'object', required: false },
        },
        by_type: {
          text: {
            max_tokens: { type: 'number', required: false },
            temperature: { type: 'number', required: false },
          },
          image: {
            size: { type: 'string', required: false },
          },
        },
      }
    }];
    updateBackendOptions();
  }
}

// ── Render connector list ─────────────────────────────────────────────────────

async function refresh() {
  listEl.innerHTML = '<div class="loading-row">Loading…</div>';
  try {
    const connectors = await api.listConnectors();
    renderList(connectors);
  } catch (err) {
    listEl.innerHTML = `<div class="error-banner">Cannot load connectors: ${err.message}</div>`;
  }
}

const CONNECTOR_TYPES = ['text', 'image', 'video', 'audio'];

function renderList(connectors) {
  const byType = {};
  CONNECTOR_TYPES.forEach(t => { byType[t] = []; });
  connectors.forEach(c => {
    if (!byType[c.type]) byType[c.type] = [];
    byType[c.type].push(c);
  });

  const html = CONNECTOR_TYPES.map(type => {
    const group = byType[type] || [];
    const items = group.length === 0
      ? '<div class="conn-empty">(none)</div>'
      : group.map(c => renderConnectorCard(c)).join('');
    return `
      <div class="conn-type-heading">${type} Connectors</div>
      ${items}
    `;
  }).join('');

  listEl.innerHTML = html || '<div class="loading-row">No connectors configured.</div>';

  // Attach event listeners to action buttons
  listEl.querySelectorAll('[data-action]').forEach(el => {
    el.addEventListener('click', handleCardAction);
  });

  // Dropdown toggles
  listEl.querySelectorAll('.dropdown-toggle').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const menu = btn.nextElementSibling;
      const isOpen = menu.style.display === 'block';
      closeAllDropdowns();
      menu.style.display = isOpen ? 'none' : 'block';
    });
  });

  document.addEventListener('click', closeAllDropdowns, { once: false });
}

function closeAllDropdowns() {
  listEl.querySelectorAll('.dropdown-menu').forEach(m => { m.style.display = 'none'; });
}

function renderConnectorCard(c) {
  const star = c.is_active ? '<span class="conn-active-star" title="Active">⭐</span>' : '<span class="conn-active-star"></span>';
  const statusClass = c.connected === true ? 'ok' : c.connected === false ? 'fail' : 'unknown';
  const statusText  = c.connected === true ? '✅ Connected' : c.connected === false ? '❌ Not connected' : '— Not tested';
  const meta = c.config ? [
    c.config.base_url ? `URL: ${c.config.base_url}` : '',
    c.config.model    ? `Model: ${c.config.model}` : '',
    c.type === 'text' && c.config.context_window ? `ctx: ${c.config.context_window}` : '',
    c.type === 'text' && c.config.max_tokens     ? `max: ${c.config.max_tokens} tok` : '',
  ].filter(Boolean).join(' &nbsp;·&nbsp; ') : '';
  const testFeedback = testFeedbackById.get(c.id);
  const feedbackHtml = testFeedback
    ? `<div class="conn-test-feedback ${testFeedback.level}">${testFeedback.message}</div>`
    : '';

  return `
    <div class="conn-card" data-id="${c.id}">
      ${star}
      <div class="conn-info">
        <div class="conn-name">
          ${escHtml(c.name)}
          <span class="conn-backend-badge">${escHtml(c.backend)}</span>
          ${c.config?.nsfw ? '<span class="conn-nsfw-badge" title="NSFW enabled">NSFW</span>' : ''}
        </div>
        ${meta ? `<div class="conn-meta">${meta}</div>` : ''}
        <div class="conn-status ${statusClass}" id="conn-status-${c.id}">${statusText}</div>
        ${feedbackHtml}
      </div>
      <div class="conn-actions">
        <button class="btn btn-secondary btn-sm" data-action="test" data-id="${c.id}">Test</button>
        <div class="dropdown-wrap">
          <button class="btn-icon dropdown-toggle" title="More actions" aria-label="More actions for ${escHtml(c.name)}">⋮</button>
          <div class="dropdown-menu" style="display:none">
            <button data-action="edit" data-id="${c.id}">Edit</button>
            ${!c.is_active ? `<button data-action="activate" data-id="${c.id}">Activate</button>` : ''}
            <button data-action="delete" data-id="${c.id}" class="danger">Delete</button>
          </div>
        </div>
      </div>
    </div>
  `;
}

async function handleCardAction(e) {
  const btn = e.currentTarget;
  const action = btn.dataset.action;
  const id = btn.dataset.id;
  closeAllDropdowns();

  if (action === 'test') {
    btn.disabled = true;
    btn.textContent = '…';
    try {
      const result = await api.testConnector(id);
      const feedback = formatTestFeedback(result);
      testFeedbackById.set(id, feedback);
    } catch (err) {
      testFeedbackById.set(id, {
        level: 'fail',
        message: `Test failed: ${escHtml(err.message)}`,
      });
    } finally {
      await refresh();
    }
  } else if (action === 'edit') {
    await openDialog(id);
  } else if (action === 'activate') {
    try {
      await api.activateConnector(id);
      showToastFn('Connector activated.', false);
      await refresh();
    } catch (err) {
      showToastFn(`Activate failed: ${err.message}`, true);
    }
  } else if (action === 'delete') {
    const card = listEl.querySelector(`.conn-card[data-id="${id}"]`);
    const name = card?.querySelector('.conn-name')?.childNodes[0]?.textContent?.trim() || 'this connector';
    const ok = await showConfirmFn(`Are you sure you want to delete ${name}? This cannot be undone.`);
    if (!ok) return;
    try {
      await api.deleteConnector(id);
      testFeedbackById.delete(id);
      showToastFn('Connector deleted.', false);
      await refresh();
    } catch (err) {
      showToastFn(`Delete failed: ${err.message}`, true);
    }
  }
}

// ── Dialog ────────────────────────────────────────────────────────────────────

async function openDialog(id) {
  editingId = id;
  clearKeyChk.checked = false;
  clearKeyRow.style.display = 'none';
  backendWarning.style.display = 'none';
  dialogFeedback.innerHTML = '';
  nameError.textContent = '';
  existingApiKeySet = false;
  baselineConfig = {};

  if (id) {
    dialogTitle.textContent = 'Edit Connector';
    try {
      const conn = await apiFetch(`/api/connectors/${id}`);
      nameInput.value = conn.name || '';
      typeSelect.value = conn.type || 'text';
      updateBackendOptions();
      backendSelect.value = conn.backend || 'openai_api';
      updateBackendWarning();
      baselineConfig = sanitizeConfig(conn.config || {});
      renderConfigFields(conn.backend, conn.config || {});
      existingApiKeySet = !!(conn.config && conn.config.api_key_set);
      clearKeyRow.style.display = existingApiKeySet ? '' : 'none';
    } catch (err) {
      showToastFn(`Failed to load connector: ${err.message}`, true);
      return;
    }
  } else {
    dialogTitle.textContent = 'Add New Connector';
    nameInput.value = '';
    typeSelect.value = 'text';
    updateBackendOptions();
    renderConfigFields(backendSelect.value, {});
  }

  openEditorPage(dialog);
  nameInput.focus();
}

function closeDialog() {
  closeEditorPage(dialog);
  editingId = null;
}

function onTypeChange() {
  baselineConfig = {};
  updateBackendOptions();
  renderConfigFields(backendSelect.value, {});
}

function updateBackendWarning() {
  if (!backendWarning) return;
  backendWarning.style.display = backendSelect.value.toLowerCase().includes('comfyui') ? '' : 'none';
}

function onBackendChange() {
  baselineConfig = {};
  updateBackendWarning();
  renderConfigFields(backendSelect.value, {});
}

function updateBackendOptions() {
  const selectedType = typeSelect.value;
  const compatible = backends.filter(b =>
    !b.supported_types || b.supported_types.includes(selectedType)
  );
  const prev = backendSelect.value;
  backendSelect.innerHTML = compatible.map(b =>
    `<option value="${escHtml(b.id)}">${escHtml(b.name || b.id)}</option>`
  ).join('');
  if (compatible.find(b => b.id === prev)) backendSelect.value = prev;
  updateBackendWarning();
}

function renderConfigFields(backendId, existingConfig) {
  const selectedType = typeSelect.value;
  const backend = backends.find(b => b.id === backendId);
  if (!backend || !backend.config_schema) {
    const commonEntries = [
      ['base_url', { type: 'string', required: true }],
      ['api_key', { type: 'string', required: false }],
      ['model', { type: 'string', required: true }],
      ['timeout', { type: 'number', required: false }],
      ['max_retries', { type: 'number', required: false }],
      ['nsfw', { type: 'boolean', required: false }],
    ];
    const typeEntries = selectedType === 'text'
      ? [
          ['max_tokens', { type: 'number', required: false }],
          ['context_window', { type: 'number', required: false }],
          ['temperature', { type: 'number', required: false }],
          ['supports_tool_calling', { type: 'boolean', required: false }],
        ]
      : selectedType === 'image'
        ? [['size', { type: 'string', required: false }]]
        : [];

    configFields.innerHTML = renderConfigSections(commonEntries, typeEntries, selectedType, existingConfig);
    return;
  }

  const splitSchema = splitSchemaByCommonAndType(backend.config_schema, selectedType);
  configFields.innerHTML = renderConfigSections(
    splitSchema.commonEntries,
    splitSchema.typeEntries,
    selectedType,
    existingConfig,
  );

  // Populate workflow select if present
  if (splitSchema.hasWorkflowSelect) {
    populateWorkflowSelect(existingConfig.workflow || 'default');
  }
}

function renderConfigSections(commonEntries, typeEntries, selectedType, existingConfig) {
  const quickEntries = [];
  const advancedCommonEntries = [];
  const advancedTypeEntries = [];

  const backendConfigKeys = new Set(['base_url', 'api_key', 'model']);
  for (const [key, schema] of commonEntries) {
    if (key === 'nsfw' || backendConfigKeys.has(key)) {
      quickEntries.push([key, schema]);
    } else {
      advancedCommonEntries.push([key, schema]);
    }
  }

  for (const [key, schema] of typeEntries) {
    if (key === 'nsfw') {
      quickEntries.push([key, schema]);
    } else {
      advancedTypeEntries.push([key, schema]);
    }
  }

  if (!quickEntries.length) {
    quickEntries.push(['nsfw', { type: 'boolean', required: true }]);
  }

  const quickHtml = renderSchemaEntries(quickEntries, existingConfig);
  const advancedCommonHtml = renderSchemaEntries(advancedCommonEntries, existingConfig);
  const advancedTypeHtml = renderSchemaEntries(advancedTypeEntries, existingConfig);

  let html = quickHtml;

  if (advancedCommonHtml || advancedTypeHtml) {
    html += `
      <details class="advanced-config-details">
        <summary>Advanced Configuration (optional)</summary>
        <div class="advanced-config-body">
          ${advancedCommonHtml ? `<div class="field-divider">Common Fields</div>${advancedCommonHtml}` : ''}
          ${advancedTypeHtml ? `<div class="field-divider">Type-Specific Fields (${escHtml(selectedType)})</div>${advancedTypeHtml}` : ''}
        </div>
      </details>
    `;
  }

  return html;
}

function renderSchemaEntries(entries, existingConfig) {
  return entries
    .filter(([key]) => key !== 'api_key_set')
    .map(([key, schema]) => {
      const isApiKey = key === 'api_key';
      const isNsfw = key === 'nsfw';
      const value = isApiKey ? '' : (existingConfig[key] !== undefined ? existingConfig[key] : '');
      const placeholder = isApiKey && existingApiKeySet ? '(set — leave blank to keep)' : '';
      if (schema.type === 'workflow_select') {
        return renderWorkflowSelect(key, schemaLabel(key), String(value || ''));
      }
      return renderField(
        key,
        schemaLabel(key),
        schema.type || 'string',
        isNsfw ? true : (schema.required || false),
        value,
        placeholder,
        isApiKey ? 'password' : null,
      );
    })
    .join('');
}

function splitSchemaByCommonAndType(configSchema, connectorType) {
  const result = {
    commonEntries: [],
    typeEntries: [],
    hasWorkflowSelect: false,
  };

  if (!configSchema || typeof configSchema !== 'object') return result;

  // New format: { common: {...}, by_type: { text: {...}, image: {...} } }
  if (configSchema.common || configSchema.by_type) {
    const common = configSchema.common && typeof configSchema.common === 'object'
      ? configSchema.common
      : {};
    const byType = configSchema.by_type && typeof configSchema.by_type === 'object'
      ? (configSchema.by_type[connectorType] || {})
      : {};

    result.commonEntries = Object.entries(common);
    result.typeEntries = Object.entries(byType);
    result.hasWorkflowSelect = result.commonEntries.concat(result.typeEntries)
      .some(([, schema]) => schema && schema.type === 'workflow_select');
    return result;
  }

  // Legacy flat format support
  const merged = { ...configSchema };
  if (connectorType === 'text') {
    delete merged.size;
  } else if (connectorType === 'image') {
    delete merged.max_tokens;
    delete merged.temperature;
  } else {
    delete merged.max_tokens;
    delete merged.temperature;
    delete merged.size;
  }

  const commonKeys = new Set(['base_url', 'api_key', 'model', 'timeout', 'nsfw']);
  for (const [key, schema] of Object.entries(merged)) {
    if (commonKeys.has(key)) {
      result.commonEntries.push([key, schema]);
    } else {
      result.typeEntries.push([key, schema]);
    }
  }
  result.hasWorkflowSelect = result.commonEntries.concat(result.typeEntries)
    .some(([, schema]) => schema && schema.type === 'workflow_select');
  return result;
}

function schemaLabel(key) {
  const map = {
    base_url: 'Base URL', api_key: 'API Key', model: 'Model',
    max_tokens: 'Max Tokens', context_window: 'Context Window (tokens)',
    temperature: 'Temperature', timeout: 'Timeout (s)', max_retries: 'Max Retries',
    supports_tool_calling: 'Tool Calling', workflow: 'Workflow', nsfw: 'NSFW Content',
    extra_body: 'Extra Body', custom_json: 'Custom JSON',
    image_support: 'Support IMG2IMG ?',
  };
  return map[key] || key.replace(/_/g, ' ');
}

function schemaTooltip(key) {
  const map = {
    base_url: 'Base URL of the connector API endpoint.',
    api_key: 'API key used for authentication by this connector.',
    model: 'Default model identifier used by this connector.',
    max_tokens: 'Maximum number of tokens to generate per reply.',
    context_window: 'Total context window of the model in tokens. Controls when conversation summarization kicks in.',
    temperature: 'Sampling temperature: lower is deterministic, higher is more creative.',
    supports_tool_calling: 'Enable OpenAI-style function/tool calling. Only enable for models that support it.',
    timeout: 'Request timeout in seconds before considering the connector unavailable.',
    max_retries: 'How many times a failed request is retried (network errors, timeouts, HTTP 429 and 5xx only). Waits double each time: 1s, 2s, 4s, 8s, 16s, capped at 30s. 0 disables retries.',
    size: 'Image size (example: 1024x1024).',
    workflow: 'ComfyUI workflow template used for image generation.',
    nsfw: 'Allow NSFW behavior for this connector. Disabled by default.',
    extra_body: 'Extra JSON fields merged into the API request body (e.g. provider-specific options).',
    image_support: 'Send the character portrait along with the prompt as an "imageDataUrl" base64 data URL, so the backend can do img2img. Disabled by default.',
    custom_json: 'Raw JSON merged into the request body sent to the backend. Lowest priority: every other parameter wins on conflict.',
  };
  return map[key] || '';
}

function renderField(key, label, type, required, value, placeholder = '', inputType = null) {
  if (type === 'boolean') {
    if (key === 'nsfw') {
      const req = required ? '<span class="required">*</span>' : '';
      const tooltip = schemaTooltip(key);
      const tooltipAttr = tooltip ? ` title="${escHtml(tooltip)}"` : '';
      const checked = !!value ? ' checked' : '';
      return `
        <div class="field-row field-row-nsfw">
          <label for="cfg-${key}"${tooltipAttr}>${escHtml(label)} ${req}</label>
          <label class="nsfw-toggle" for="cfg-${key}">
            <input type="checkbox" id="cfg-${key}" name="${key}" class="nsfw-toggle-input"${checked}>
            <span class="nsfw-toggle-track" aria-hidden="true">
              <span class="nsfw-toggle-option nsfw-toggle-option-off">Filtered</span>
              <span class="nsfw-toggle-option nsfw-toggle-option-on">Allowed</span>
              <span class="nsfw-toggle-knob"></span>
            </span>
          </label>
          <div class="field-help">This choice controls content filtering and safety guardrails for this connector.</div>
        </div>
      `;
    }

    const tooltip = schemaTooltip(key);
    const tooltipAttr = tooltip ? ` title="${escHtml(tooltip)}"` : '';
    const checked = !!value ? ' checked' : '';
    return `
      <div class="field-row field-row-check">
        <label for="cfg-${key}"${tooltipAttr}>
          <input type="checkbox" id="cfg-${key}" name="${key}"${checked}>
          ${escHtml(label)}
        </label>
      </div>
    `;
  }

  const req = required ? '<span class="required">*</span>' : '';
  const tooltip = schemaTooltip(key);
  const tooltipAttr = tooltip ? ` title="${escHtml(tooltip)}"` : '';

  if (type === 'object') {
    const jsonValue = value && typeof value === 'object' ? JSON.stringify(value, null, 2) : (value || '');
    return `
      <div class="field-row">
        <label for="cfg-${key}"${tooltipAttr}>${escHtml(label)} ${req}</label>
        <textarea id="cfg-${key}" name="${key}" data-type="object" rows="4" placeholder='{"key": "value"}'>${escHtml(jsonValue)}</textarea>
      </div>
    `;
  }

  const itype = inputType || (type === 'number' ? 'number' : 'text');
  return `
    <div class="field-row">
      <label for="cfg-${key}"${tooltipAttr}>${escHtml(label)} ${req}</label>
      <input type="${itype}" id="cfg-${key}" name="${key}" value="${escHtml(String(value))}" placeholder="${escHtml(placeholder)}">
    </div>
  `;
}

function renderWorkflowSelect(key, label, currentValue) {
  const tooltip = schemaTooltip(key);
  const tooltipAttr = tooltip ? ` title="${escHtml(tooltip)}"` : '';
  return `
    <div class="field-row">
      <label for="cfg-${key}"${tooltipAttr}>${escHtml(label)}</label>
      <select id="cfg-${key}" name="${key}">
        <option value="${escHtml(currentValue || 'default')}">${escHtml(currentValue || 'default')}</option>
      </select>
    </div>
  `;
}

async function populateWorkflowSelect(selectedValue) {
  const sel = document.getElementById('cfg-workflow');
  if (!sel) return;
  try {
    const workflows = await apiFetch('/api/connectors/comfyui-workflows');
    sel.innerHTML = workflows.map(w =>
      `<option value="${escHtml(w)}"${w === selectedValue ? ' selected' : ''}>${escHtml(w)}</option>`
    ).join('');
    if (!workflows.includes(selectedValue) && selectedValue) {
      const opt = document.createElement('option');
      opt.value = selectedValue;
      opt.textContent = selectedValue;
      opt.selected = true;
      sel.prepend(opt);
    }
  } catch (_) {
    // Keep the placeholder option if the endpoint is unavailable
  }
}

function collectConfig() {
  const cfg = { ...baselineConfig };
  configFields.querySelectorAll('input[name], select[name], textarea[name]').forEach(inp => {
    const key = inp.name;
    if (inp.tagName === 'TEXTAREA' && inp.dataset.type === 'object') {
      const raw = inp.value.trim();
      try { cfg[key] = raw ? JSON.parse(raw) : {}; } catch (_) { cfg[key] = raw; }
      return;
    }
    const val = inp.type === 'checkbox' ? '' : inp.value.trim();
    if (key === 'api_key') {
      if (val !== '' || clearKeyChk.checked) cfg[key] = val;
    } else if (inp.type === 'checkbox') {
      cfg[key] = inp.checked;
    } else {
      cfg[key] = inp.type === 'number' ? (val === '' ? undefined : Number(val)) : val;
    }
  });
  return cfg;
}

function sanitizeConfig(config) {
  const safe = { ...config };
  delete safe.api_key_set;
  return safe;
}

async function handleTest() {
  if (!editingId) {
    dialogFeedback.innerHTML = '<div class="error-banner">Save the connector first, then test it.</div>';
    return;
  }
  testBtn.disabled = true;
  testBtn.textContent = '…';
  dialogFeedback.innerHTML = '';
  try {
    const result = await api.testConnector(editingId);
    const feedback = formatTestFeedback(result);
    testFeedbackById.set(editingId, feedback);
    dialogFeedback.innerHTML = `<div class="${feedback.level === 'ok' ? 'success-banner' : 'error-banner'}">${feedback.message}</div>`;
    await refresh();
  } catch (err) {
    const message = `Test failed: ${escHtml(err.message)}`;
    testFeedbackById.set(editingId, { level: 'fail', message });
    dialogFeedback.innerHTML = `<div class="error-banner">${message}</div>`;
    await refresh();
  } finally {
    testBtn.disabled = false;
    testBtn.textContent = 'Test';
  }
}

function formatTestFeedback(result) {
  const details = result?.details || {};

  if (result?.connected) {
    const models = Array.isArray(details.models_available) ? details.models_available : [];
    let message = '';
    let level = 'ok';
    
    if (models.length > 0) {
      const preview = models.slice(0, 3).map(m => escHtml(String(m))).join(', ');
      const extra = models.length > 3 ? ` (+${models.length - 3} more)` : '';
      message = `Last test: Connected. ${models.length} model(s) available (${preview}${extra}).`;
    } else {
      message = 'Last test: Connected.';
    }
    
    // Add warning if model is not in the available models list
    if (details.model_warning) {
      message += ` ⚠️ ${escHtml(String(details.model_warning))}`;
      level = 'warn';
    }
    
    return { level, message };
  }

  const errorText = details.error || result?.detail || 'Connection failed.';
  return {
    level: 'fail',
    message: `Last test: ${escHtml(String(errorText))}`,
  };
}

async function handleSave() {
  nameError.textContent = '';
  dialogFeedback.innerHTML = '';

  const name = nameInput.value.trim();
  if (!name) { nameError.textContent = 'Name is required.'; nameInput.focus(); return; }

  const body = {
    name,
    type: typeSelect.value,
    backend: backendSelect.value,
    config: collectConfig(),
  };

  saveBtn.disabled = true;
  saveBtn.textContent = 'Saving…';

  try {
    if (editingId) {
      await api.updateConnector(editingId, body);
      showToastFn('Connector saved.', false);
    } else {
      await api.createConnector(body);
      showToastFn('Connector created.', false);
    }
    closeDialog();
    await refresh();
  } catch (err) {
    dialogFeedback.innerHTML = `<div class="error-banner">${escHtml(err.message)}</div>`;
  } finally {
    saveBtn.disabled = false;
    saveBtn.textContent = 'Save';
  }
}

// ── Health badge update ───────────────────────────────────────────────────────

/**
 * Update connector status badges from health data without a full re-render.
 * Called by the 30-second health polling loop in admin/index.html.
 *
 * @param {Object} healthData - response from GET /api/health/
 */
export function applyHealthBadges(healthData) {
  const connectors = healthData?.connectors;
  if (!connectors || typeof connectors !== 'object' || Array.isArray(connectors)) return;
  Object.values(connectors).forEach(c => {
    if (!c || !c.id) return;
    const el = document.getElementById(`conn-status-${c.id}`);
    if (!el) return;
    if (c.connected === true) {
      el.className = 'conn-status ok';
      el.textContent = '✅ Connected';
    } else if (c.connected === false) {
      el.className = 'conn-status fail';
      el.textContent = '❌ Not connected';
    }
  });
}

// ── Util ─────────────────────────────────────────────────────────────────────

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
