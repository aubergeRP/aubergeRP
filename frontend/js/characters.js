/**
 * characters.js — Character list management in the sidebar.
 */
import { fetchCharacters } from './api.js';

let _characters = [];
let _selectedId = null;
let _onSelect = () => {};

/**
 * @param {(character: object) => void} onSelect  Called when a character is clicked.
 */
export function initCharacters(onSelect) {
  _onSelect = onSelect;
}

export async function loadCharacters() {
  _characters = await fetchCharacters();
  renderCharacters();
  return _characters;
}

/**
 * Inject a character that is not part of the public list (unlisted chat opened
 * via /?character=<id>) so the sidebar and header can display it.
 * @param {object} card  A CharacterCard as returned by GET /api/characters/{id}
 * @returns {object} the summary-shaped entry
 */
export function addStandaloneCharacter(card) {
  const existing = _characters.find(c => c.id === card.id);
  if (existing) return existing;
  const summary = {
    id: card.id,
    name: card.data.name,
    description: card.data.description || '',
    avatar_url: `/api/characters/${card.id}/avatar`,
    has_avatar: !!card.has_avatar,
    tags: card.data.tags || [],
  };
  _characters.push(summary);
  renderCharacters();
  return summary;
}

export function getCharacter(id) {
  return _characters.find(c => c.id === id) || null;
}

export function setSelectedCharacter(id) {
  _selectedId = id;
  renderCharacters();
}

function renderCharacters() {
  const list = document.getElementById('char-list');
  if (!list) return;

  list.innerHTML = '';

  if (_characters.length === 0) {
    const li = document.createElement('li');
    li.style.color = 'var(--color-text-muted)';
    li.style.fontSize = '0.82rem';
    li.style.padding = '0.75rem 1rem';
    li.textContent = 'No character available.';
    li.style.cursor = 'default';
    list.appendChild(li);
    return;
  }

  for (const char of _characters) {
    const li = document.createElement('li');
    li.dataset.id = char.id;
    if (char.id === _selectedId) li.classList.add('active');
    li.setAttribute('role', 'button');
    li.setAttribute('tabindex', '0');
    li.setAttribute('aria-label', `Select character ${char.name}`);

    const img = document.createElement('img');
    img.className = 'avatar-thumb';
    img.src = char.has_avatar
      ? char.avatar_url
      : 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><rect width="40" height="40" fill="%230f3460"/><text x="50%" y="55%" dominant-baseline="middle" text-anchor="middle" font-size="18" fill="%23e0e0e0">' + encodeURIComponent(char.name[0] || '?') + '</text></svg>';
    img.alt = char.name;

    const span = document.createElement('span');
    span.className = 'char-name';
    span.textContent = char.name;

    li.appendChild(img);
    li.appendChild(span);

    li.addEventListener('click', () => selectChar(char.id));
    li.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') selectChar(char.id); });

    list.appendChild(li);
  }
}

function selectChar(id) {
  _selectedId = id;
  renderCharacters();
  const char = _characters.find(c => c.id === id);
  if (char) _onSelect(char);
}
