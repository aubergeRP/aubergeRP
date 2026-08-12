/**
 * chat.js — Conversation management, SSE streaming, message rendering.
 */
import {
  fetchConversations,
  fetchConversation,
  createConversation,
  deleteConversation,
  sendMessage,
  generateSceneImage,
  fetchHealth,
  getSessionToken,
  copyShareUrl,
} from './api.js';
import { setStatusItem } from './layout.js';

// ── State ────────────────────────────────────────────────────────────────────

let _activeConversationId = null;
let _activeCharacter = null;
let _streaming = false;
let _healthInterval = null;
let _lastUserMessage = null;
let _pinnedToBottom = true;
let _hasImageConnector = false;

/** EventSource used by other browser tabs to receive remote SSE events. */
let _remoteEventSource = null;
/** Streaming message handler for events arriving on the remote EventSource. */
let _remoteStreaming = null;

// ── Init ─────────────────────────────────────────────────────────────────────

export function initChat() {
  const sendBtn = document.getElementById('send-btn');
  const input = document.getElementById('msg-input');

  sendBtn.addEventListener('click', () => handleSend());
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  });
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 200) + 'px';
  });

  document.getElementById('new-conv-btn').addEventListener('click', handleNewConversation);

  const genImgBtn = document.getElementById('generate-image-btn');
  if (genImgBtn) {
    genImgBtn.addEventListener('click', () => handleGenerateImage());
  }

  // "Share session" button
  const shareBtn = document.getElementById('share-session-btn');
  if (shareBtn) {
    shareBtn.addEventListener('click', async () => {
      try {
        await copyShareUrl();
        showToast('Session URL copied — open it in another browser to share this session.');
      } catch (err) {
        showToast('Could not copy URL: ' + err.message);
      }
    });
  }

  // Track whether the user is scrolled to the bottom of the message list.
  // Streaming auto-scroll only fires when pinned, so users can scroll up to
  // read earlier messages without being yanked back on every token.
  const messageList = document.getElementById('message-list');
  messageList.addEventListener('scroll', () => {
    const { scrollTop, scrollHeight, clientHeight } = messageList;
    _pinnedToBottom = scrollHeight - scrollTop - clientHeight < 80;
  });

  // iOS Safari: the visual viewport shrinks when the keyboard appears but the
  // layout viewport does not, so the input area would be hidden under the
  // keyboard. Resizing #app to the visual viewport height keeps it visible.
  // We also scroll to bottom when pinned so new messages remain visible after
  // the keyboard opens.
  if (window.visualViewport) {
    const syncToVisualViewport = () => {
      const app = document.getElementById('app');
      const vv = window.visualViewport;
      app.style.height = `${vv.height}px`;
      app.style.transform = `translateY(${vv.offsetTop}px)`;
      scrollIfPinned();
    };
    window.visualViewport.addEventListener('resize', syncToVisualViewport);
    window.visualViewport.addEventListener('scroll', syncToVisualViewport);
  }

  // Health polling
  startHealthPolling();

  // Lightbox
  initLightbox();
}

// ── Lightbox ─────────────────────────────────────────────────────────────────

function initLightbox() {
  const lb = document.createElement('div');
  lb.id = 'lightbox';
  lb.setAttribute('role', 'dialog');
  lb.setAttribute('aria-modal', 'true');
  lb.setAttribute('aria-label', 'Image lightbox');

  const img = document.createElement('img');
  img.id = 'lightbox-img';
  img.alt = 'Full-size image';

  const closeBtn = document.createElement('button');
  closeBtn.id = 'lightbox-close';
  closeBtn.setAttribute('aria-label', 'Close lightbox');
  closeBtn.textContent = '✕';

  lb.appendChild(img);
  lb.appendChild(closeBtn);
  document.body.appendChild(lb);

  lb.addEventListener('click', e => {
    if (e.target === lb || e.target.id === 'lightbox-close') closeLightbox();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeLightbox();
  });
}

function openLightbox(src) {
  document.getElementById('lightbox-img').src = src;
  document.getElementById('lightbox').classList.add('open');
  document.body.style.overflow = 'hidden';
}

function closeLightbox() {
  const lb = document.getElementById('lightbox');
  if (lb) lb.classList.remove('open');
  document.body.style.overflow = '';
}

// ── Character selection ───────────────────────────────────────────────────────

