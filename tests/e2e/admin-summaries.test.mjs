/**
 * Exercises the admin "Summaries" debug screen against a stubbed API.
 *
 * A tiny static server serves the real frontend plus fake /api/summaries
 * responses, so the real summaries.js runs against the real markup and
 * stylesheets. Only the backend is faked.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { chromium } from 'playwright';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.join(__dirname, '..', '..', 'frontend');

const CONV_ID = 'conv-1';
const LIST_ROW = {
  conversation_id: CONV_ID,
  title: 'Night at the inn',
  character_name: 'Aria',
  message_count: 12,
  messages_since_summary: 4,
  context_tokens: 900,
  context_window: 4096,
  threshold: 0.75,
  budget_tokens: 2816,
  summary_count: 1,
  last_summary_at: '2026-08-12T10:00:00+00:00',
  updated_at: '2026-08-12T10:05:00+00:00',
};
const DETAIL = {
  conversation_id: CONV_ID,
  context_tokens: 900,
  budget_tokens: 2816,
  context_window: 4096,
  threshold: 0.75,
  summary: 'RELATIONSHIP: allies. EVENTS: met at the inn.',
  summaries: [{
    id: 's1', content: 'RELATIONSHIP: allies. EVENTS: met at the inn.',
    covers_until_message_id: 'm8', covers_message_count: 8,
    based_on_summary_id: '', tokens: 12, created_at: '2026-08-12T10:00:00+00:00',
  }],
  messages_since: [
    { id: 'm9', role: 'user', content: 'MARKER_USER_MESSAGE', timestamp: '2026-08-12T10:01:00+00:00' },
    { id: 'm10', role: 'assistant', content: 'MARKER_ASSISTANT_MESSAGE', timestamp: '2026-08-12T10:02:00+00:00' },
  ],
};

const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript',
  '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml',
};

async function harnessHtml() {
  const html = await fs.readFile(path.join(frontendDir, 'admin', 'index.html'), 'utf8');
  const section = html
    .match(/<section id="section-summaries"[\s\S]*?<\/section>/)[0]
    .replace('style="display:none"', '');  // the nav is not part of this harness
  return `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<link rel="stylesheet" href="/css/main.css"><link rel="stylesheet" href="/css/admin.css">
</head><body><div id="admin-main"><main id="admin-content">${section}</main></div>
<script type="module">
  import { setAdminToken } from '/js/admin/auth.js';
  import { initSummaries } from '/js/admin/summaries.js';
  setAdminToken('fake-token');
  window.__toasts = [];
  const ctrl = initSummaries({ showToast: (m) => window.__toasts.push(m) });
  window.__refresh = () => ctrl.refresh();
  ctrl.refresh();
</script></body></html>`;
}

async function startServer(calls) {
  const page = await harnessHtml();
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    calls.push(`${req.method} ${req.url}`);
    if (url.pathname === '/e2e/summaries.html') {
      res.writeHead(200, { 'content-type': 'text/html' });
      return res.end(page);
    }
    if (url.pathname === '/api/summaries/') {
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify([LIST_ROW]));
    }
    if (url.pathname === `/api/summaries/${CONV_ID}/summarize`) {
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ created: true, summary: DETAIL.summaries[0] }));
    }
    if (url.pathname === `/api/summaries/${CONV_ID}`) {
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify(
        req.method === 'DELETE' ? { deleted: 1 } : DETAIL));
    }
    try {
      const file = path.join(frontendDir, url.pathname);
      const body = await fs.readFile(file);
      res.writeHead(200, { 'content-type': MIME[path.extname(file)] || 'text/plain' });
      res.end(body);
    } catch {
      res.writeHead(404);
      res.end('not found');
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return { server, port: server.address().port };
}

async function withPage(fn) {
  const calls = [];
  const { server, port } = await startServer(calls);
  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  try {
    await page.goto(`http://127.0.0.1:${port}/e2e/summaries.html`, { waitUntil: 'load' });
    await fn(page, calls);
  } finally {
    await page.close();
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}

test('lists conversations with their context size against the budget', async () => {
  await withPage(async (page) => {
    await page.waitForSelector('#summaries-list table tbody tr');
    const row = await page.textContent('#summaries-list tbody tr');
    assert.ok(row.includes('Night at the inn'));
    assert.ok(row.includes('Aria'));
    assert.ok(row.includes('900 / 2816'), `budget column missing in: ${row}`);
  });
});

test('inspecting a conversation shows the summary and the messages that follow', async () => {
  await withPage(async (page) => {
    await page.click('#summaries-list button[data-detail]');
    await page.waitForSelector('#summaries-force-btn');

    const detail = await page.textContent('#summaries-detail');
    assert.ok(detail.includes('RELATIONSHIP: allies'));
    assert.ok(detail.includes('MARKER_USER_MESSAGE'));
    assert.ok(detail.includes('MARKER_ASSISTANT_MESSAGE'));
    assert.ok(detail.includes('Messages since (2)'));
  });
});

test('the action buttons call the summarize and delete endpoints', async () => {
  await withPage(async (page, calls) => {
    await page.click('#summaries-list button[data-detail]');
    await page.waitForSelector('#summaries-force-btn');

    await page.click('#summaries-force-btn');
    await page.waitForFunction(() => window.__toasts.length > 0);
    assert.ok(calls.includes(`POST /api/summaries/${CONV_ID}/summarize`));

    await page.click('#summaries-del-btn');
    await page.waitForFunction(() => window.__toasts.length > 1);
    assert.ok(calls.includes(`DELETE /api/summaries/${CONV_ID}`));

    await page.click('#summaries-del-all-btn');
    await page.waitForFunction(() => window.__toasts.length > 2);
    assert.ok(calls.includes(`DELETE /api/summaries/${CONV_ID}?all=true`));
  });
});
