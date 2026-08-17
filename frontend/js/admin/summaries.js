import { adminFetch } from '/js/admin/auth.js';

/**
 * admin/summaries.js — Conversation summary debug panel.
 *
 * Shows, per conversation, the current prompt size against the summarization
 * budget, the last stored summary and the messages sent after it.
 *
 * Exports initSummaries({ showToast }) → { refresh }
 */

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

let showToastFn = () => {};
let _openId = null;

export function initSummaries({ showToast }) {
  showToastFn = showToast;
  const btn = document.getElementById('summaries-refresh-btn');
  if (btn) btn.addEventListener('click', () => refresh());
  return { refresh };
}

async function refresh() {
  const feedbackEl = document.getElementById('summaries-feedback');
  const listEl = document.getElementById('summaries-list');
  feedbackEl.innerHTML = '';
  listEl.innerHTML = '<div class="loading-row">Loading…</div>';
  try {
    const rows = await apiFetch('/api/summaries/');
    renderList(rows, listEl);
    if (_openId) await openDetail(_openId);
  } catch (err) {
    listEl.innerHTML = '';
    feedbackEl.innerHTML =
      `<div class="error-banner">Cannot load summaries: ${escHtml(err.message)}</div>`;
  }
}

function renderList(rows, container) {
  if (!rows.length) {
    container.innerHTML = '<div class="loading-row">No conversation yet.</div>';
    return;
  }
  const body = rows.map(r => {
    const pct = r.budget_tokens > 0
      ? Math.min(100, Math.round((r.context_tokens / r.budget_tokens) * 100))
      : 0;
    const over = r.context_tokens > r.budget_tokens;
    return `<tr>
      <td>${escHtml(r.title || r.conversation_id)}<br>
          <small style="color:var(--color-muted,#888)">${escHtml(r.character_name)}</small></td>
      <td>${r.message_count}</td>
      <td>${r.context_tokens} / ${r.budget_tokens}
          <div style="background:var(--color-border,#333);height:6px;border-radius:3px;margin-top:4px">
            <div style="width:${pct}%;height:6px;border-radius:3px;background:${over ? '#c0392b' : '#27ae60'}"></div>
          </div></td>
      <td>${r.summary_count || '—'}</td>
      <td>${r.messages_since_summary}</td>
      <td>${r.last_summary_at ? escHtml(new Date(r.last_summary_at).toLocaleString()) : '—'}</td>
      <td><button class="btn btn-small btn-secondary" data-detail="${escHtml(r.conversation_id)}">Inspect</button></td>
    </tr>`;
  }).join('');

  container.innerHTML = `<div class="stats-table-wrap"><table class="stats-table">
    <thead><tr>
      <th>Conversation</th><th>Messages</th><th>Context / budget</th>
      <th>Summaries</th><th>Since summary</th><th>Last summary</th><th></th>
    </tr></thead><tbody>${body}</tbody></table></div>`;

  container.querySelectorAll('button[data-detail]').forEach(btn => {
    btn.addEventListener('click', () => openDetail(btn.dataset.detail));
  });
}

async function openDetail(conversationId) {
  const el = document.getElementById('summaries-detail');
  _openId = conversationId;
  el.innerHTML = '<div class="loading-row">Loading…</div>';
  let data;
  try {
    data = await apiFetch(`/api/summaries/${encodeURIComponent(conversationId)}`);
  } catch (err) {
    el.innerHTML = `<div class="error-banner">${escHtml(err.message)}</div>`;
    return;
  }

  const msgs = data.messages_since.map(m => `<div style="margin-bottom:0.5rem">
      <strong>${escHtml(m.role)}</strong>
      <small style="color:var(--color-muted,#888)">${escHtml(new Date(m.timestamp).toLocaleString())}</small>
      <div style="white-space:pre-wrap">${escHtml(m.content)}</div>
    </div>`).join('') || '<em>No message after the summary.</em>';

  el.innerHTML = `<div class="stats-panel" style="margin-top:1.5rem">
    <h3 class="health-subheading">Conversation ${escHtml(conversationId)}</h3>
    <p style="color:var(--color-muted,#888);font-size:0.9rem">
      ${data.context_tokens} tokens for a budget of ${data.budget_tokens}
      (context window ${data.context_window} − reply max ${data.max_tokens},
      × threshold ${data.threshold}) —
      ${data.summaries.length} stored summary/summaries.
    </p>
    <div style="margin:0.75rem 0">
      <button class="btn btn-primary" id="summaries-force-btn">Summarize now</button>
      <button class="btn btn-secondary" id="summaries-del-btn">Delete last summary</button>
      <button class="btn btn-secondary" id="summaries-del-all-btn">Delete all summaries</button>
    </div>
    <h3 class="health-subheading">Last summary</h3>
    <div style="white-space:pre-wrap;margin-bottom:1rem">${
      data.summary ? escHtml(data.summary) : '<em>No summary stored yet.</em>'
    }</div>
    <h3 class="health-subheading">Messages since (${data.messages_since.length})</h3>
    ${msgs}
  </div>`;

  document.getElementById('summaries-force-btn')
    .addEventListener('click', () => act(conversationId, 'summarize'));
  document.getElementById('summaries-del-btn')
    .addEventListener('click', () => act(conversationId, 'delete'));
  document.getElementById('summaries-del-all-btn')
    .addEventListener('click', () => act(conversationId, 'delete-all'));
}

async function act(conversationId, action) {
  const id = encodeURIComponent(conversationId);
  try {
    if (action === 'summarize') {
      const res = await apiFetch(`/api/summaries/${id}/summarize`, { method: 'POST' });
      showToastFn(res.created ? 'Summary created' : 'Nothing to summarize');
    } else if (action === 'delete') {
      const res = await apiFetch(`/api/summaries/${id}`, { method: 'DELETE' });
      showToastFn(res.deleted ? 'Last summary deleted' : 'No summary to delete');
    } else {
      const res = await apiFetch(`/api/summaries/${id}?all=true`, { method: 'DELETE' });
      showToastFn(`${res.deleted} summary/summaries deleted`);
    }
  } catch (err) {
    showToastFn(err.message, 'error');
    return;
  }
  await refresh();
}

function escHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