export async function onCharacterSelected(character) {
  _activeCharacter = character;
  _activeConversationId = null;

  updateCharHeader(character);
  showChatUI();
  clearMessages();

  await loadConversations(character.id);
}

function updateCharHeader(character) {
  const header = document.getElementById('char-header');
  const avatar = header.querySelector('.char-avatar');
  const name = header.querySelector('.char-name');

  avatar.src = character.has_avatar
    ? character.avatar_url
    : 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><rect width="40" height="40" fill="%230f3460"/><text x="50%" y="55%" dominant-baseline="middle" text-anchor="middle" font-size="18" fill="%23e0e0e0">' + encodeURIComponent(character.name[0] || '?') + '</text></svg>';
  avatar.alt = character.name;
  name.textContent = character.name;
}

function showChatUI() {
  document.getElementById('empty-state').style.display = 'none';
  document.getElementById('char-header').style.display = 'flex';
  document.getElementById('message-list').style.display = 'flex';
  document.getElementById('input-area').style.display = 'flex';
}

// ── Conversations ─────────────────────────────────────────────────────────────

async function loadConversations(characterId) {
  let convs;
  try {
    convs = await fetchConversations(characterId);
  } catch (err) {
    showToast('Failed to load conversations: ' + err.message);
    return;
  }
  renderConvList(convs);

  if (convs.length > 0) {
    await selectConversation(convs[0].id);
  } else {
    await handleNewConversation();
  }
}

function renderConvList(convs) {
  const list = document.getElementById('conv-list');
  list.innerHTML = '';

  for (const conv of convs) {
    const li = document.createElement('li');
    li.className = 'conv-item';
    li.dataset.id = conv.id;
    if (conv.id === _activeConversationId) li.classList.add('active');
    li.setAttribute('role', 'button');
    li.setAttribute('tabindex', '0');

    const titleSpan = document.createElement('span');
    titleSpan.className = 'conv-title';
    titleSpan.textContent = conv.title;
    titleSpan.title = conv.title;

    const countSpan = document.createElement('span');
    countSpan.className = 'conv-count';
    countSpan.textContent = conv.message_count;

    const delBtn = document.createElement('button');
    delBtn.className = 'conv-delete';
    delBtn.textContent = '✕';
    delBtn.setAttribute('aria-label', 'Delete conversation');
    delBtn.addEventListener('click', async e => {
      e.stopPropagation();
      if (!confirm('Delete this conversation?')) return;
      try {
        await deleteConversation(conv.id);
        if (_activeConversationId === conv.id) {
          _activeConversationId = null;
          clearMessages();
        }
        await loadConversations(_activeCharacter.id);
      } catch (err) {
        showToast('Delete failed: ' + err.message);
      }
    });

    li.appendChild(titleSpan);
    li.appendChild(countSpan);
    li.appendChild(delBtn);

    li.addEventListener('click', () => selectConversation(conv.id));
    li.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') selectConversation(conv.id); });

    list.appendChild(li);
  }
}

async function selectConversation(id) {
  _activeConversationId = id;
  highlightActiveConv(id);
  clearMessages();

  // Subscribe this tab to remote events for the newly selected conversation
  subscribeRemoteEvents(id);

  // Close sidebar on small mobile screens after selecting a conversation
  if (window.innerWidth < 768) {
    document.dispatchEvent(new CustomEvent('closeSidebar'));
  }

  let conv;
  try {
    conv = await fetchConversation(id);
  } catch (err) {
    showToast('Failed to load conversation: ' + err.message);
    return;
  }

  if (
    conv.messages.length === 1
    && conv.messages[0].role === 'assistant'
    && typeof conv.messages[0].content === 'string'
    && conv.messages[0].content.trim() !== ''
  ) {
    await appendAssistantMessageProgressively(conv.messages[0].content, conv.messages[0].images || []);
  } else {
    for (const msg of conv.messages) {
      appendMessage(msg.role, msg.content, msg.images || []);
    }
  }
  scrollToBottom();
}

function highlightActiveConv(id) {
  document.querySelectorAll('#conv-list li').forEach(li => {
    li.classList.toggle('active', li.dataset.id === id);
  });
}

// ── Remote SSE (multi-browser) ────────────────────────────────────────────────

/**
 * Subscribe to the server-side event bus for the active conversation.
 * Events pushed by other browser tabs sharing the same session token are
 * received here and rendered when this tab is not the one that sent the
 * message (i.e. _streaming is false).
 */
