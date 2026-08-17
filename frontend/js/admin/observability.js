/**
 * admin/observability.js — Operations dashboard for the Admin UI.
 *
 * Aggregates the /api/observability/* endpoints into one operational view:
 * system health, Telegram runtime, sessions, LLM activity, context pressure,
 * proactive schedules and recent errors.
 *
 * Exports initObservability({ showToast }) → { refresh, setVisible }
 */

import { adminFetch } from '/js/admin/auth.js';

// ── API helpers ──────────────────────────────────────────────────────────────

async function apiFetch(path) {
  const res = await adminFetch(path);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const body = await res.json(); if (body.detail) detail = body.detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

function qs(params) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== '' && v !== null && v !== undefined) search.set(k, v);
  });
  const str = search.toString();
  return str ? `?${str}` : '';
}

//: The dashboard reports a single, fixed window — an operational tail, not a
//: reporting tool.  Longer ranges belong to Statistics.
const RANGE_HOURS = 24;
//: Recent-generation rows kept on screen; matches the memory-only body buffer.
const RECENT_LIMIT = 50;

const api = {
  overview:  (p) => apiFetch(`/api/observability/overview${qs(p)}`),
  payload:   (id) => apiFetch(`/api/observability/llm/${encodeURIComponent(id)}/payload`),
  telegram:  ()  => apiFetch('/api/observability/telegram'),
  webhook:   (id) => apiFetch(`/api/observability/telegram/${id}/webhook`),
  sessions:  (p) => apiFetch(`/api/observability/sessions${qs(p)}`),
  llm:       (p) => apiFetch(`/api/observability/llm${qs(p)}`),
  memory:    (p) => apiFetch(`/api/observability/memory${qs(p)}`),
  schedules: (p) => apiFetch(`/api/observability/schedules${qs(p)}`),
  errors:    (p) => apiFetch(`/api/observability/errors${qs(p)}`),
};

// ── DOM refs ─────────────────────────────────────────────────────────────────

const autoRefreshEl  = document.getElementById('obs-autorefresh');
const refreshBtn     = document.getElementById('obs-refresh-btn');
const feedbackEl     = document.getElementById('obs-feedback');
const summaryEl      = document.getElementById('obs-summary');
const telegramWrap   = document.getElementById('obs-telegram-wrap');
const sessionsWrap   = document.getElementById('obs-sessions-wrap');
const llmWrap        = document.getElementById('obs-llm-wrap');
const memoryWrap     = document.getElementById('obs-memory-wrap');
const schedulesWrap  = document.getElementById('obs-schedules-wrap');
const errorsWrap     = document.getElementById('obs-errors-wrap');

const sessionTransportEl = document.getElementById('obs-session-transport');
const sessionBotEl       = document.getElementById('obs-session-bot');
const llmTypeEl          = document.getElementById('obs-llm-type');
const llmStatusEl        = document.getElementById('obs-llm-status');

