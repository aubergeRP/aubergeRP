/**
 * admin/medias.js — Media library management for the Admin UI.
 *
 * Exports initMedias({ showToast, showConfirm }) -> { refresh }
 *
 * Features:
 *  - Table layout with small preview thumbnail
 *  - Server-side pagination with page size selector
 *  - Media type filter (image / video / audio)
 *  - Lightbox modal for full-size preview (image, video, audio)
 */

import { adminFetch } from '/js/admin/auth.js';

async function apiFetch(path, options = {}) {
  // The library exposes generation prompts, so reads are admin-only too.
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
  listMedias: (page, perPage, mediaType) => {
    const params = new URLSearchParams({ page, per_page: perPage });
    if (mediaType) params.set('media_type', mediaType);
    return apiFetch(`/api/media/?${params}`);
  },
  deleteMedia: (id) => apiFetch(`/api/media/${id}`, { method: 'DELETE' }),
};

// DOM refs
const listEl          = document.getElementById('medias-list');
const feedbackEl      = document.getElementById('medias-feedback');
const refreshBtn      = document.getElementById('refresh-medias-btn');
const typeFilterEl    = document.getElementById('medias-type-filter');
const perPageEl       = document.getElementById('medias-per-page');
const paginationTopEl = document.getElementById('medias-pagination-top');
const paginationBotEl = document.getElementById('medias-pagination-bottom');
const lightbox        = document.getElementById('media-lightbox');
const lightboxBody    = document.getElementById('media-lightbox-body');
const lightboxCaption = document.getElementById('media-lightbox-caption');
const lightboxClose   = document.getElementById('media-lightbox-close');

let showToastFn   = () => {};
let showConfirmFn = () => Promise.resolve(false);

// Pagination state
let currentPage    = 1;
let currentPerPage = 50;
let currentType    = '';

export function initMedias({ showToast, showConfirm }) {
  showToastFn   = showToast;
  showConfirmFn = showConfirm;

  refreshBtn?.addEventListener('click', () => { currentPage = 1; refresh(); });

  typeFilterEl?.addEventListener('change', () => {
    currentType = typeFilterEl.value;
    currentPage = 1;
    refresh();
  });

  perPageEl?.addEventListener('change', () => {
    currentPerPage = parseInt(perPageEl.value, 10) || 50;
    currentPage    = 1;
    refresh();
  });

  listEl?.addEventListener('click', handleListClick);

  // Lightbox controls
  lightboxClose?.addEventListener('click', closeLightbox);
  lightbox?.addEventListener('click', (e) => { if (e.target === lightbox) closeLightbox(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && lightbox?.open) closeLightbox(); });

  return { refresh };
}

async function refresh() {
  if (!listEl) return;
  if (feedbackEl) feedbackEl.innerHTML = '';

  listEl.innerHTML = '<div class="loading-row">Loading…</div>';
  renderPagination(null);

  try {
    const data = await api.listMedias(currentPage, currentPerPage, currentType);
    renderTable(data.items || []);
    renderPagination(data);
  } catch (err) {
    listEl.innerHTML = `<div class="error-banner">Cannot load medias: ${escHtml(err.message)}</div>`;
  }
}

// ─── Table rendering ─────────────────────────────────────────────────────────