function subscribeRemoteEvents(conversationId) {
  closeRemoteEvents();

  const token = getSessionToken();
  const url = `/api/chat/${encodeURIComponent(conversationId)}/events?session_token=${encodeURIComponent(token)}`;
  const es = new EventSource(url);
  _remoteEventSource = es;

  es.onmessage = e => {
    // Ignore events while this tab is already streaming from POST response
    if (_streaming) return;

    let event;
    try { event = JSON.parse(e.data); } catch (err) {
      console.error('[remote SSE] Failed to parse event:', e.data, err);
      return;
    }

    // Proactive messages announce themselves before the text exists: show the
    // typing indicator, and drop it again if the generation produced nothing.
    if (event.type === 'typing') {
      if (event.state === 'start') {
        if (!_remoteStreaming) _remoteStreaming = createStreamingMessage();
      } else if (_remoteStreaming) {
        _remoteStreaming.cancel();
        _remoteStreaming = null;
      }
      return;
    }

    if (event.type === 'token' || event.type === 'image_start') {
      if (!_remoteStreaming) {
        _remoteStreaming = createStreamingMessage();
      }
    }

    if (_remoteStreaming) {
      dispatchSSEEvent(event, _remoteStreaming);
      if (event.type === 'done' || event.type === 'error') {
        _remoteStreaming = null;
      }
    }
  };

  es.onerror = () => {
    // EventSource auto-reconnects on transient errors; nothing extra needed.
  };
}

function closeRemoteEvents() {
  if (_remoteEventSource) {
    _remoteEventSource.close();
    _remoteEventSource = null;
  }
  _remoteStreaming = null;
}

async function handleNewConversation() {
  if (!_activeCharacter) return;
  let conv;
  try {
    conv = await createConversation(_activeCharacter.id);
  } catch (err) {
    showToast('Failed to create conversation: ' + err.message);
    return;
  }
  // Refresh list and select the new one
  const convs = await fetchConversations(_activeCharacter.id);
  renderConvList(convs);
  await selectConversation(conv.id);
}

// ── Messages ─────────────────────────────────────────────────────────────────

function clearMessages() {
  document.getElementById('message-list').innerHTML = '';
}

/**
 * Create an image wrapper element with lightbox-click and copy-to-clipboard button.
 */
function createImageElement(url) {
  const wrapper = document.createElement('div');
  wrapper.className = 'img-wrapper';

  const img = document.createElement('img');
  img.className = 'msg-image';
  img.src = url;
  img.alt = 'Generated image';
  img.addEventListener('click', () => openLightbox(url));

  const copyBtn = document.createElement('button');
  copyBtn.className = 'img-copy-btn';
  copyBtn.setAttribute('aria-label', 'Copy image to clipboard');
  copyBtn.textContent = '⧉';
  copyBtn.addEventListener('click', e => {
    e.stopPropagation();
    copyImageToClipboard(url);
  });

  wrapper.appendChild(img);
  wrapper.appendChild(copyBtn);
  return wrapper;
}

async function copyImageToClipboard(url) {
  try {
    const res = await fetch(url);
    const blob = await res.blob();
    const supportedTypes = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];
    const type = supportedTypes.includes(blob.type) ? blob.type : 'image/png';
    await navigator.clipboard.write([new ClipboardItem({ [type]: blob })]);
    showToast('Image copied to clipboard');
  } catch (err) {
    showToast('Copy failed: ' + err.message);
  }
}

/**
 * Append a fully-formed message and return the bubble element.
 */
function appendMessage(role, content, images = []) {
  const list = document.getElementById('message-list');

  const wrapper = document.createElement('article');
  wrapper.className = `msg ${role}`;

  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  bubble.innerHTML = renderMarkdown(content);
  applyRoleplayHintStyling(bubble);
  wrapper.appendChild(bubble);

  if (images && images.length > 0) {
    const imgContainer = document.createElement('div');
    imgContainer.className = 'msg-images';
    for (const url of images) {
      imgContainer.appendChild(createImageElement(url));
    }
    wrapper.appendChild(imgContainer);
  }

  list.appendChild(wrapper);
  scrollToBottom();
  return bubble;
}

/**
 * Render a full assistant message progressively using the same streaming
 * renderer path as SSE responses.
 */
