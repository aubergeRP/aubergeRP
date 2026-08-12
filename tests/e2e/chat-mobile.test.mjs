import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { chromium } from 'playwright';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.join(__dirname, '..', '..', 'frontend');

const MOBILE = { width: 390, height: 844 };

/**
 * Static copy of the chat page: scripts removed (no API server here) and
 * root-relative asset URLs rewritten to absolute file URLs, so the real markup
 * is rendered with the real stylesheet.
 */
async function buildHarness() {
  const html = await fs.readFile(path.join(frontendDir, 'index.html'), 'utf8');
  const stripped = html
    .replace(/<script[\s\S]*?<\/script>/g, '')
    .replace(/(href|src)="\/([^"]+)"/g, (_, attr, rel) =>
      `${attr}="${pathToFileURL(path.join(frontendDir, rel)).href}"`);
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'auberge-chat-'));
  const file = path.join(dir, 'chat.html');
  await fs.writeFile(file, stripped, 'utf8');
  return pathToFileURL(file).href;
}

async function withPage(fn) {
  const url = await buildHarness();
  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--allow-file-access-from-files'],
  });
  const page = await browser.newPage({ viewport: MOBILE });
  try {
    await page.goto(url, { waitUntil: 'load' });
    // The static copy has no JS: fill in what app.js would render and reveal
    // the chat panes that start hidden.
    await page.evaluate(() => {
      document.querySelector('#header .logo').textContent = 'aubergeRP';
      ['char-header', 'message-list', 'input-area', 'generate-image-btn'].forEach(id => {
        document.getElementById(id).style.display = '';
      });
      document.getElementById('empty-state').style.display = 'none';
    });
    await fn(page);
  } finally {
    await page.close();
    await browser.close();
  }
}

test('mobile: page does not scroll horizontally', async () => {
  await withPage(async (page) => {
    const overflow = await page.evaluate(() => {
      const doc = document.documentElement;
      return doc.scrollWidth - doc.clientWidth;
    });
    assert.ok(overflow <= 1, `document overflows horizontally by ${overflow}px`);
  });
});

test('mobile: header controls stay on a single row', async () => {
  await withPage(async (page) => {
    const rects = await page.$$eval(
      '#header > *',
      els => els.filter(e => getComputedStyle(e).display !== 'none')
               .map(e => e.getBoundingClientRect().toJSON()),
    );
    assert.ok(rects.length >= 3);
    const header = await page.$eval('#header', e => e.getBoundingClientRect().toJSON());
    for (const r of rects) {
      assert.ok(r.right <= header.right + 1, `header control overflows: right=${r.right}`);
      assert.ok(r.bottom <= header.bottom + 1, 'header control wrapped to a second row');
    }
  });
});

test('mobile: composer input uses a 16px font so iOS does not zoom', async () => {
  await withPage(async (page) => {
    const size = await page.$eval('#msg-input', e => parseFloat(getComputedStyle(e).fontSize));
    assert.ok(size >= 16, `input font-size is ${size}px, iOS Safari will zoom on focus`);
  });
});

test('mobile: composer leaves most of the screen to the messages', async () => {
  await withPage(async (page) => {
    const inputHeight = await page.$eval('#input-area', e => e.getBoundingClientRect().height);
    assert.ok(inputHeight <= MOBILE.height * 0.2,
      `composer takes ${Math.round(inputHeight)}px of ${MOBILE.height}px`);
  });
});

test('mobile: send and image buttons meet the touch-target size', async () => {
  await withPage(async (page) => {
    const heights = await page.$$eval('#send-btn, #generate-image-btn',
      els => els.map(e => e.getBoundingClientRect().height));
    assert.equal(heights.length, 2);
    for (const h of heights) assert.ok(h >= 44, `button height ${h} < 44`);
  });
});
