import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { chromium } from 'playwright';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const harnessUrl = pathToFileURL(path.join(__dirname, 'harness', 'chat-streaming.html')).href;

async function withPage(fn) {
  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--allow-file-access-from-files'],
  });
  const page = await browser.newPage();
  try {
    await page.goto(harnessUrl, { waitUntil: 'load' });
    await fn(page);
  } finally {
    await page.close();
    await browser.close();
  }
}

test('shows a visible in-progress placeholder before assistant text arrives', async () => {
  await withPage(async (page) => {
    await page.evaluate(() => window.__chatHarness.startImage('a lantern-lit tavern interior'));

    await page.waitForSelector('.img-placeholder', { state: 'visible' });

    const placeholderText = await page.textContent('.img-placeholder');
    const bubbleDisplay = await page.$eval('.msg-bubble', (el) => getComputedStyle(el).display);

    assert.ok(placeholderText.includes('Generating image'));
    assert.ok(placeholderText.includes('a lantern-lit tavern interior'));
    assert.equal(bubbleDisplay, 'none');
  });
});

test('updates progress and hides the pending status once the image completes', async () => {
  await withPage(async (page) => {
    await page.evaluate(() => {
      window.__chatHarness.startImage('storm over the keep');
      window.__chatHarness.progressImage(3, 5);
    });

    const labelText = await page.textContent('.img-progress-label');
    assert.ok(labelText.includes('3 / 5'));

    await page.evaluate(() => window.__chatHarness.completeImage('https://example.test/final-image.png'));
    await page.waitForSelector('.msg-image', { state: 'attached' });

    const statusVisible = await page.isVisible('.msg-media-status').catch(() => false);
    const imageSrc = await page.getAttribute('.msg-image', 'src');

    assert.equal(statusVisible, false);
    assert.equal(imageSrc, 'https://example.test/final-image.png');
  });
});

test('dispatchSSEEvent routes image progress events to the visible placeholder', async () => {
  await withPage(async (page) => {
    await page.evaluate(() => window.__chatHarness.dispatchProgressEvent());

    await page.waitForSelector('.img-placeholder', { state: 'visible' });
    const labelText = await page.textContent('.img-progress-label');
    const progressWidth = await page.$eval('.img-progress-bar', (el) => getComputedStyle(el).width);

    assert.ok(labelText.includes('1 / 4'));
    assert.notEqual(progressWidth, '0px');
  });
});

test('shows retry button and error message when image generation fails', async () => {
  await withPage(async (page) => {
    await page.evaluate(() => window.__chatHarness.startAndFailImage('a dragon', 'HTTP 400: Content Policy Violation'));

    await page.waitForSelector('.img-error', { state: 'attached' });

    const errorText = await page.textContent('.img-error');
    assert.ok(errorText.includes('HTTP 400: Content Policy Violation'));
    assert.ok(!errorText.includes('developer.mozilla.org'));

    const retryBtn = await page.$('.img-error .msg-retry-btn');
    assert.ok(retryBtn !== null, 'retry button should be present after image failure');
    const btnText = await retryBtn.textContent();
    assert.equal(btnText.trim(), 'Retry');
  });
});

test('shows retry button even when image_failed arrives without a prior image_start', async () => {
  await withPage(async (page) => {
    // Simulate server sending image_failed without image_start (placeholder missing)
    await page.evaluate(() => window.__chatHarness.failImageOrphan('gen-orphan', 'Unexpected failure'));

    await page.waitForSelector('.img-error', { state: 'attached' });

    const retryBtn = await page.$('.img-error .msg-retry-btn');
    assert.ok(retryBtn !== null, 'retry button should appear even without a placeholder');
  });
});

test('proactive typing indicator is shown then removed when nothing is generated', async () => {
  await withPage(async (page) => {
    await page.evaluate(() => window.__chatHarness.startTyping());
    await page.waitForSelector('.typing-indicator', { state: 'visible' });

    await page.evaluate(() => window.__chatHarness.cancelTyping());
    await page.waitForSelector('.typing-indicator', { state: 'detached' });

    const bubbles = await page.$$('.msg.assistant');
    assert.equal(bubbles.length, 0, 'the empty bubble must be removed too');
  });
});

test('proactive message keeps its bubble when content arrived before cancel', async () => {
  await withPage(async (page) => {
    await page.evaluate(() => {
      window.__chatHarness.startTyping();
      window.__chatHarness.typeToken('Are you still there?');
      window.__chatHarness.cancelTyping();
    });

    await page.waitForSelector('.msg-bubble', { state: 'visible' });
    const text = await page.textContent('.msg-bubble');
    assert.ok(text.includes('Are you still there?'));
  });
});