async function appendAssistantMessageProgressively(content, images = []) {
  const streaming = createStreamingMessage();
  const chunkSize = 4;
  const delayMs = 14;

  for (let i = 0; i < content.length; i += chunkSize) {
    streaming.onToken(content.slice(i, i + chunkSize));
    // Keep the same progressive feel as streamed responses for first_mes.
    await new Promise(resolve => setTimeout(resolve, delayMs));
  }

  for (const url of images) {
    const genId = `initial-${Math.random().toString(36).slice(2, 10)}`;
    streaming.onImageStart(genId, '');
    streaming.onImageComplete(genId, url);
  }

  streaming.finalize();
}

/**
 * Create a streaming assistant message bubble.
 * Returns { bubble, imagesContainer, finalize(content, images) }
 */
function createStreamingMessage() {
  const list = document.getElementById('message-list');

  // Typing indicator
  const typingEl = document.createElement('div');
  typingEl.className = 'msg assistant';
  const indicator = document.createElement('div');
  indicator.className = 'typing-indicator';
  indicator.innerHTML = '<div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div>';
  typingEl.appendChild(indicator);
  list.appendChild(typingEl);
  scrollIfPinned();

  const wrapper = document.createElement('article');
  wrapper.className = 'msg assistant';
  wrapper.style.display = 'none'; // hidden until first token

  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  bubble.style.display = 'none';
  wrapper.appendChild(bubble);

  const imagesContainer = document.createElement('div');
  imagesContainer.className = 'msg-images';
  wrapper.appendChild(imagesContainer);

  list.appendChild(wrapper);

  let rawText = '';
  const pendingImages = new Set();
  const imagePrompts = new Map(); // genId -> prompt mapping for retries

  return {
    onToken(token) {
      rawText += token;
      typingEl.remove();
      wrapper.style.display = '';
      bubble.style.display = '';
      bubble.innerHTML = renderMarkdown(rawText);
      applyRoleplayHintStyling(bubble);
      scrollIfPinned();
    },
    onImageStart(genId, prompt) {
      typingEl.remove();
      wrapper.style.display = '';
      pendingImages.add(genId);
      imagePrompts.set(genId, prompt);

      const ph = document.createElement('div');
      ph.className = 'img-placeholder';
      ph.id = `img-ph-${genId}`;
      ph.innerHTML = `
        <div class="spinner" id="img-spinner-${genId}"></div>
        <span class="img-placeholder-title">Generating image…</span>
        ${prompt ? `<div class="img-placeholder-prompt">${escapeHtml(prompt)}</div>` : ''}
        <div class="img-progress" id="img-progress-${genId}" style="display:none">
          <div class="img-progress-bar" id="img-progress-bar-${genId}"></div>
        </div>
        <span class="img-progress-label" id="img-progress-label-${genId}" style="display:none"></span>
      `;
      imagesContainer.appendChild(ph);
      scrollIfPinned();
    },
    onImageProgress(genId, step, total) {
      const spinner = document.getElementById(`img-spinner-${genId}`);
      const progressEl = document.getElementById(`img-progress-${genId}`);
      const barEl = document.getElementById(`img-progress-bar-${genId}`);
      const labelEl = document.getElementById(`img-progress-label-${genId}`);
      if (spinner) spinner.style.display = 'none';
      if (progressEl) progressEl.style.display = 'block';
      const pct = total > 0 ? Math.round((step / total) * 100) : 0;
      if (barEl) barEl.style.width = `${pct}%`;
      if (labelEl) { labelEl.style.display = 'block'; labelEl.textContent = `${step} / ${total}`; }
      scrollIfPinned();
    },
    onImageComplete(genId, imageUrl, prompt) {
      pendingImages.delete(genId);
      if (prompt) imagePrompts.set(genId, prompt);
      const ph = document.getElementById(`img-ph-${genId}`);
      if (ph) {
        ph.replaceWith(createImageElement(imageUrl));
      }
      scrollToBottom();
    },
    onImageFailed(genId, detail) {
      pendingImages.delete(genId);
      const ph = document.getElementById(`img-ph-${genId}`);
      const errEl = document.createElement('div');
      errEl.className = 'img-error';
      const prompt = imagePrompts.get(genId) || '';
      errEl.innerHTML = '⚠ ' + (detail || 'Image generation failed');

      const retryBtn = document.createElement('button');
      retryBtn.className = 'msg-retry-btn';
      retryBtn.textContent = 'Retry';
      retryBtn.style.marginLeft = '8px';
      retryBtn.addEventListener('click', async () => {
        retryBtn.disabled = true;
        retryBtn.textContent = 'Retrying…';
        try {
          await retryImageGeneration(_activeConversationId, prompt, genId, errEl, imagesContainer);
        } catch (err) {
          errEl.textContent = '⚠ Retry failed: ' + err.message;
          retryBtn.remove();
        }
      });
      errEl.appendChild(retryBtn);

      if (ph) {
        ph.replaceWith(errEl);
      } else {
        imagesContainer.appendChild(errEl);
      }
    },
    finalize() {
      typingEl.remove();
      wrapper.style.display = '';
      if (rawText) bubble.style.display = '';
    },
    /** Drop the bubble entirely — used when nothing was ever rendered in it. */
    cancel() {
      typingEl.remove();
      if (!rawText && pendingImages.size === 0 && !imagesContainer.hasChildNodes()) {
        wrapper.remove();
      } else {
        this.finalize();
      }
    },
  };
}

