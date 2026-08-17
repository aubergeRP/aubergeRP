/**
 * admin/characters.js — Character management for the Admin UI.
 *
 * Exports initCharacters({ showToast, showConfirm }) → { refresh }
 */

import { adminFetch } from '/js/admin/auth.js';
import { closeEditorPage, openEditorPage } from '/js/admin/editor-page.js?v=1';

// ── API helpers ──────────────────────────────────────────────────────────────

async function apiFetch(path, options = {}) {
  // Reads also go through adminFetch: with the public character list disabled,
  // an unauthenticated GET /api/characters/ returns an empty list.
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
  listCharacters:   ()          => apiFetch('/api/characters/'),
  getCharacter:     (id)        => apiFetch(`/api/characters/${id}`),
  createCharacter:  (body)      => apiFetch('/api/characters/', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  updateCharacter:  (id, body)  => apiFetch(`/api/characters/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  deleteCharacter:  (id)        => apiFetch(`/api/characters/${id}`, { method: 'DELETE' }),
  duplicateCharacter:(id)       => apiFetch(`/api/characters/${id}/duplicate`, { method: 'POST' }),
  translateCharacter:(id, language) => apiFetch(`/api/characters/${id}/translate`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ language }) }),
  uploadAvatar:     (id, file)  => {
    const fd = new FormData();
    fd.append('file', file);
    return apiFetch(`/api/characters/${id}/avatar`, { method: 'POST', body: fd });
  },
  importCharacter:  (file)      => {
    const fd = new FormData();
    fd.append('file', file);
    return apiFetch('/api/characters/import', { method: 'POST', body: fd });
  },
  exportJson:       (id)        => `/api/characters/${id}/export/json`,
  exportPng:        (id)        => `/api/characters/${id}/export/png`,
  avatarUrl:        (id)        => `/api/characters/${id}/avatar`,
  listScheduleInstancesByCharacter: (id) => apiFetch(`/api/schedules/instances/character/${id}`),
  setScheduleInstanceEnabled: (id, enabled) =>
    apiFetch(`/api/schedules/instances/${id}/enabled`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    }),
  deleteScheduleInstance: (id) => apiFetch(`/api/schedules/instances/${id}`, { method: 'DELETE' }),
};

// ── DOM refs ─────────────────────────────────────────────────────────────────

const listEl        = document.getElementById('character-list');
const importBtn     = document.getElementById('import-char-btn');
const newBtn        = document.getElementById('new-char-btn');

// Import dialog
const importDialog  = document.getElementById('import-dialog');
const importClose   = document.getElementById('import-dialog-close');
const importCancel  = document.getElementById('import-dialog-cancel');
const importDropZone= document.getElementById('import-drop-zone');
const importFileInp = document.getElementById('import-file-input');
const importError   = document.getElementById('import-error');

// Translate dialog
const translateDialog = document.getElementById('translate-dialog');
const translateClose  = document.getElementById('translate-dialog-close');
const translateCancel = document.getElementById('translate-dialog-cancel');
const translateGo     = document.getElementById('translate-dialog-go');
const translateLangInp= document.getElementById('translate-language');
const translateError  = document.getElementById('translate-error');

// Char edit dialog
const charDialog    = document.getElementById('char-dialog');
const charDialogTitle = document.getElementById('char-dialog-title');
const charDialogFeedback = document.getElementById('char-dialog-feedback');
const charClose     = document.getElementById('char-dialog-close');
const charCancel    = document.getElementById('char-dialog-cancel');
const charSave      = document.getElementById('char-dialog-save');
const charAvatarImg = document.getElementById('char-edit-avatar');
const charAvatarUploadBtn = document.getElementById('char-avatar-upload-btn');
const charAvatarInput = document.getElementById('char-avatar-input');

// Form fields
const fName         = document.getElementById('char-name');
const fDesc         = document.getElementById('char-description');
const fPersonality  = document.getElementById('char-personality');
const fFirstMes     = document.getElementById('char-first-mes');
const fMesExample   = document.getElementById('char-mes-example');
const fScenario     = document.getElementById('char-scenario');
const fSystemPrompt = document.getElementById('char-system-prompt');
const fTags         = document.getElementById('char-tags');
const fImgPrompt    = document.getElementById('char-img-prompt-prefix');
const fNegPrompt    = document.getElementById('char-neg-prompt');
const fCreator      = document.getElementById('char-creator');
const fCreatorNotes = document.getElementById('char-creator-notes');
const fNameError    = document.getElementById('char-name-error');
const fDescError    = document.getElementById('char-desc-error');

// Schedules section
const schedulesListEl   = document.getElementById('char-schedules-list');
const scheduleAddBtn    = document.getElementById('char-schedule-add-btn');
const runtimeSchedulesListEl = document.getElementById('char-runtime-schedules-list');

// ── State ────────────────────────────────────────────────────────────────────

let editingId = null;  // null = new, string = existing id
let pendingAvatarFile = null;
/** @type {Array<object>} */
let editingSchedules = [];  // mutable list of schedule definitions
let editingProactive = {
  enabled: true,
  decision_mode: 'contextual',
  minimum_cooldown_minutes: 180,
};
let showToastFn   = () => {};
let showConfirmFn = () => Promise.resolve(false);

// ── Init ─────────────────────────────────────────────────────────────────────

export function initCharacters({ showToast, showConfirm }) {
  showToastFn   = showToast;
  showConfirmFn = showConfirm;

  importBtn.addEventListener('click', openImportDialog);
  newBtn.addEventListener('click', () => openEditDialog(null));

  // Import dialog
  importClose.addEventListener('click', closeImportDialog);
  importCancel.addEventListener('click', closeImportDialog);

  importDropZone.addEventListener('click', () => importFileInp.click());
  importDropZone.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') importFileInp.click(); });
  importDropZone.addEventListener('dragover', e => { e.preventDefault(); importDropZone.classList.add('drag-over'); });
  importDropZone.addEventListener('dragleave', () => importDropZone.classList.remove('drag-over'));
  importDropZone.addEventListener('drop', e => {
    e.preventDefault();
    importDropZone.classList.remove('drag-over');
    const file = e.dataTransfer?.files?.[0];
    if (file) handleImportFile(file);
  });
  importFileInp.addEventListener('change', () => {
    const file = importFileInp.files?.[0];
    if (file) handleImportFile(file);
  });

  // Translate dialog
  translateClose.addEventListener('click', closeTranslateDialog);
  translateCancel.addEventListener('click', closeTranslateDialog);
  translateGo.addEventListener('click', runTranslation);
  translateLangInp.addEventListener('keydown', e => { if (e.key === 'Enter') runTranslation(); });

  // Edit dialog
  charClose.addEventListener('click', closeEditDialog);
  charCancel.addEventListener('click', closeEditDialog);
  charSave.addEventListener('click', handleSave);

  // Schedules
  scheduleAddBtn.addEventListener('click', () => {
    editingSchedules.push({
      id: 'new_schedule_' + Date.now(),
      enabled: true,
      type: 'daily_at',
      time: '09:00',
      start: null,
      end: null,
      delay_minutes: null,
      inactivity_minutes: null,
      not_before_time: null,
      minimum_cooldown_minutes: null,
      one_shot: false,
      instruction: '',
    });
    renderSchedules();
  });

  // Avatar upload
  charAvatarUploadBtn.addEventListener('click', () => charAvatarInput.click());
  charAvatarInput.addEventListener('change', () => {
    const file = charAvatarInput.files?.[0];
    if (!file) return;
    if (!file.type.startsWith('image/')) { showToastFn('Please select an image file.', true); return; }
    if (file.size > 10 * 1024 * 1024) { showToastFn('Image must be ≤ 10 MB.', true); return; }
    pendingAvatarFile = file;
    const reader = new FileReader();
    reader.onload = e => { charAvatarImg.src = e.target.result; };
    reader.readAsDataURL(file);
  });

  refresh();
  return { refresh };
}

// ── Render character list ─────────────────────────────────────────────────────

async function refresh() {
  listEl.innerHTML = '<div class="loading-row">Loading…</div>';
  try {
    const chars = await api.listCharacters();
    renderList(chars);
  } catch (err) {
    listEl.innerHTML = `<div class="error-banner">Cannot load characters: ${err.message}</div>`;
  }
}

function renderList(chars) {
  if (!chars.length) {
    listEl.innerHTML = '<div class="loading-row">No characters yet. Import or create one.</div>';
    return;
  }
  listEl.innerHTML = chars.map(c => renderCharCard(c)).join('');
  listEl.querySelectorAll('[data-action]').forEach(el => {
    el.addEventListener('click', handleCardAction);
  });
  listEl.querySelectorAll('.dropdown-toggle').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const menu = btn.nextElementSibling;
      const isOpen = menu.style.display === 'block';
      closeAllDropdowns();
      menu.style.display = isOpen ? 'none' : 'block';
    });
  });
}

function closeAllDropdowns() {
  listEl.querySelectorAll('.dropdown-menu').forEach(m => { m.style.display = 'none'; });
}

function renderCharCard(c) {
  const avatarSrc = c.has_avatar ? api.avatarUrl(c.id) : '';
  const avatarEl  = avatarSrc
    ? `<img class="char-card-avatar" src="${escHtml(avatarSrc)}" alt="${escHtml(c.name)} avatar" loading="lazy">`
    : `<div class="char-card-avatar" aria-hidden="true" style="display:flex;align-items:center;justify-content:center;font-size:1.6rem;">🧝</div>`;

  const tags = Array.isArray(c.tags) && c.tags.length ? c.tags.join(', ') : '';

  return `
    <div class="char-card" data-id="${c.id}">
      ${avatarEl}
      <div class="char-card-info">
        <div class="char-card-name">${escHtml(c.name)}</div>
        ${c.description ? `<div class="char-card-desc">${escHtml(c.description)}</div>` : ''}
        ${tags ? `<div class="char-card-tags">Tags: ${escHtml(tags)}</div>` : ''}
      </div>
      <div class="char-card-actions">
        <button class="btn btn-secondary btn-sm" data-action="edit" data-id="${c.id}">Edit</button>
        <div class="dropdown-wrap">
          <button class="btn-icon dropdown-toggle" title="More actions" aria-label="More actions for ${escHtml(c.name)}">⋮</button>
          <div class="dropdown-menu" style="display:none">
            <button data-action="copy-link" data-id="${c.id}">Copy chat link</button>
            <button data-action="duplicate" data-id="${c.id}">Duplicate</button>
            <button data-action="translate" data-id="${c.id}">Translate…</button>
            <button data-action="export-json" data-id="${c.id}">Export as JSON</button>
            <button data-action="export-png" data-id="${c.id}">Export as PNG</button>
            <button data-action="delete" data-id="${c.id}" class="danger">Delete</button>
          </div>
        </div>
      </div>
    </div>
  `;
}

async function handleCardAction(e) {
  const btn    = e.currentTarget;
  const action = btn.dataset.action;
  const id     = btn.dataset.id;
  closeAllDropdowns();

  if (action === 'edit') {
    await openEditDialog(id);
  } else if (action === 'copy-link') {
    // Direct link to this chat — the only way in when public listing is off.
    const url = `${window.location.origin}/?character=${id}`;
    try {
      await navigator.clipboard.writeText(url);
      showToastFn('Chat link copied to clipboard.', false);
    } catch (_) {
      showToastFn(url, false);
    }
  } else if (action === 'duplicate') {
    try {
      await api.duplicateCharacter(id);
      showToastFn('Character duplicated.', false);
      await refresh();
    } catch (err) {
      showToastFn(`Duplicate failed: ${err.message}`, true);
    }
  } else if (action === 'translate') {
    openTranslateDialog(id);
  } else if (action === 'export-json') {
    downloadUrl(api.exportJson(id));
  } else if (action === 'export-png') {
    downloadUrl(api.exportPng(id));
  } else if (action === 'delete') {
    const card = listEl.querySelector(`.char-card[data-id="${id}"]`);
    const name = card?.querySelector('.char-card-name')?.textContent?.trim() || 'this character';
    const ok = await showConfirmFn(`Are you sure you want to delete ${name}? This cannot be undone.`);
    if (!ok) return;
    try {
      await api.deleteCharacter(id);
      showToastFn('Character deleted.', false);
      await refresh();
    } catch (err) {
      showToastFn(`Delete failed: ${err.message}`, true);
    }
  }
}

function downloadUrl(url) {
  const a = document.createElement('a');
  a.href = url;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ── Import dialog ─────────────────────────────────────────────────────────────

function openImportDialog() {
  importError.textContent = '';
  importFileInp.value = '';
  openEditorPage(importDialog);
}

function closeImportDialog() {
  closeEditorPage(importDialog);
}

async function handleImportFile(file) {
  importError.textContent = '';
  importFileInp.value = '';
  try {
    await api.importCharacter(file);
    showToastFn('Character imported successfully.', false);
    closeImportDialog();
    await refresh();
  } catch (err) {
    importError.textContent = err.message;
  }
}

// ── Translate dialog ──────────────────────────────────────────────────────────

let translatingId = null;

function openTranslateDialog(id) {
  translatingId = id;
  translateError.textContent = '';
  translateLangInp.value = '';
  translateGo.disabled = false;
  translateGo.textContent = 'Translate';
  openEditorPage(translateDialog);
  translateLangInp.focus();
}

function closeTranslateDialog() {
  if (translateGo.disabled) return;  // a translation is running
  closeEditorPage(translateDialog);
  translatingId = null;
}

async function runTranslation() {
  const language = translateLangInp.value.trim();
  if (!language) {
    translateError.textContent = 'Please enter a target language.';
    return;
  }
  translateError.textContent = '';
  translateGo.disabled = true;
  translateGo.textContent = 'Translating…';
  try {
    const card = await api.translateCharacter(translatingId, language);
    translateGo.disabled = false;
    closeTranslateDialog();
    showToastFn(`Translated copy created: ${card.data?.name || language}.`, false);
    await refresh();
  } catch (err) {
    translateGo.disabled = false;
    translateGo.textContent = 'Translate';
    translateError.textContent = err.message;
  }
}

// ── Edit dialog ───────────────────────────────────────────────────────────────

async function openEditDialog(id) {
  editingId = id;
  pendingAvatarFile = null;
  fNameError.textContent = '';
  fDescError.textContent = '';
  charDialogFeedback.innerHTML = '';
  charAvatarInput.value = '';

  if (id) {
    charDialogTitle.textContent = 'Edit Character';
    try {
      const char = await api.getCharacter(id);
      const d = char.data || char;
      fName.value         = d.name || '';
      fDesc.value         = d.description || '';
      fPersonality.value  = d.personality || '';
      fFirstMes.value     = d.first_mes || '';
      fMesExample.value   = d.mes_example || '';
      fScenario.value     = d.scenario || '';
      fSystemPrompt.value = d.system_prompt || '';
      fTags.value         = Array.isArray(d.tags) ? d.tags.join(', ') : (d.tags || '');
      fImgPrompt.value    = d.extensions?.aubergerp?.image_prompt_prefix || '';
      fNegPrompt.value    = d.extensions?.aubergerp?.negative_prompt || '';
      fCreator.value      = d.creator || '';
      fCreatorNotes.value = d.creator_notes || '';
      editingSchedules    = JSON.parse(JSON.stringify(d.extensions?.aubergerp?.schedules || []));
      editingProactive    = JSON.parse(JSON.stringify(d.extensions?.aubergerp?.proactive || editingProactive));
      renderSchedules();
      await renderRuntimeSchedules(id);

      if (char.has_avatar || char.avatar_url) {
        charAvatarImg.src = api.avatarUrl(id);
        charAvatarImg.style.display = '';
      } else {
        charAvatarImg.src = '';
        charAvatarImg.style.display = 'none';
      }
    } catch (err) {
      showToastFn(`Failed to load character: ${err.message}`, true);
      return;
    }
  } else {
    charDialogTitle.textContent = 'New Character';
    [fName, fDesc, fPersonality, fFirstMes, fMesExample, fScenario,
     fSystemPrompt, fTags, fImgPrompt, fNegPrompt, fCreator, fCreatorNotes].forEach(el => { el.value = ''; });
    charAvatarImg.src = '';
    charAvatarImg.style.display = 'none';
    editingSchedules = [];
    editingProactive = {
      enabled: true,
      decision_mode: 'contextual',
      minimum_cooldown_minutes: 180,
    };
    renderSchedules();
    renderRuntimeSchedules(null);
  }

  openEditorPage(charDialog);
  fName.focus();
}

function closeEditDialog() {
  closeEditorPage(charDialog);
  editingId = null;
  pendingAvatarFile = null;
  editingSchedules = [];
}

async function handleSave() {
  fNameError.textContent = '';
  fDescError.textContent = '';
  charDialogFeedback.innerHTML = '';

  const name = fName.value.trim();
  const desc = fDesc.value.trim();
  if (!name) { fNameError.textContent = 'Name is required.'; fName.focus(); return; }
  if (!desc) { fDescError.textContent = 'Description is required.'; fDesc.focus(); return; }

  const tags = fTags.value.split(',').map(t => t.trim()).filter(Boolean);

  const body = {
    name,
    description: desc,
    personality:  fPersonality.value.trim(),
    first_mes:    fFirstMes.value.trim(),
    mes_example:  fMesExample.value.trim(),
    scenario:     fScenario.value.trim(),
    system_prompt: fSystemPrompt.value.trim(),
    tags,
    creator:      fCreator.value.trim(),
    creator_notes: fCreatorNotes.value.trim(),
    extensions: {
      aubergerp: {
        image_prompt_prefix: fImgPrompt.value.trim(),
        negative_prompt:     fNegPrompt.value.trim(),
        schedules: collectSchedules(),
        proactive: editingProactive,
      },
    }
  };

  charSave.disabled = true;
  charSave.textContent = 'Saving…';

  try {
    let savedChar;
    if (editingId) {
      savedChar = await api.updateCharacter(editingId, body);
    } else {
      savedChar = await api.createCharacter(body);
    }

    // Upload avatar if one was selected
    if (pendingAvatarFile && savedChar?.id) {
      try {
        await api.uploadAvatar(savedChar.id, pendingAvatarFile);
      } catch (avatarErr) {
        showToastFn(`Character saved but avatar upload failed: ${avatarErr.message}`, true);
        closeEditDialog();
        await refresh();
        return;
      }
    }

    showToastFn(editingId ? 'Character saved.' : 'Character created.', false);
    closeEditDialog();
    await refresh();
  } catch (err) {
    charDialogFeedback.innerHTML = `<div class="error-banner">${escHtml(err.message)}</div>`;
  } finally {
    charSave.disabled = false;
    charSave.textContent = 'Save';
  }
}

// ── Util ─────────────────────────────────────────────────────────────────────

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Schedule helpers ──────────────────────────────────────────────────────────

function renderSchedules() {
  if (!schedulesListEl) return;
  schedulesListEl.innerHTML = '';
  editingSchedules.forEach((sched, idx) => {
    const row = document.createElement('div');
    row.className = 'schedule-row';
    row.style.cssText = 'border:1px solid var(--color-border,#444);border-radius:6px;padding:0.75rem;margin-bottom:0.5rem;';

    const isWindow = sched.type === 'daily_window';
    const isAfterDelay = sched.type === 'after_delay';
    const isAfterInactivity = sched.type === 'after_inactivity';
    row.innerHTML = `
      <div style="display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap;margin-bottom:0.5rem">
        <input type="text" data-field="id" value="${escHtml(sched.id || '')}" placeholder="Schedule ID" style="flex:1;min-width:8rem" title="Unique schedule ID">
        <select data-field="type" style="flex:0 0 auto">
          <option value="daily_at"${sched.type === 'daily_at' ? ' selected' : ''}>daily_at</option>
          <option value="daily_window"${isWindow ? ' selected' : ''}>daily_window</option>
          <option value="after_delay"${isAfterDelay ? ' selected' : ''}>after_delay</option>
          <option value="after_inactivity"${isAfterInactivity ? ' selected' : ''}>after_inactivity</option>
        </select>
        <label style="display:flex;align-items:center;gap:0.25rem;cursor:pointer">
          <input type="checkbox" data-field="enabled"${sched.enabled ? ' checked' : ''}> Enabled
        </label>
        <button type="button" data-action="delete-sched" data-idx="${idx}" style="margin-left:auto" title="Delete schedule">✕</button>
      </div>
      <div class="sched-time-fields" style="display:flex;gap:0.5rem;flex-wrap:wrap;margin-bottom:0.5rem">
        <div class="sched-daily-at" style="display:${isWindow ? 'none' : 'flex'};gap:0.5rem;align-items:center">
          <label>Time (HH:MM)</label>
          <input type="text" data-field="time" value="${escHtml(sched.time || '09:00')}" placeholder="09:00" style="width:6rem">
        </div>
        <div class="sched-daily-window" style="display:${isWindow ? 'flex' : 'none'};gap:0.5rem;align-items:center">
          <label>Window start</label>
          <input type="text" data-field="start" value="${escHtml(sched.start || '09:00')}" placeholder="09:00" style="width:6rem">
          <label>end</label>
          <input type="text" data-field="end" value="${escHtml(sched.end || '11:00')}" placeholder="11:00" style="width:6rem">
        </div>
        <div class="sched-after-delay" style="display:${isAfterDelay ? 'flex' : 'none'};gap:0.5rem;align-items:center">
          <label>Delay (min)</label>
          <input type="number" data-field="delay_minutes" value="${escHtml(String(sched.delay_minutes ?? 180))}" style="width:6rem">
        </div>
        <div class="sched-after-inactivity" style="display:${isAfterInactivity ? 'flex' : 'none'};gap:0.5rem;align-items:center">
          <label>Inactivity (min)</label>
          <input type="number" data-field="inactivity_minutes" value="${escHtml(String(sched.inactivity_minutes ?? 1440))}" style="width:7rem">
        </div>
      </div>
      <div class="sched-time-fields" style="display:flex;gap:0.5rem;flex-wrap:wrap;margin-bottom:0.5rem">
        <label>Not before</label>
        <input type="text" data-field="not_before_time" value="${escHtml(sched.not_before_time || '')}" placeholder="09:00" style="width:6rem">
        <label>Cooldown (min)</label>
        <input type="number" data-field="minimum_cooldown_minutes" value="${escHtml(String(sched.minimum_cooldown_minutes ?? ''))}" style="width:7rem">
        <label style="display:flex;align-items:center;gap:0.25rem;">
          <input type="checkbox" data-field="one_shot"${sched.one_shot ? ' checked' : ''}> One-shot
        </label>
      </div>
      <div>
        <label style="display:block;font-size:0.82rem;margin-bottom:0.25rem">Instruction</label>
        <textarea data-field="instruction" rows="2" placeholder="Ask {{user}} how they slept…" style="width:100%;box-sizing:border-box">${escHtml(sched.instruction || '')}</textarea>
      </div>
    `;

    // Type toggle
    row.querySelector('[data-field="type"]').addEventListener('change', e => {
      const val = e.target.value;
      editingSchedules[idx].type = val;
      row.querySelector('.sched-daily-at').style.display = val === 'daily_at' ? 'flex' : 'none';
      row.querySelector('.sched-daily-window').style.display = val === 'daily_window' ? 'flex' : 'none';
      row.querySelector('.sched-after-delay').style.display = val === 'after_delay' ? 'flex' : 'none';
      row.querySelector('.sched-after-inactivity').style.display = val === 'after_inactivity' ? 'flex' : 'none';
    });

    // Live sync for text/checkbox fields
    row.querySelectorAll('[data-field]').forEach(el => {
      const field = el.dataset.field;
      if (el.type === 'checkbox') {
        el.addEventListener('change', () => { editingSchedules[idx][field] = el.checked; });
      } else if (el.tagName === 'SELECT') {
        // handled above
      } else {
        el.addEventListener('input', () => { editingSchedules[idx][field] = el.value; });
      }
    });

    // Delete button
    row.querySelector('[data-action="delete-sched"]').addEventListener('click', () => {
      editingSchedules.splice(idx, 1);
      renderSchedules();
    });

    schedulesListEl.appendChild(row);
  });
}

function collectSchedules() {
  return editingSchedules.map(s => {
    const out = {
      id: s.id || '',
      enabled: !!s.enabled,
      type: s.type || 'daily_at',
      instruction: s.instruction || '',
    };
    if (out.type === 'daily_at') {
      out.time = s.time || '09:00';
    } else if (out.type === 'daily_window') {
      out.start = s.start || '09:00';
      out.end   = s.end   || '11:00';
    } else if (out.type === 'after_delay') {
      out.delay_minutes = Number(s.delay_minutes || 180);
    } else if (out.type === 'after_inactivity') {
      out.inactivity_minutes = Number(s.inactivity_minutes || 1440);
    }
    if (s.not_before_time) out.not_before_time = s.not_before_time;
    if (s.minimum_cooldown_minutes !== null && s.minimum_cooldown_minutes !== undefined && s.minimum_cooldown_minutes !== '') {
      out.minimum_cooldown_minutes = Number(s.minimum_cooldown_minutes);
    }
    if (s.one_shot) out.one_shot = true;
    return out;
  }).filter(s => s.id && s.instruction);
}

async function renderRuntimeSchedules(characterId) {
  if (!runtimeSchedulesListEl) return;
  if (!characterId) {
    runtimeSchedulesListEl.innerHTML = '<div class="loading-row">No runtime schedule instances yet.</div>';
    return;
  }
  try {
    const rows = await api.listScheduleInstancesByCharacter(characterId);
    if (!Array.isArray(rows) || rows.length === 0) {
      runtimeSchedulesListEl.innerHTML = '<div class="loading-row">No runtime schedule instances yet.</div>';
      return;
    }
    runtimeSchedulesListEl.innerHTML = rows.map(r => `
      <div class="schedule-row" style="border:1px solid var(--color-border,#444);border-radius:6px;padding:0.5rem;margin-bottom:0.5rem;">
        <div><strong>${escHtml(r.schedule_def_id)}</strong> <code>${escHtml(r.trigger_type || '')}</code></div>
        <div style="font-size:0.85rem;opacity:0.85;">origin: ${escHtml(r.origin || '')} · next: ${escHtml(String(r.next_run_at || '—'))}</div>
        <div style="font-size:0.85rem;opacity:0.85;">last: ${escHtml(String(r.last_execution_at || '—'))} · status: ${escHtml(r.last_execution_status || '—')}</div>
        <div style="display:flex;gap:0.5rem;margin-top:0.35rem;">
          <button class="btn btn-secondary btn-sm" data-action="toggle-runtime" data-id="${escHtml(r.id)}" data-enabled="${r.enabled ? '1' : '0'}">${r.enabled ? 'Disable' : 'Enable'}</button>
          <button class="btn btn-secondary btn-sm" data-action="delete-runtime" data-id="${escHtml(r.id)}">Cancel</button>
        </div>
      </div>
    `).join('');
    runtimeSchedulesListEl.querySelectorAll('[data-action="toggle-runtime"]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const id = btn.dataset.id;
        const enabled = btn.dataset.enabled === '1';
        await api.setScheduleInstanceEnabled(id, !enabled);
        await renderRuntimeSchedules(characterId);
      });
    });
    runtimeSchedulesListEl.querySelectorAll('[data-action="delete-runtime"]').forEach(btn => {
      btn.addEventListener('click', async () => {
        await api.deleteScheduleInstance(btn.dataset.id);
        await renderRuntimeSchedules(characterId);
      });
    });
  } catch (err) {
    runtimeSchedulesListEl.innerHTML = `<div class="error-banner">${escHtml(err.message)}</div>`;
  }
}
