/**
 * api.js — Thin wrappers around fetch for all aubergeRP REST endpoints.
 * All functions return the parsed JSON (or throw on HTTP errors).
 *
 * Session token management
 * ─────────────────────────
 * Each browser tab persists a UUID in localStorage under the key
 * "auberge_session_token".  If the URL contains ?token=<uuid> (put there by
 * the "Share session" feature), that value is installed into localStorage and
 * the query-string is removed from the address bar before the app boots.
 *
 * The token is sent as the X-Session-Token header with every request so that
 * the server can scope conversations and image storage per user.
 */

// ── Session token ────────────────────────────────────────────────────────────

const _TOKEN_KEY = 'auberge_session_token';

function _uuid() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  // Fallback for non-secure contexts (self-signed certs, older mobile browsers)
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

function _initSessionToken() {
  // Check if the URL carries a shared token.
  // SECURITY NOTE: The ?token= parameter is intentionally used for the
  // "Share session" feature so users can open a shared session in another
  // browser.  The token is written to localStorage immediately and the
  // query-string is removed from the address bar via history.replaceState()
  // BEFORE any network request is made.  This prevents the token from
  // leaking into server access logs or the Referer header of subsequent
  // navigations.  Do not re-add the token to any URLs after this point.
  const params = new URLSearchParams(window.location.search);
  const urlToken = params.get('token');
  if (urlToken) {
    localStorage.setItem(_TOKEN_KEY, urlToken);
    // Remove ?token=… from the address bar without reloading
    params.delete('token');
    const clean = params.toString()
      ? `${window.location.pathname}?${params}`
      : window.location.pathname;
    history.replaceState(null, '', clean);
  }

  // Create a new token if none exists
  if (!localStorage.getItem(_TOKEN_KEY)) {
    localStorage.setItem(_TOKEN_KEY, _uuid());
  }
}

_initSessionToken();

export function getSessionToken() {
  return localStorage.getItem(_TOKEN_KEY) || '';
}

/**
 * Copy a shareable URL (with ?token=…) to the clipboard.
 * Returns a Promise that resolves when the copy succeeds.
 */
export function copyShareUrl() {
  const url = `${window.location.origin}${window.location.pathname}?token=${getSessionToken()}`;
  return navigator.clipboard.writeText(url);
}

// ── Core fetch helper ────────────────────────────────────────────────────────

async function apiFetch(path, options = {}) {
  const headers = {
    'X-Session-Token': getSessionToken(),
    ...(options.headers || {}),
  };
  const res = await fetch(path, { ...options, headers });
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

// ── Health ──────────────────────────────────────────────────────────────────

export function fetchHealth() {
  return apiFetch('/api/health/');
}

// ── Characters ──────────────────────────────────────────────────────────────

export function fetchCharacters() {
  return apiFetch('/api/characters/');
}

export function fetchCharacter(id) {
  return apiFetch(`/api/characters/${id}`);
}

// ── Conversations ────────────────────────────────────────────────────────────

export function fetchConversations(characterId) {
  const qs = characterId ? `?character_id=${encodeURIComponent(characterId)}` : '';
  return apiFetch(`/api/conversations/${qs}`);
}

export function fetchConversation(id) {
  return apiFetch(`/api/conversations/${id}`);
}

export function createConversation(characterId) {
  return apiFetch('/api/conversations/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ character_id: characterId }),
  });
}

export function deleteConversation(id) {
  return apiFetch(`/api/conversations/${id}`, { method: 'DELETE' });
}

// ── Chat (returns a raw Response for SSE streaming) ──────────────────────────

export async function sendMessage(conversationId, content) {
  const res = await fetch(`/api/chat/${conversationId}/message`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Session-Token': getSessionToken(),
    },
    body: JSON.stringify({ content }),
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body.detail) detail = body.detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return res; // caller reads .body
}

export async function generateSceneImage(conversationId) {
  const res = await fetch(`/api/chat/${encodeURIComponent(conversationId)}/generate-image`, {
    method: 'POST',
    headers: {
      'X-Session-Token': getSessionToken(),
    },
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body.detail) detail = body.detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return res; // caller reads .body as SSE stream
}

// ── Timezone ─────────────────────────────────────────────────────────────────

export function fetchTimezone() {
  return apiFetch('/api/timezone/');
}

export function updateTimezone(timezone) {
  return apiFetch('/api/timezone/', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ timezone }),
  });
}
