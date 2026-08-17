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
const DESKTOP = { width: 1280, height: 800 };

/**
 * Build a static copy of the admin page: script tags removed (no API server in
 * this test) and root-relative asset URLs rewritten to absolute file URLs, so
 * the real admin markup is rendered with the real stylesheets.
 */
async function buildHarness() {
  const html = await fs.readFile(path.join(frontendDir, 'admin', 'index.html'), 'utf8');
  const stripped = html
    .replace(/<script[\s\S]*?<\/script>/g, '')
    .replace(/(href|src)="\/([^"]+)"/g, (_, attr, rel) =>
      `${attr}="${pathToFileURL(path.join(frontendDir, rel)).href}"`);
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'auberge-admin-'));
  const file = path.join(dir, 'admin.html');
  await fs.writeFile(file, stripped, 'utf8');
  return pathToFileURL(file).href;
}

async function withPage(viewport, fn) {
  const url = await buildHarness();
  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--allow-file-access-from-files'],
  });
  const page = await browser.newPage({ viewport });
  try {
    await page.goto(url, { waitUntil: 'load' });
    // The static copy has no JS: reveal every section for layout checks.
    await page.evaluate(() => {
      document.querySelectorAll('.admin-section').forEach(s => { s.style.display = ''; });
    });
    await fn(page);
  } finally {
    await page.close();
    await browser.close();
  }
}

test('mobile: nav stacks above the content as a tab bar', async () => {
  await withPage(MOBILE, async (page) => {
    const { nav, content } = await page.evaluate(() => ({
      nav: document.getElementById('admin-nav').getBoundingClientRect().toJSON(),
      content: document.getElementById('admin-content').getBoundingClientRect().toJSON(),
    }));
    assert.ok(nav.bottom <= content.top + 1, 'nav should sit above the content');
    assert.ok(nav.width > MOBILE.width * 0.9, 'nav should span the viewport width');
  });
});

test('mobile: nav buttons meet the 44px touch-target height', async () => {
  await withPage(MOBILE, async (page) => {
    const heights = await page.$$eval('.nav-btn', els => els.map(e => e.getBoundingClientRect().height));
    assert.ok(heights.length > 0);
    for (const h of heights) assert.ok(h >= 44, `nav button height ${h} < 44`);
  });
});

test('mobile: page does not scroll horizontally', async () => {
  await withPage(MOBILE, async (page) => {
    const overflow = await page.evaluate(() => {
      const doc = document.documentElement;
      const content = document.getElementById('admin-content');
      return {
        page: doc.scrollWidth - doc.clientWidth,
        content: content.scrollWidth - content.clientWidth,
      };
    });
    assert.ok(overflow.page <= 1, `document overflows horizontally by ${overflow.page}px`);
    assert.ok(overflow.content <= 1, `admin content overflows horizontally by ${overflow.content}px`);
  });
});

test('mobile: dialogs fit the viewport width', async () => {
  await withPage(MOBILE, async (page) => {
    const widths = await page.evaluate(() => {
      const out = [];
      document.querySelectorAll('.dialog-backdrop').forEach(bd => {
        bd.style.display = 'flex';
        out.push(bd.querySelector('.dialog').getBoundingClientRect().width);
      });
      return out;
    });
    assert.ok(widths.length > 0);
    for (const w of widths) assert.ok(w <= MOBILE.width, `dialog width ${w} > viewport`);
  });
});

test('mobile: full-page editors fit the viewport width', async () => {
  await withPage(MOBILE, async (page) => {
    const widths = await page.evaluate(() => {
      const out = [];
      document.querySelectorAll('.editor-page').forEach(ed => {
        ed.style.display = 'block';
        out.push(ed.querySelector('.editor-panel').getBoundingClientRect().width);
      });
      return out;
    });
    assert.ok(widths.length > 0, 'no editor page found');
    for (const w of widths) assert.ok(w <= MOBILE.width, `editor width ${w} > viewport`);
  });
});

test('editors are pages, not click-to-close layers', async () => {
  await withPage(DESKTOP, async (page) => {
    const result = await page.evaluate(() => {
      const editor = document.getElementById('char-dialog');
      // A page participates in the document flow instead of covering it.
      const position = getComputedStyle(editor).position;
      // Clicking beside the form must not discard what was typed.
      editor.style.display = 'block';
      editor.click();
      return { position, stillOpen: editor.style.display !== 'none' };
    });
    assert.notStrictEqual(result.position, 'fixed', 'editor still renders as an overlay');
    assert.ok(result.stillOpen, 'clicking the editor background closed it');
  });
});

test('desktop: nav stays in the left column', async () => {
  await withPage(DESKTOP, async (page) => {
    const { nav, content } = await page.evaluate(() => ({
      nav: document.getElementById('admin-nav').getBoundingClientRect().toJSON(),
      content: document.getElementById('admin-content').getBoundingClientRect().toJSON(),
    }));
    assert.ok(nav.right <= content.left + 1, 'nav should sit left of the content');
  });
});