function renderTable(medias) {
  if (!listEl) return;
  if (!medias.length) {
    listEl.innerHTML = '<div class="loading-row">No generated media yet.</div>';
    return;
  }

  const rows = medias.map(renderRow).join('');
  listEl.innerHTML = `
    <table class="medias-table">
      <thead>
        <tr>
          <th class="col-preview">Preview</th>
          <th class="col-type">Type</th>
          <th class="col-prompt">Prompt sent to connector</th>
          <th class="col-date">Created</th>
          <th class="col-conv">Conversation</th>
          <th class="col-msg">Message</th>
          <th class="col-url">URL</th>
          <th class="col-actions"></th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderRow(media) {
  const mediaType = normalizeMediaType(media.media_type, media.media_url);
  const thumb     = renderThumb(mediaType, media.media_url, media.prompt || media.media_url);
  const badge     = `<span class="media-type-badge media-type-${escHtml(mediaType)}">${escHtml(mediaType)}</span>`;
  const date      = escHtml(formatDateTime(media.created_at));
  const convId    = escHtml(shortId(media.conversation_id));
  const msgId     = escHtml(shortId(media.message_id));
  const urlHtml   = `<a href="${escAttr(media.media_url)}" target="_blank" rel="noopener" title="${escAttr(media.media_url)}">${escHtml(truncateUrl(media.media_url))}</a>`;

  const promptHtml = media.prompt
    ? `<span title="${escAttr(media.prompt)}">${escHtml(truncate(media.prompt, 90))}</span>`
    : '<span class="media-empty">(none recorded)</span>';

  return `
    <tr class="media-row" data-media-id="${escHtml(media.id)}">
      <td class="col-preview">${thumb}</td>
      <td class="col-type">${badge}</td>
      <td class="col-prompt">${promptHtml}</td>
      <td class="col-date">${date}</td>
      <td class="col-conv" title="${escAttr(media.conversation_id)}">${convId}</td>
      <td class="col-msg"  title="${escAttr(media.message_id)}">${msgId}</td>
      <td class="col-url">${urlHtml}</td>
      <td class="col-actions">
        <button class="btn btn-secondary btn-sm"
          data-action="toggle-media-detail"
          data-media-id="${escHtml(media.id)}"
          aria-expanded="false"
          aria-label="Show generation details">Details</button>
        <button class="btn btn-danger btn-sm"
          data-action="delete-media"
          data-media-id="${escHtml(media.id)}"
          aria-label="Delete media">Delete</button>
      </td>
    </tr>
    <tr class="media-detail-row" data-detail-for="${escHtml(media.id)}" hidden>
      <td colspan="8">${renderDetail(media)}</td>
    </tr>`;
}

/**
 * Render the image generation pipeline, step by step, so an admin can tell
 * whether the character prefix and negative prompt were really applied.
 */
function renderDetail(media) {
  const hasTrace = media.raw_prompt || media.llm_input_prompt
    || media.llm_output_prompt || media.prompt_prefix
    || media.negative_prompt || media.connector_name;

  if (!hasTrace && !media.prompt) {
    return '<p class="media-empty">Not recorded (media generated before this was tracked).</p>';
  }

  const steps = [
    ['1. Keywords from the roleplay LLM', media.raw_prompt, false],
    ['2. Prompt sent to the text LLM', media.llm_input_prompt, true],
    ['3. Answer of the text LLM', media.llm_output_prompt, true],
    ['4. Character prefix', media.prompt_prefix, false],
    ['5. Final prompt sent to the connector', media.prompt, true],
    ['6. Character negative prompt', media.negative_prompt, false],
    ['7. Image connector', media.connector_name, false],
  ];

  const body = steps.map(([label, value, block]) => {
    const content = value
      ? (block ? `<pre>${escHtml(value)}</pre>` : escHtml(value))
      : '<span class="media-empty">(none)</span>';
    return `<dt>${escHtml(label)}</dt><dd>${content}</dd>`;
  }).join('');

  const legacy = hasTrace
    ? ''
    : '<p class="media-empty">Only the final prompt was recorded for this media '
      + '(generated before the full trace was tracked).</p>';

  return `${legacy}<dl class="media-detail">${body}</dl>`;
}

function renderThumb(mediaType, mediaUrl, label) {
  const safeUrl  = escAttr(mediaUrl);
  const safeLabel = escAttr(label);

  if (mediaType === 'image') {
    return `<img class="media-thumb-sm" loading="lazy" src="${safeUrl}" alt="Preview"
              data-action="open-lightbox" data-media-type="image"
              data-media-url="${safeUrl}" data-media-label="${safeLabel}"
              title="Click to enlarge">`;
  }
  if (mediaType === 'video') {
    return `<button class="media-preview-btn" data-action="open-lightbox"
              data-media-type="video" data-media-url="${safeUrl}"
              data-media-label="${safeLabel}" aria-label="Play video">▶ Video</button>`;
  }
  if (mediaType === 'audio') {
    return `<button class="media-preview-btn" data-action="open-lightbox"
              data-media-type="audio" data-media-url="${safeUrl}"
              data-media-label="${safeLabel}" aria-label="Play audio">♪ Audio</button>`;
  }
  return `<a class="btn btn-secondary btn-sm" href="${safeUrl}" target="_blank" rel="noopener">Open</a>`;
}

// ─── Pagination ───────────────────────────────────────────────────────────────

function renderPagination(data) {
  const html = data ? buildPaginationHtml(data) : '';
  if (paginationTopEl) paginationTopEl.innerHTML = html;
  if (paginationBotEl) paginationBotEl.innerHTML = html;

  // Attach click handlers to all pagination buttons
  for (const container of [paginationTopEl, paginationBotEl]) {
    container?.querySelectorAll('[data-page]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const pg = parseInt(btn.getAttribute('data-page'), 10);
        if (!Number.isNaN(pg)) { currentPage = pg; refresh(); }
      });
    });
  }
}

function buildPaginationHtml(data) {
  const { total, page, per_page } = data;
  if (total === 0) return '';
  const totalPages = Math.ceil(total / per_page);
  const start = (page - 1) * per_page + 1;
  const end   = Math.min(page * per_page, total);
  const info  = `<span class="pagination-info">${start}–${end} of ${total}</span>`;

  const prevDisabled = page <= 1 ? ' disabled' : '';
  const nextDisabled = page >= totalPages ? ' disabled' : '';
  const prevBtn = `<button class="btn btn-secondary btn-sm" data-page="${page - 1}"${prevDisabled}>&#8592; Prev</button>`;
  const nextBtn = `<button class="btn btn-secondary btn-sm" data-page="${page + 1}"${nextDisabled}>Next &#8594;</button>`;

  // Show up to 7 page number buttons around current page
  const pageNums = buildPageNumbers(page, totalPages);
  const numBtns  = pageNums.map((p) =>
    p === '…'
      ? `<span class="pagination-ellipsis">…</span>`
      : `<button class="btn btn-sm ${p === page ? 'btn-primary' : 'btn-secondary'}" data-page="${p}">${p}</button>`
  ).join('');

  return `<div class="pagination-bar">${prevBtn}${numBtns}${nextBtn}${info}</div>`;
}

