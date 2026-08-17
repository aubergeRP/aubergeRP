import { drawBarChart } from '/vendor/simple-charts.js';

//: Single, fixed reporting window — one less knob on the page.
const RANGE_DAYS = 14;

const feedbackEl = document.getElementById('stats-feedback');
const summaryEl = document.getElementById('stats-summary');
const refreshBtn = document.getElementById('refresh-stats-btn');
const timelineCanvas = document.getElementById('stats-timeline-chart');
const connectorsTableWrap = document.getElementById('stats-connectors-table-wrap');
const conversationsTableWrap = document.getElementById('stats-conversations-table-wrap');

let showToastFn = () => {};

async function apiFetch(path) {
  const res = await fetch(path);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body && body.detail) detail = body.detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

export function initStatistics({ showToast }) {
  showToastFn = showToast;
  refreshBtn.addEventListener('click', refresh);
  window.addEventListener('resize', redrawCharts);
  return { refresh };
}

let latestPayload = null;

async function refresh() {
  feedbackEl.innerHTML = '';
  summaryEl.innerHTML = '<div class="loading-row">Loading…</div>';
  connectorsTableWrap.innerHTML = '';
  conversationsTableWrap.innerHTML = '';

  try {
    const payload = await apiFetch(`/api/statistics/?days=${RANGE_DAYS}&top=15`);
    latestPayload = payload;
    renderSummary(payload.summary || {});
    renderTables(payload);
    redrawCharts();
  } catch (err) {
    latestPayload = null;
    summaryEl.innerHTML = '';
    feedbackEl.innerHTML = `<div class="error-banner">Cannot load statistics: ${escHtml(err.message)}</div>`;
    showToastFn('Failed to load statistics.', true);
  }
}

function redrawCharts() {
  if (!latestPayload) return;
  renderTimelineChart(latestPayload.timeline || []);
}

function renderSummary(summary) {
  const cards = [
    { label: 'Messages', value: formatInt(summary.total_messages) },
    { label: 'Conversations', value: formatInt(summary.total_conversations) },
    { label: 'LLM Calls', value: formatInt(summary.llm_calls) },
    { label: 'Success Rate', value: `${Number(summary.success_rate || 0).toFixed(1)}%` },
    { label: 'Total Tokens', value: formatInt(summary.total_tokens) },
    { label: 'Avg Latency', value: `${Math.round(Number(summary.avg_latency_ms || 0))} ms` },
  ];
  summaryEl.innerHTML = cards
    .map(card => `
      <article class="stats-card">
        <div class="stats-card-label">${escHtml(card.label)}</div>
        <div class="stats-card-value">${escHtml(card.value)}</div>
      </article>
    `)
    .join('');
}

function renderTimelineChart(timeline) {
  const labels = timeline.map(row => String(row.date || '').slice(5));
  const values = timeline.map(row => (Number(row.tokens_in || 0) + Number(row.tokens_out || 0)));
  drawBarChart(timelineCanvas, labels, values, {
    title: 'Tokens per day',
    barColor: '#50e3c2',
    labelColor: 'rgba(240,245,255,0.75)',
    gridColor: 'rgba(240,245,255,0.11)',
  });
}

function renderTables(payload) {
  connectorsTableWrap.innerHTML = renderTable(
    ['Connector', 'Backend', 'Calls', 'Failed', 'Tokens In', 'Tokens Out', 'Avg Latency'],
    (payload.by_connector || []).map(row => [
      row.name || '(unknown)',
      row.backend || '(unknown)',
      num(formatInt(row.llm_calls)),
      num(formatInt(row.failed)),
      num(formatInt(row.tokens_in)),
      num(formatInt(row.tokens_out)),
      num(`${Math.round(Number(row.avg_latency_ms || 0))} ms`),
    ]),
    'No connector usage recorded yet.'
  );

  conversationsTableWrap.innerHTML = renderTable(
    ['Conversation', 'Messages', 'LLM Calls', 'Tokens In', 'Tokens Out', 'Avg Latency'],
    (payload.by_conversation || []).map(row => [
      row.title || row.conversation_id,
      num(formatInt(row.message_count)),
      num(formatInt(row.llm_calls)),
      num(formatInt(row.tokens_in)),
      num(formatInt(row.tokens_out)),
      num(`${Math.round(Number(row.avg_latency_ms || 0))} ms`),
    ]),
    'No conversation usage recorded yet.'
  );
}

// Cells wrapped in num() are right-aligned, header included.
function num(value) {
  return { num: true, value: String(value) };
}

function renderTable(headers, rows, emptyMessage) {
  if (!rows.length) {
    return `<div class="loading-row">${escHtml(emptyMessage)}</div>`;
  }

  const numeric = new Set();
  rows.forEach(cols => cols.forEach((c, i) => { if (c && c.num) numeric.add(i); }));
  const cls = i => (numeric.has(i) ? ' class="num"' : '');
  const thead = `<thead><tr>${headers
    .map((h, i) => `<th${cls(i)}>${escHtml(h)}</th>`).join('')}</tr></thead>`;
  const tbody = `<tbody>${rows
    .map(cols => `<tr>${cols
      .map((c, i) => `<td${cls(i)}>${escHtml(c && c.num ? c.value : c)}</td>`)
      .join('')}</tr>`)
    .join('')}</tbody>`;

  return `<div class="stats-table-wrap"><table class="stats-table">${thead}${tbody}</table></div>`;
}

function formatInt(value) {
  const n = Number(value || 0);
  return n.toLocaleString('en-US');
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