// ── Formatting ───────────────────────────────────────────────────────────────

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function fmtDuration(seconds) {
  if (!seconds && seconds !== 0) return '—';
  const s = Math.floor(seconds);
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const mins = Math.floor((s % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${mins}m`;
  if (mins) return `${mins}m`;
  return `${s}s`;
}

function fmtAgo(iso) {
  if (!iso) return '—';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '—';
  const delta = Math.max(0, (Date.now() - then) / 1000);
  if (delta < 60) return 'just now';
  return `${fmtDuration(delta)} ago`;
}

function fmtBytes(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'kB', 'MB', 'GB'];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtNumber(value) {
  return Number(value ?? 0).toLocaleString();
}

function badge(text, kind) {
  return `<span class="badge badge-${kind}">${esc(text)}</span>`;
}

function stateBadge(state) {
  if (state === 'running') return badge('Running', 'running');
  if (state === 'error') return badge('Not running', 'error');
  return badge('Stopped', 'disabled');
}

function outcomeBadge(status) {
  if (status === 'sent') return badge('Sent', 'running');
  if (status === 'skipped') return badge('Skipped', 'warn');
  if (status === 'failed') return badge('Failed', 'error');
  return '<span class="obs-muted">never run</span>';
}

function card(label, value, hint) {
  return `
    <div class="stats-card">
      <div class="stats-card-label">${esc(label)}</div>
      <div class="stats-card-value">${esc(value)}</div>
      ${hint ? `<div class="obs-card-hint">${esc(hint)}</div>` : ''}
    </div>`;
}

// Wrap a cell in num() to right-align it — header included — everywhere it
// appears.  Every figure on this page goes through it.
function num(html) {
  return { num: true, html: String(html) };
}

function table(headers, rows, emptyText) {
  if (!rows.length) return `<div class="obs-empty">${esc(emptyText)}</div>`;
  const numeric = new Set();
  rows.forEach((cells) => cells.forEach((c, i) => { if (c && c.num) numeric.add(i); }));
  const cls = (i) => (numeric.has(i) ? ' class="num"' : '');
  const head = headers.map((h, i) => `<th${cls(i)}>${esc(h)}</th>`).join('');
  const body = rows.map((cells) => `<tr>${cells.map((c, i) => {
    const html = (c && c.num) ? c.html : String(c ?? '');
    return `<td${cls(i)}>${html}</td>`;
  }).join('')}</tr>`).join('');
  return `<div class="stats-table-wrap"><table class="stats-table">
    <thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

// Drill-down link: jumps to a section and pre-filters it.
function link(text, sectionAnchor) {
  return `<a href="#" class="obs-link" data-jump="${esc(sectionAnchor)}">${esc(text)}</a>`;
}

// ── Renderers ────────────────────────────────────────────────────────────────

function renderSummary(overview) {
  const sys = overview.system;
  const llm = overview.llm;
  const tg = overview.telegram;
  const pro = overview.proactive;
  const mem = overview.memory;
  const errorCount = overview.errors.recent;

  summaryEl.innerHTML = [
    card('Uptime', fmtDuration(sys.uptime_seconds), `v${sys.version} · db ${
      sys.database_ok ? fmtBytes(sys.database_size_bytes) : 'unavailable'}`),
    card('Sessions', fmtNumber(sys.sessions), `${fmtNumber(sys.active_conversations)} active conversations`),
    card('Telegram bots', `${tg.running}/${tg.configured}`, 'running'),
    card('LLM generations', fmtNumber(llm.generations), `${llm.failure_rate}% failed`),
    card('Avg latency', `${fmtNumber(Math.round(llm.avg_latency_ms))} ms`, 'per generation'),
    card('Schedules', `${pro.enabled} enabled`, pro.next_run_at ? `next ${fmtAgo(pro.next_run_at)}` : 'none due'),
    card('Summaries', fmtNumber(mem.summaries_generated), `${mem.summarization_failures} failed`),
    card('Recent errors', fmtNumber(errorCount), 'since process start'),
  ].join('');
}

function renderTelegram(bots) {
  // Keep the session filter in sync with the configured bots.
  const current = sessionBotEl.value;
  sessionBotEl.innerHTML = '<option value="">All bots</option>' +
    bots.map((b) => `<option value="${esc(b.id)}">${esc(b.name)}</option>`).join('');
  sessionBotEl.value = current;

  const rows = bots.map((bot) => [
    `<strong>${esc(bot.name)}</strong>${bot.username ? `<div class="obs-muted">@${esc(bot.username)}</div>` : ''}`,
    esc(bot.character_name || bot.character_id || '—'),
    bot.enabled ? stateBadge(bot.runtime_state) : badge('Disabled', 'disabled'),
    esc(bot.update_mode) + (
      bot.update_mode === 'webhook'
        ? ` · <a href="#" class="obs-link" data-webhook="${esc(bot.id)}">detail</a>`
        : ''
    ),
    fmtAgo(bot.last_update_at),
    fmtAgo(bot.last_message_sent_at),
    num(bot.delivery_failures > 0
      ? `<span class="obs-error-text">${fmtNumber(bot.delivery_failures)}</span>`
      : '0'),
    num(link(fmtNumber(bot.sessions), `sessions:bot=${bot.id}`)),
    [bot.last_runtime_error, bot.last_error, bot.webhook_last_error]
      .filter(Boolean).map(esc).join('<br>') || '—',
  ]);

  telegramWrap.innerHTML = table(
    ['Bot', 'Character', 'State', 'Mode', 'Last update', 'Last sent', 'Failures', 'Sessions', 'Last error'],
    rows,
    'No Telegram bot is configured.',
  ) + '<div id="obs-webhook-detail"></div>';
}

function renderWebhook(info) {
  const target = document.getElementById('obs-webhook-detail');
  if (!target) return;
  if (!info.available) {
    target.innerHTML = `<div class="obs-empty">Webhook info unavailable: ${esc(info.detail || 'unknown')}</div>`;
    return;
  }
  target.innerHTML = `<div class="obs-webhook">
    <div><span class="obs-muted">URL</span> ${esc(info.url || '—')}</div>
    <div><span class="obs-muted">Pending updates</span> ${fmtNumber(info.pending_update_count)}</div>
    <div><span class="obs-muted">Last Telegram error</span> ${esc(info.last_error_message || 'none')}</div>
    <div><span class="obs-muted">IP</span> ${esc(info.ip_address || '—')}</div>
  </div>`;
}

function renderSessions(sessions) {
  const rows = sessions.map((s) => [
    esc(s.transport),
    esc(s.channel_name || '—'),
    esc(s.character_name || '—'),
    esc(s.user_ref || '—'),
    num(fmtNumber(s.message_count)),
    fmtAgo(s.last_user_activity),
    fmtAgo(s.last_assistant_activity),
    `${link('LLM', `llm:conversation=${s.conversation_id}`)} · ${link('context', `memory:conversation=${s.conversation_id}`)}`,
  ]);
  sessionsWrap.innerHTML = table(
    ['Transport', 'Bot/Channel', 'Character', 'User', 'Messages', 'Last user', 'Last assistant', ''],
    rows,
    'No session recorded yet.',
  );
}

function renderLLM(payload) {
  const byType = payload.summary.by_type || {};
  const typeRows = Object.entries(byType).map(([type, stats]) => [
    esc(type),
    num(fmtNumber(stats.generations)),
    num(fmtNumber(stats.failed)),
    num(`${fmtNumber(Math.round(stats.avg_latency_ms))} ms`),
    num(fmtNumber(stats.tokens_in)),
    num(fmtNumber(stats.tokens_out)),
  ]);

  const recentRows = payload.recent.map((r) => [
    fmtAgo(r.timestamp),
    esc(r.generation_type),
    esc(r.conversation_title || r.conversation_id || '—'),
    esc(r.model || r.connector_name || '—'),
    num(`${fmtNumber(r.duration_ms)} ms`),
    r.error_detail
      ? `<span class="obs-error-text" title="${esc(r.error_detail)}">Failed</span>`
      : (r.success ? badge('OK', 'running') : badge('Failed', 'error')),
    num(fmtNumber(r.tokens_in)),
    num(fmtNumber(r.tokens_out)),
    r.has_payload
      ? `<a href="#" class="obs-link" data-payload="${esc(r.id)}">input/output</a>`
      : '<span class="obs-muted">—</span>',
  ]);

  llmWrap.innerHTML = `
    ${table(['Type', 'Generations', 'Failed', 'Avg latency', 'Tokens in', 'Tokens out'],
            typeRows, 'No LLM activity in the last 24 h.')}
    <h4 class="obs-subhead">Recent generations <span class="obs-muted">— last ${RECENT_LIMIT}</span></h4>
    ${table(['When', 'Type', 'Conversation', 'Model', 'Duration', 'Result', 'Tokens in', 'Tokens out', ''],
            recentRows, 'No generation in the last 24 h.')}
    <div id="obs-payload-detail"></div>`;
}

function renderPayload(info) {
  const target = document.getElementById('obs-payload-detail');
  if (!target) return;
  if (!info.available) {
    target.innerHTML = `<div class="obs-empty">${esc(info.detail || 'Not available.')}</div>`;
    return;
  }
  target.innerHTML = `<div class="obs-payload">
    <h4 class="obs-subhead">Input</h4>
    <pre class="obs-pre">${esc(info.request) || '<span class="obs-muted">(empty)</span>'}</pre>
    <h4 class="obs-subhead">Output</h4>
    <pre class="obs-pre">${esc(info.response) || '<span class="obs-muted">(empty)</span>'}</pre>
  </div>`;
  target.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderMemory(payload) {
  const rows = payload.conversations.map((c) => [
    esc(c.title || c.conversation_id),
    esc(c.character_name || '—'),
    num(fmtNumber(c.message_count)),
    num(`${fmtNumber(c.context_tokens_estimated)} / ${fmtNumber(c.context_limit)}`),
    num(`${c.context_pressure_pct}%`),
    c.has_stored_summary ? fmtAgo(c.last_summary_at) : '<span class="obs-muted">none</span>',
    num(c.summarization_failures > 0
      ? `<span class="obs-error-text">${fmtNumber(c.summarization_failures)}</span>`
      : '0'),
  ]);
  memoryWrap.innerHTML = table(
    ['Conversation', 'Character', 'Messages', 'Context (est.)', 'Pressure', 'Last summary', 'Failures'],
    rows,
    'No conversation recorded yet.',
  );
}

function renderSchedules(schedules) {
  const rows = schedules.map((s) => [
    esc(s.character_name || s.character_id || '—'),
    esc(s.conversation_title || s.conversation_id || '—'),
    esc(s.trigger),
    s.enabled ? badge('Enabled', 'running') : badge('Disabled', 'disabled'),
    s.next_run_at ? fmtAgo(s.next_run_at) : '—',
    fmtAgo(s.last_execution_at),
    `${outcomeBadge(s.last_execution_status)}${
      s.last_execution_reason ? `<div class="obs-muted">${esc(s.last_execution_reason)}</div>` : ''
    }`,
  ]);
  schedulesWrap.innerHTML = table(
    ['Character', 'Conversation', 'Trigger', 'State', 'Next run', 'Last run', 'Result'],
    rows,
    'No proactive schedule instance.',
  );
}

function renderErrors(errors) {
  const rows = errors.map((e) => [
    fmtAgo(e.timestamp),
    esc(e.component),
    `<span class="obs-error-text">${esc(e.summary)}</span>`,
    [e.bot_id, e.conversation_id, e.schedule_id].filter(Boolean).map(esc).join('<br>') || '—',
  ]);
  errorsWrap.innerHTML = `<div class="obs-note">In-memory history — cleared when the server restarts.</div>` +
    table(['When', 'Component', 'Error', 'Related'], rows, 'No operational error recorded.');
}

// ── Controller ───────────────────────────────────────────────────────────────

export function initObservability({ showToast }) {
  let visible = false;
  let timer = null;
  let inFlight = false;

  function parseTri(value) {
    if (value === 'true') return true;
    if (value === 'false') return false;
    return '';
  }

  async function refresh() {
    if (inFlight) return;
    inFlight = true;
    try {
      const [overview, bots, sessions, llm, memory, schedules, errors] = await Promise.all([
        api.overview({ hours: RANGE_HOURS }),
        api.telegram(),
        api.sessions({
          transport: sessionTransportEl.value,
          bot_id: sessionBotEl.value,
        }),
        api.llm({
          hours: RANGE_HOURS,
          limit: RECENT_LIMIT,
          generation_type: llmTypeEl.value,
          success: parseTri(llmStatusEl.value),
        }),
        api.memory({}),
        api.schedules({}),
        api.errors({ hours: RANGE_HOURS }),
      ]);
      feedbackEl.innerHTML = '';
      renderSummary(overview);
      renderTelegram(bots);
      renderSessions(sessions);
      renderLLM(llm);
      renderMemory(memory);
      renderSchedules(schedules);
      renderErrors(errors);
    } catch (err) {
      feedbackEl.innerHTML = `<div class="error-banner">${esc(err.message)}</div>`;
      if (showToast) showToast(`Operations: ${err.message}`);
    } finally {
      inFlight = false;
    }
  }

  // Lightweight periodic refresh, only while the section is on screen.
  function schedule() {
    clearInterval(timer);
    timer = null;
    if (visible && autoRefreshEl && autoRefreshEl.checked) {
      timer = setInterval(refresh, 30000);
    }
  }

  function setVisible(value) {
    visible = value;
    schedule();
  }

  if (refreshBtn) refreshBtn.addEventListener('click', refresh);
  if (autoRefreshEl) autoRefreshEl.addEventListener('change', schedule);
  [sessionTransportEl, sessionBotEl, llmTypeEl, llmStatusEl]
    .filter(Boolean)
    .forEach((el) => el.addEventListener('change', refresh));

  // Drill-down: links carry a "section:filter=value" instruction.
  document.getElementById('section-observability').addEventListener('click', async (ev) => {
    const webhookBtn = ev.target.closest('[data-webhook]');
    if (webhookBtn) {
      ev.preventDefault();
      try {
        renderWebhook(await api.webhook(webhookBtn.dataset.webhook));
      } catch (err) {
        if (showToast) showToast(`Webhook info: ${err.message}`);
      }
      return;
    }
    const payloadBtn = ev.target.closest('[data-payload]');
    if (payloadBtn) {
      ev.preventDefault();
      try {
        renderPayload(await api.payload(payloadBtn.dataset.payload));
      } catch (err) {
        if (showToast) showToast(`Generation bodies: ${err.message}`);
      }
      return;
    }
    const jump = ev.target.closest('[data-jump]');
    if (!jump) return;
    ev.preventDefault();
    const [target, filter] = jump.dataset.jump.split(':');
    const [key, value] = (filter || '').split('=');
    if (target === 'sessions' && key === 'bot') sessionBotEl.value = value;
    if (target === 'llm' && key === 'conversation') {
      const payload = await api.llm({
        hours: RANGE_HOURS, limit: RECENT_LIMIT, conversation_id: value,
      });
      renderLLM(payload);
      llmWrap.scrollIntoView({ behavior: 'smooth', block: 'start' });
      return;
    }
    if (target === 'memory' && key === 'conversation') {
      const payload = await api.memory({ conversation_id: value });
      renderMemory(payload);
      memoryWrap.scrollIntoView({ behavior: 'smooth', block: 'start' });
      return;
    }
    refresh();
  });

  return { refresh, setVisible };
}