function buildPageNumbers(current, total) {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const pages = [];
  pages.push(1);
  if (current > 3) pages.push('…');
  for (let p = Math.max(2, current - 1); p <= Math.min(total - 1, current + 1); p++) pages.push(p);
  if (current < total - 2) pages.push('…');
  pages.push(total);
  return pages;
}

// ─── Lightbox ────────────────────────────────────────────────────────────────

function openLightbox(mediaType, mediaUrl, label) {
  if (!lightbox) return;

  if (mediaType === 'image') {
    lightboxBody.innerHTML = `<img class="lightbox-img" src="${escAttr(mediaUrl)}" alt="${escAttr(label)}">`;
  } else if (mediaType === 'video') {
    lightboxBody.innerHTML = `<video class="lightbox-video" controls autoplay src="${escAttr(mediaUrl)}"></video>`;
  } else if (mediaType === 'audio') {
    lightboxBody.innerHTML = `<audio class="lightbox-audio" controls autoplay src="${escAttr(mediaUrl)}"></audio>`;
  } else {
    lightboxBody.innerHTML = `<a class="btn btn-secondary" href="${escAttr(mediaUrl)}" target="_blank" rel="noopener">Open media</a>`;
  }

  lightboxCaption.textContent = label;
  lightbox.showModal();
}

function closeLightbox() {
  if (!lightbox) return;
  lightboxBody.innerHTML = '';
  lightboxCaption.textContent = '';
  lightbox.close();
}

// ─── Event handling ───────────────────────────────────────────────────────────

async function handleListClick(event) {
  const target = event.target;
  if (!(target instanceof Element)) return;

  const action = target.getAttribute('data-action');

  if (action === 'open-lightbox') {
    const mediaType = target.getAttribute('data-media-type') || 'image';
    const mediaUrl  = target.getAttribute('data-media-url') || '';
    const label     = target.getAttribute('data-media-label') || mediaUrl;
    openLightbox(mediaType, mediaUrl, label);
    return;
  }

  if (action === 'toggle-media-detail') {
    const id  = target.getAttribute('data-media-id');
    const row = listEl?.querySelector(`[data-detail-for="${CSS.escape(id)}"]`);
    if (row) {
      row.hidden = !row.hidden;
      target.setAttribute('aria-expanded', String(!row.hidden));
    }
    return;
  }

  if (action === 'delete-media') {
    const mediaId = target.getAttribute('data-media-id');
    if (!mediaId) return;

    const ok = await showConfirmFn('Delete this media and its associated file from disk?');
    if (!ok) return;

    target.setAttribute('disabled', 'true');
    try {
      await api.deleteMedia(mediaId);
      showToastFn('Media deleted.', false);
      await refresh();
    } catch (err) {
      showToastFn(`Delete failed: ${err.message}`, true);
      target.removeAttribute('disabled');
    }
  }
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function normalizeMediaType(mediaType, mediaUrl) {
  const t = String(mediaType || '').toLowerCase();
  if (t === 'image' || t === 'video' || t === 'audio') return t;
  return inferTypeFromUrl(mediaUrl);
}

function inferTypeFromUrl(url) {
  const lower = String(url || '').toLowerCase();
  if (/\.(png|jpe?g|gif|webp|avif|svg)(\?|$)/.test(lower)) return 'image';
  if (/\.(mp4|webm|mov|mkv|avi)(\?|$)/.test(lower))        return 'video';
  if (/\.(mp3|wav|m4a|flac|ogg|opus)(\?|$)/.test(lower))   return 'audio';
  return 'image';
}

function formatDateTime(value) {
  if (!value) return '-';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString();
}

/** Show only the last two path segments of a URL for brevity. */
function truncateUrl(url) {
  try {
    const parts = String(url).split('/').filter(Boolean);
    return parts.slice(-2).join('/') || url;
  } catch (_) {
    return url;
  }
}

/** Clip a long prompt so the table stays readable; the full text is in the title. */
function truncate(text, max) {
  const str = String(text);
  return str.length > max ? str.slice(0, max) + '…' : str;
}

/** Show last 8 chars of a UUID to keep the table narrow. */
function shortId(id) {
  if (!id) return '-';
  return id.length > 8 ? '…' + id.slice(-8) : id;
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

