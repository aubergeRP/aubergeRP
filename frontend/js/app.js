import { applyGuiCustomization } from '/js/gui-customization.js';
import { initStatusBar, initHeaderLogo } from '/js/layout.js';
import { initCharacters, loadCharacters, setSelectedCharacter } from '/js/characters.js';
import { initChat, onCharacterSelected, showToast } from '/js/chat.js';
import { fetchTimezone, updateTimezone } from '/js/api.js';

initStatusBar();
initHeaderLogo();

// Hamburger toggle (responsive sidebar)
const hamburgerBtn = document.getElementById('hamburger-btn');
const sidebar = document.getElementById('sidebar');
const overlay = document.getElementById('sidebar-overlay');

function toggleSidebar(force) {
  const open = force !== undefined ? force : !sidebar.classList.contains('open');
  sidebar.classList.toggle('open', open);
  hamburgerBtn.setAttribute('aria-expanded', String(open));
}

hamburgerBtn.addEventListener('click', () => toggleSidebar());
overlay.addEventListener('click', () => toggleSidebar(false));
document.addEventListener('closeSidebar', () => toggleSidebar(false));

// Close sidebar on mobile when a character is clicked
function onCharSelected(character) {
  if (window.innerWidth < 1024) toggleSidebar(false);
  setSelectedCharacter(character.id);
  localStorage.setItem('auberge_last_character_id', character.id);
  onCharacterSelected(character).catch(err => showToast(err.message));
}

// Fullscreen toggle
const fullscreenBtn = document.getElementById('fullscreen-btn');
fullscreenBtn.addEventListener('click', () => {
  if (!document.fullscreenElement) {
    document.documentElement.requestFullscreen();
  } else {
    document.exitFullscreen();
  }
});
document.addEventListener('fullscreenchange', () => {
  const isFs = !!document.fullscreenElement;
  fullscreenBtn.textContent = isFs ? '⊡' : '⛶';
  fullscreenBtn.setAttribute('aria-label', isFs ? 'Exit fullscreen' : 'Toggle fullscreen');
});

// Init
applyGuiCustomization();
initCharacters(onCharSelected);
initChat();

loadCharacters().then(characters => {
  if (!characters || characters.length === 0) return;
  const lastId = localStorage.getItem('auberge_last_character_id');
  const toSelect = (lastId && characters.find(c => c.id === lastId)) || characters[0];
  onCharSelected(toSelect);
}).catch(err => showToast('Failed to load characters: ' + err.message));

// ── Timezone detection ────────────────────────────────────────────────────────
// Detect the browser IANA timezone, compare with stored value, and persist if
// different.  Failures are silently ignored so they never break the UI.
const _TZ_STORAGE_KEY = 'auberge_last_tz';

function _detectBrowserTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch (_) {
    return null;
  }
}

async function _syncTimezone() {
  const detected = _detectBrowserTimezone();
  if (!detected) return;

  try {
    const current = await fetchTimezone();
    if (current.timezone !== detected) {
      await updateTimezone(detected);
      // Record the timezone we just sent so we don't re-send on every page load
      // unless the browser timezone actually changes.
      localStorage.setItem(_TZ_STORAGE_KEY, detected);
    } else if (!localStorage.getItem(_TZ_STORAGE_KEY)) {
      // Server already has the right timezone; cache it locally.
      localStorage.setItem(_TZ_STORAGE_KEY, detected);
    }
  } catch (_) {
    // Network errors or invalid timezone must not break the app.
  }
}

_syncTimezone();