// ── Sending ──────────────────────────────────────────────────────────────────

async function handleGenerateImage() {
  if (_streaming || !_activeConversationId) return;

  setStreaming(true);
  const streaming = createStreamingMessage();

  let res;
  try {
    res = await generateSceneImage(_activeConversationId);
  } catch (err) {
    streaming.finalize();
    appendErrorMessage('Generate image failed: ' + err.message);
    setStreaming(false);
    return;
  }

  try {
    await readSSE(res, streaming);
  } catch (err) {
    streaming.finalize();
    appendErrorMessage('Stream error: ' + err.message);
  }

  setStreaming(false);
}

async function handleSend(contentOverride) {
  if (_streaming || !_activeConversationId) return;

  const input = document.getElementById('msg-input');
  const content = contentOverride !== undefined ? contentOverride : input.value.trim();
  if (!content) return;

  if (contentOverride === undefined) {
    input.value = '';
    input.style.height = 'auto';
  }

  _lastUserMessage = content;
  setStreaming(true);

  // Append user message only on initial send, not on retry
  if (contentOverride === undefined) {
    appendMessage('user', content);
  }

  const streaming = createStreamingMessage();

  let res;
  try {
    res = await sendMessage(_activeConversationId, content);
  } catch (err) {
    streaming.finalize();
    appendErrorMessage('Send failed: ' + err.message, () => handleSend(_lastUserMessage));
    setStreaming(false);
    return;
  }

  try {
    await readSSE(res, streaming);
  } catch (err) {
    streaming.finalize();
    appendErrorMessage('Stream error: ' + err.message, () => handleSend(_lastUserMessage));
  }

  setStreaming(false);
}

/** Read SSE frames from the fetch Response body and dispatch events. */
async function readSSE(response, handlers) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    // SSE frames are separated by \n\n; each frame is "data: <json>"
    const frames = buf.split('\n\n');
    buf = frames.pop(); // keep partial last frame

    for (const frame of frames) {
      const line = frame.trim();
      if (!line) continue;
      if (!line.startsWith('data:')) continue;
      const jsonStr = line.slice('data:'.length).trim();
      if (!jsonStr) continue;

      let event;
      try {
        event = JSON.parse(jsonStr);
      } catch (_) {
        continue;
      }

      dispatchSSEEvent(event, handlers);
    }
  }
}

function dispatchSSEEvent(event, handlers) {
  switch (event.type) {
    case 'token':
      handlers.onToken(event.content || '');
      break;
    case 'image_start':
      handlers.onImageStart(event.generation_id, event.prompt);
      break;
    case 'image_progress':
      handlers.onImageProgress(event.generation_id, event.step, event.total);
      break;
    case 'image_complete':
      handlers.onImageComplete(event.generation_id, event.image_url, event.prompt);
      break;
    case 'image_failed':
      handlers.onImageFailed(event.generation_id, event.detail);
      break;
    case 'done':
      handlers.finalize();
      break;
    case 'warning':
      appendWarningMessage(event.detail || 'Warning');
      break;
    case 'error':
      handlers.finalize();
      appendErrorMessage(event.detail || 'Unknown error', () => handleSend(_lastUserMessage));
      break;
  }
}

