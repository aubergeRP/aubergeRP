import { initAdminAuth } from '/js/admin/auth.js?v=2';
import { initConnectors, applyHealthBadges } from '/js/admin/connectors.js?v=2';
import { initCharacters } from '/js/admin/characters.js?v=2';
import { initMedias }    from '/js/admin/medias.js?v=3';
import { initConfig }     from '/js/admin/config.js?v=2';
import { initStatistics } from '/js/admin/statistics.js?v=2';
import { initPrompts }    from '/js/admin/prompts.js?v=2';
import { initTelegram }   from '/js/admin/telegram.js?v=1';
import { applyGuiCustomization } from '/js/gui-customization.js?v=2';
import { initStatusBar, setStatusItem, initHeaderLogo } from '/js/layout.js?v=2';

initStatusBar();
initHeaderLogo({ badge: 'Admin' });

async function main() {
  const authenticated = await initAdminAuth();
  if (!authenticated) return;
  applyGuiCustomization();

  // ── Section routing (hash-based) ────────────────────────────────────────
  const SECTIONS = ['connectors', 'characters', 'medias', 'health', 'statistics', 'config', 'customize', 'prompts', 'telegram'];
  const sectionEls = {};
  const navBtns = {};

  SECTIONS.forEach(s => {
    sectionEls[s] = document.getElementById(`section-${s}`);
    navBtns[s] = document.querySelector(`.nav-btn[data-section="${s}"]`);
  });

  function activateSection(name) {
    if (!SECTIONS.includes(name)) name = 'connectors';
    SECTIONS.forEach(s => {
      sectionEls[s].style.display = (s === name) ? '' : 'none';
      navBtns[s].classList.toggle('active', s === name);
      navBtns[s].setAttribute('aria-current', s === name ? 'page' : 'false');
    });
    window.location.hash = name;
    if (name === 'connectors') connectorCtrl.refresh();
    else if (name === 'characters') charCtrl.refresh();
    else if (name === 'medias') mediaCtrl.refresh();
    else if (name === 'health') loadHealth();
    else if (name === 'statistics') statsCtrl.refresh();
    else if (name === 'config') configCtrl.refresh();
    else if (name === 'customize') loadCustomize();
    else if (name === 'prompts') promptsCtrl.refresh();
    else if (name === 'telegram') telegramCtrl.refresh();
  }

  SECTIONS.forEach(s => {
    navBtns[s].addEventListener('click', () => activateSection(s));
  });

  // ── Toast ─────────────────────────────────────────────────────────────
  const toastEl = document.getElementById('toast');
  let toastTimer = null;
  function showToast(msg, isError = true) {
    toastEl.textContent = msg;
    toastEl.className = isError ? 'show error' : 'show success';
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.className = ''; }, 3500);
  }
  window.adminShowToast = showToast;

  // ── Confirm dialog ────────────────────────────────────────────────────
  const confirmDialog = document.getElementById('confirm-dialog');
  const confirmMsg = document.getElementById('confirm-dialog-msg');
  const confirmOk = document.getElementById('confirm-ok');
  const confirmCancel = document.getElementById('confirm-cancel');
  let confirmResolve = null;

  function showConfirm(msg) {
    confirmMsg.textContent = msg;
    confirmDialog.style.display = 'flex';
    return new Promise(resolve => { confirmResolve = resolve; });
  }
  confirmOk.addEventListener('click', () => { confirmDialog.style.display = 'none'; confirmResolve && confirmResolve(true); });
  confirmCancel.addEventListener('click', () => { confirmDialog.style.display = 'none'; confirmResolve && confirmResolve(false); });
  window.adminShowConfirm = showConfirm;

  // ── Health section ────────────────────────────────────────────────────
  async function loadHealth() {
    const el = document.getElementById('health-content');
    el.innerHTML = '<div class="loading-row">Loading…</div>';
    try {
      const [health, chars, convs] = await Promise.all([
        fetch('/api/health/').then(r => r.json()),
        fetch('/api/characters/').then(r => r.json()),
        fetch('/api/conversations/').then(r => r.json()),
      ]);

      const connTypes = ['text', 'image', 'video', 'audio'];
      const connRows = connTypes.map(t => {
        const c = health.connectors?.[t];
        if (!c) return `<div class="health-row"><span class="health-type">${t}</span><span class="health-na">— Not available</span></div>`;
        const icon = c.connected ? '✅' : '❌';
        return `<div class="health-row"><span class="health-type">${t}</span> ${icon} <strong>${c.name}</strong></div>`;
      }).join('');

      const c = health.connectors || {};
      setStatusItem('text', c.text);
      setStatusItem('image', c.image);
      setStatusItem('video', c.video);
      setStatusItem('audio', c.audio);

      el.innerHTML = `
        <dl class="health-dl">
          <dt>aubergeRP Version</dt><dd>${health.version}</dd>
        </dl>
        <h3 class="health-subheading">Active Connectors</h3>
        <div class="health-connectors">${connRows}</div>
        <h3 class="health-subheading">Storage</h3>
        <dl class="health-dl">
          <dt>Characters</dt><dd>${Array.isArray(chars) ? chars.length : '?'}</dd>
          <dt>Conversations</dt><dd>${Array.isArray(convs) ? convs.length : '?'}</dd>
        </dl>
        <h3 class="health-subheading">API Reference</h3>
        <dl class="health-dl">
          <dt>Interactive Docs</dt><dd><a href="/api-docs" target="_blank">Open API Reference (Redoc)</a></dd>
          <dt>OpenAPI Spec</dt><dd><a href="/openapi.json" target="_blank">/openapi.json</a></dd>
        </dl>
      `;
    } catch (err) {
      el.innerHTML = `<div class="error-banner">Cannot connect to aubergeRP API: ${err.message}</div>`;
    }
  }

  document.getElementById('refresh-health-btn').addEventListener('click', loadHealth);

  // ── Customization section ─────────────────────────────────────────────
  async function loadCustomize() {
    const feedbackEl = document.getElementById('customize-feedback');
    feedbackEl.innerHTML = '';
    try {
      const cfg = await fetch('/api/config/gui').then(r => r.json());
      document.getElementById('customize-css').value = cfg.custom_css || '';
      document.getElementById('customize-header-html').value = cfg.custom_header_html || '';
      document.getElementById('customize-footer-html').value = cfg.custom_footer_html || '';
      refreshCustomizeHighlights();
    } catch (err) {
      feedbackEl.innerHTML = `<div class="error-banner">Cannot load GUI config: ${err.message}</div>`;
    }
  }

  document.getElementById('customize-save-btn').addEventListener('click', async () => {
    const feedbackEl = document.getElementById('customize-feedback');
    feedbackEl.innerHTML = '';
    const btn = document.getElementById('customize-save-btn');
    btn.disabled = true;
    btn.textContent = 'Saving…';
    const body = {
      custom_css: document.getElementById('customize-css').value,
      custom_header_html: document.getElementById('customize-header-html').value,
      custom_footer_html: document.getElementById('customize-footer-html').value,
    };
    try {
      await fetch('/api/config/gui', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      showToast('Customization saved. Reload pages to apply.', false);
    } catch (err) {
      feedbackEl.innerHTML = `<div class="error-banner">${err.message}</div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Save';
    }
  });

  // ── Init modules ──────────────────────────────────────────────────────
  const connectorCtrl  = initConnectors({ showToast, showConfirm });
  const charCtrl       = initCharacters({ showToast, showConfirm });
  const mediaCtrl      = initMedias({ showToast, showConfirm });
  const statsCtrl      = initStatistics({ showToast });
  const configCtrl     = initConfig({ showToast });
  const promptsCtrl    = initPrompts({ showToast });
  const telegramCtrl   = initTelegram({ showToast, showConfirm });

  const _customizeEditors = [
    { inputId: 'customize-css', highlightId: 'customize-css-highlight', language: 'css' },
    { inputId: 'customize-header-html', highlightId: 'customize-header-html-highlight', language: 'html' },
    { inputId: 'customize-footer-html', highlightId: 'customize-footer-html-highlight', language: 'html' },
  ];

  function initCustomizeSyntaxHighlight() {
    _customizeEditors.forEach(({ inputId, highlightId, language }) => {
      const input = document.getElementById(inputId);
      const highlight = document.getElementById(highlightId);
      if (!input || !highlight) return;
      const refresh = () => renderSyntaxHighlight(input, highlight, language);
      input.addEventListener('input', refresh);
      input.addEventListener('scroll', () => syncHighlightScroll(input, highlight));
      refresh();
      syncHighlightScroll(input, highlight);
    });
  }

  function refreshCustomizeHighlights() {
    _customizeEditors.forEach(({ inputId, highlightId, language }) => {
      const input = document.getElementById(inputId);
      const highlight = document.getElementById(highlightId);
      if (!input || !highlight) return;
      renderSyntaxHighlight(input, highlight, language);
    });
  }

  function renderSyntaxHighlight(input, highlight, language) {
    const raw = input.value || '';
    const highlighted = language === 'css' ? highlightCss(raw) : highlightHtml(raw);
    // Keep one visual line when empty and preserve trailing newline alignment.
    highlight.innerHTML = highlighted || '<span class="tok-muted"> </span>';
    if (raw.endsWith('\n')) {
      highlight.innerHTML += '\n';
    }
    syncHighlightScroll(input, highlight);
  }

  function syncHighlightScroll(input, highlight) {
    highlight.scrollTop = input.scrollTop;
    highlight.scrollLeft = input.scrollLeft;
  }

  function escCode(text) {
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function highlightCss(code) {
    let out = escCode(code);
    out = out.replace(/\/\*[\s\S]*?\*\//g, '<span class="tok-comment">$&</span>');
    out = out.replace(/([.#]?[a-zA-Z_][\w-]*)(\s*\{)/g, '<span class="tok-selector">$1</span>$2');
    out = out.replace(/([a-z-]+)(\s*:)/gi, '<span class="tok-attr">$1</span>$2');
    out = out.replace(/(:\s*)([^;}{\n]+)/g, '$1<span class="tok-value">$2</span>');
    out = out.replace(/([0-9]+(?:\.[0-9]+)?(?:px|em|rem|%|vh|vw|s|ms)?)/g, '<span class="tok-number">$1</span>');
    return out;
  }

  function highlightHtml(code) {
    let out = escCode(code);
    out = out.replace(/(&lt;!--[\s\S]*?--&gt;)/g, '<span class="tok-comment">$1</span>');
    out = out.replace(/(&lt;\/?)([a-zA-Z][\w:-]*)([^&]*?)(\/?&gt;)/g, (_, p1, p2, p3, p4) => {
      let attrs = p3.replace(/(\s)([a-zA-Z_:][\w:.-]*)(=)(&quot;.*?&quot;|&#39;.*?&#39;)/g,
        '$1<span class="tok-attr">$2</span>$3<span class="tok-string">$4</span>');
      return `${p1}<span class="tok-tag">${p2}</span>${attrs}${p4}`;
    });
    return out;
  }

  initCustomizeSyntaxHighlight();

  // ── Health polling (every 30s) — updates connector status badges ──────────
  async function pollHealth() {
    try {
      const health = await fetch('/api/health/').then(r => r.json());
      applyHealthBadges(health);
      const c = health.connectors || {};
      setStatusItem('text', c.text);
      setStatusItem('image', c.image);
      setStatusItem('video', c.video);
      setStatusItem('audio', c.audio);
    } catch (_) {
      // Silently ignore polling errors; badges keep their last-known state
    }
  }
  pollHealth();
  setInterval(pollHealth, 30000);

  // ── Initial section ───────────────────────────────────────────────────
  const hash = window.location.hash.replace('#', '');
  activateSection(SECTIONS.includes(hash) ? hash : 'connectors');

  window.addEventListener('hashchange', () => {
    const h = window.location.hash.replace('#', '');
    activateSection(SECTIONS.includes(h) ? h : 'connectors');
  });
} // end main()

main();
