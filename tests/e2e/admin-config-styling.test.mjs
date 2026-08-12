/**
 * Guards the dark-theme rendering of the admin form controls.
 *
 * Regression covered: `.field-row` used to enumerate the input types it styled
 * (text/number/password), so any other type — `url`, `email`, … — fell back to
 * the browser's white default. The page also lacked a `color-scheme`
 * declaration, which made native widgets (checkboxes, select popups, number
 * spinners) render in the browser's light theme.
 *
 * Like admin-mobile.test.mjs this renders the real markup with the real
 * stylesheets, scripts stripped — no API server involved.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { chromium } from 'playwright';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.join(__dirname, '..', '..', 'frontend');

const DESKTOP = { width: 1280, height: 900 };

async function buildHarness() {
  const html = await fs.readFile(path.join(frontendDir, 'admin', 'index.html'), 'utf8');
  const stripped = html
    .replace(/<script[\s\S]*?<\/script>/g, '')
    .replace(/(href|src)="\/([^"]+)"/g, (_, attr, rel) =>
      `${attr}="${pathToFileURL(path.join(frontendDir, rel)).href}"`);
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'auberge-admin-css-'));
  const file = path.join(dir, 'admin.html');
  await fs.writeFile(file, stripped, 'utf8');
  return pathToFileURL(file).href;
}

async function withPage(fn) {
  const url = await buildHarness();
  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--allow-file-access-from-files'],
  });
  const page = await browser.newPage({ viewport: DESKTOP });
  try {
    await page.goto(url, { waitUntil: 'load' });
    await page.evaluate(() => {
      document.querySelectorAll('.admin-section').forEach(s => { s.style.display = ''; });
      document.querySelectorAll('details').forEach(d => { d.open = true; });
    });
    await fn(page);
  } finally {
    await page.close();
    await browser.close();
  }
}

/** Perceived luminance of an `rgb()` / `rgba()` string, 0 (black) → 255 (white). */
function luminance(color) {
  const [r, g, b] = color.match(/[\d.]+/g).map(Number);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

test('the page declares a dark color-scheme for native widgets', async () => {
  await withPage(async (page) => {
    const scheme = await page.evaluate(() =>
      getComputedStyle(document.documentElement).colorScheme);
    assert.equal(scheme, 'dark');
  });
});

test('no text-like form control renders on a light background', async () => {
  await withPage(async (page) => {
    const light = await page.evaluate(() => {
      const offenders = [];
      const selector =
        '.field-row input:not([type="checkbox"]):not([type="radio"]):not([type="file"]),' +
        '.field-row select, .field-row textarea';
      for (const el of document.querySelectorAll(selector)) {
        const bg = getComputedStyle(el).backgroundColor;
        const [r, g, b] = bg.match(/[\d.]+/g).map(Number);
        const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
        if (lum > 128) offenders.push(`${el.id || el.tagName}: ${bg}`);
      }
      return offenders;
    });
    assert.deepEqual(light, [], `light backgrounds found: ${light.join(', ')}`);
  });
});

test('the url input is styled like the other text inputs', async () => {
  // #tg-bot-webhook-url is the only type="url" field and was the visible
  // symptom of the enumerated-selector bug.
  await withPage(async (page) => {
    const [urlBg, textBg] = await page.evaluate(() => [
      getComputedStyle(document.getElementById('tg-bot-webhook-url')).backgroundColor,
      getComputedStyle(document.getElementById('cfg-host')).backgroundColor,
    ]);
    assert.equal(urlBg, textBg);
    assert.ok(luminance(urlBg) < 128, `expected a dark background, got ${urlBg}`);
  });
});

test('checkboxes get the accent colour outside .field-row-check too', async () => {
  await withPage(async (page) => {
    const accents = await page.evaluate(() => {
      const out = {};
      for (const el of document.querySelectorAll('.field-row input[type="checkbox"]')) {
        out[el.id] = getComputedStyle(el).accentColor;
      }
      return out;
    });
    const ids = Object.keys(accents);
    assert.ok(ids.length > 0, 'expected checkboxes in the admin form');
    for (const id of ids) {
      assert.notEqual(accents[id], 'auto', `${id} falls back to the browser default`);
    }
  });
});

test('every Configuration setting is present in the form', async () => {
  // Mirrors the FIELDS table in frontend/js/admin/config.js: a setting added to
  // the API but forgotten in the markup would leave the panel incomplete.
  const expected = [
    'cfg-user-name',
    'cfg-active-text', 'cfg-active-image',
    'cfg-public-character-list',
    'cfg-ooc-protection', 'cfg-image-autonomy',
    'cfg-active-text-summarization', 'cfg-active-text-utility',
    'cfg-context-window', 'cfg-summarization-threshold', 'cfg-image-autonomy-cooldown',
    'cfg-host', 'cfg-port', 'cfg-log-level', 'cfg-sentry-dsn',
    'cfg-admin-token-ttl', 'cfg-data-dir',
    'cfg-scheduler-enabled', 'cfg-scheduler-interval', 'cfg-scheduler-cleanup-days',
    'cfg-health-check-enabled', 'cfg-health-check-interval',
    'cfg-metrics-enabled',
  ];
  await withPage(async (page) => {
    const missing = await page.evaluate((ids) =>
      ids.filter(id => !document.querySelector(`#section-config #${id}`)), expected);
    assert.deepEqual(missing, [], `missing from #section-config: ${missing.join(', ')}`);
  });
});

test('the data directory field is read-only', async () => {
  await withPage(async (page) => {
    const readOnly = await page.evaluate(() =>
      document.getElementById('cfg-data-dir').readOnly);
    assert.equal(readOnly, true);
  });
});