function appendErrorMessage(detail, onRetry) {
  const list = document.getElementById('message-list');
  const el = document.createElement('div');
  el.className = 'msg-error';

  const text = document.createElement('span');
  text.textContent = '⚠ ' + detail;
  el.appendChild(text);

  if (onRetry) {
    const retryBtn = document.createElement('button');
    retryBtn.className = 'msg-retry-btn';
    retryBtn.textContent = 'Retry';
    retryBtn.addEventListener('click', () => {
      el.remove();
      onRetry();
    });
    el.appendChild(retryBtn);
  }

  list.appendChild(el);
  scrollToBottom();
}

function appendWarningMessage(detail) {
  const list = document.getElementById('message-list');
  const el = document.createElement('div');
  el.className = 'msg-warning';
  const text = document.createElement('span');
  text.textContent = '⚠ ' + detail;
  el.appendChild(text);
  list.appendChild(el);
  scrollToBottom();
}

function setStreaming(streaming) {
  _streaming = streaming;
  document.getElementById('msg-input').disabled = streaming;
  document.getElementById('send-btn').disabled = streaming;
  const genImgBtn = document.getElementById('generate-image-btn');
  if (genImgBtn) genImgBtn.disabled = streaming;
  if (!streaming) {
    // Scroll to bottom before focusing to ensure the latest message is visible
    // even when the mobile keyboard opens and shrinks the viewport.
    scrollToBottom();
    document.getElementById('msg-input').focus();
  }
}

// ── Health polling ────────────────────────────────────────────────────────────

function startHealthPolling() {
  updateHealth();
  _healthInterval = setInterval(() => {
    if (!document.hidden) updateHealth();
  }, 30000);

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) updateHealth();
  });
}

async function updateHealth() {
  let data;
  try {
    data = await fetchHealth();
  } catch (_) {
    setStatusItem('text', null);
    setStatusItem('image', null);
    return;
  }
  const c = data.connectors || {};
  setStatusItem('text', c.text);
  setStatusItem('image', c.image);
  updateImageConnectorStatus(!!c.image);
}

function updateImageConnectorStatus(hasConnector) {
  _hasImageConnector = hasConnector;
  const btn = document.getElementById('generate-image-btn');
  if (btn) {
    btn.style.display = hasConnector ? '' : 'none';
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function renderMarkdown(text) {
  if (typeof marked !== 'undefined') {
    const raw = marked.parse(text, { breaks: true });
    return typeof DOMPurify !== 'undefined' ? DOMPurify.sanitize(raw) : raw;
  }
  // Fallback: escape HTML
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>');
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Highlight roleplay instruction segments wrapped in [ ... ] or { ... }
 * without altering code blocks or Markdown-rendered italic/bold elements
 * (the LLM uses *[stage direction]* which marked.js renders as <em>[...]</em>).
 */
function applyRoleplayHintStyling(rootEl) {
  const walker = document.createTreeWalker(rootEl, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  let node;
  while ((node = walker.nextNode())) {
    const parentTag = node.parentElement ? node.parentElement.tagName : '';
    if (parentTag === 'CODE' || parentTag === 'PRE' || parentTag === 'EM' || parentTag === 'STRONG') continue;
    textNodes.push(node);
  }

  for (const textNode of textNodes) {
    const text = textNode.nodeValue || '';
    if (!text.includes('[') && !text.includes('{')) continue;

    const fragments = splitRoleplayFragments(text);
    if (!fragments.some(f => f.type === 'hint')) continue;

    const frag = document.createDocumentFragment();
    for (const part of fragments) {
      if (part.type === 'hint') {
        const span = document.createElement('span');
        span.className = 'rp-hint';
        span.textContent = part.text;
        frag.appendChild(span);
      } else {
        frag.appendChild(document.createTextNode(part.text));
      }
    }
    textNode.parentNode.replaceChild(frag, textNode);
  }
}

function splitRoleplayFragments(text) {
  const out = [];
  let i = 0;

  while (i < text.length) {
    const ch = text[i];
    if (ch !== '[' && ch !== '{') {
      let j = i + 1;
      while (j < text.length && text[j] !== '[' && text[j] !== '{') j += 1;
      out.push({ type: 'text', text: text.slice(i, j) });
      i = j;
      continue;
    }

    const close = ch === '[' ? ']' : '}';
    const end = text.indexOf(close, i + 1);
    if (end === -1) {
      out.push({ type: 'text', text: text.slice(i) });
      break;
    }

    out.push({ type: 'hint', text: text.slice(i, end + 1) });
    i = end + 1;
  }

  return out;
}

function scrollToBottom() {
  const list = document.getElementById('message-list');
  list.scrollTop = list.scrollHeight;
  _pinnedToBottom = true;
}

function scrollIfPinned() {
  if (!_pinnedToBottom) return;
  const list = document.getElementById('message-list');
  list.scrollTop = list.scrollHeight;
}


async function retryImageGeneration(conversationId, prompt, genId, errEl, imagesContainer) {
  // Replace error with placeholder while retrying
  const ph = document.createElement('div');
  ph.className = 'img-placeholder';
  ph.id = `img-ph-${genId}`;
  ph.innerHTML = `
    <div class="spinner" id="img-spinner-${genId}"></div>
    <span class="img-placeholder-title">Retrying image…</span>
    ${prompt ? `<div class="img-placeholder-prompt">${escapeHtml(prompt)}</div>` : ''}
  `;
  errEl.replaceWith(ph);
  scrollToBottom();

  // Create a minimal handler just for image events
  const imageHandlers = {
    onImageStart: () => {},
    onImageProgress: (gId, step, total) => {
      if (gId === genId) {
        const spinner = document.getElementById(`img-spinner-${genId}`);
        const progressEl = document.createElement('div');
        progressEl.className = 'img-progress';
        progressEl.id = `img-progress-retry-${genId}`;
        progressEl.innerHTML = `
          <div class="img-progress-bar" id="img-progress-bar-retry-${genId}" style="width: 0%"></div>
        `;
        if (!spinner.nextElementSibling || !spinner.nextElementSibling.classList.contains('img-progress')) {
          if (spinner) spinner.after(progressEl);
        }
        const pct = total > 0 ? Math.round((step / total) * 100) : 0;
        const barEl = document.getElementById(`img-progress-bar-retry-${genId}`);
        if (barEl) barEl.style.width = `${pct}%`;
      }
    },
    onImageComplete: (gId, imageUrl) => {
      if (gId === genId) {
        const ph2 = document.getElementById(`img-ph-${genId}`);
        if (ph2) {
          ph2.replaceWith(createImageElement(imageUrl));
          scrollToBottom();
        }
      }
    },
    onImageFailed: (gId, detail) => {
      if (gId === genId) {
        const ph2 = document.getElementById(`img-ph-${genId}`);
        if (ph2) {
          const errEl2 = document.createElement('div');
          errEl2.className = 'img-error';
          errEl2.innerHTML = '⚠ ' + (detail || 'Unknown error');

          const retryBtn2 = document.createElement('button');
          retryBtn2.className = 'msg-retry-btn';
          retryBtn2.textContent = 'Retry';
          retryBtn2.style.marginLeft = '8px';
          retryBtn2.addEventListener('click', async () => {
            retryBtn2.disabled = true;
            retryBtn2.textContent = 'Retrying…';
            try {
              await retryImageGeneration(conversationId, prompt, genId, errEl2, imagesContainer);
            } catch (err) {
              errEl2.textContent = '⚠ Retry failed: ' + err.message;
              retryBtn2.remove();
            }
          });
          errEl2.appendChild(retryBtn2);

          ph2.replaceWith(errEl2);
          scrollToBottom();
        }
      }
    },
  };

  try {
    const res = await fetch(`/api/chat/${encodeURIComponent(conversationId)}/retry-image`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Session-Token': getSessionToken(),
      },
      body: JSON.stringify({ prompt, generation_id: genId }),
    });

    if (!res.ok) {
      throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    }

    await readSSE(res, imageHandlers);
  } catch (err) {
    const ph2 = document.getElementById(`img-ph-${genId}`);
    if (ph2) {
      const errEl2 = document.createElement('div');
      errEl2.className = 'img-error';
      errEl2.textContent = '⚠ ' + err.message;
      ph2.replaceWith(errEl2);
    }
    throw err;
  }
}

export function showToast(message, duration = 4000) {
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), duration);
}

export const __test = {
  createStreamingMessage,
  dispatchSSEEvent,
  scrollIfPinned,
  getPinned: () => _pinnedToBottom,
  setPinned: (v) => { _pinnedToBottom = v; },
  updateImageConnectorStatus,
  getHasImageConnector: () => _hasImageConnector,
  renderMarkdown,
  applyRoleplayHintStyling,
  splitRoleplayFragments,
};
