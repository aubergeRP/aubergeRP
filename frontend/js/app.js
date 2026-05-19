import { applyGuiCustomization } from '/js/gui-customization.js';
import { initStatusBar, initHeaderLogo } from '/js/layout.js';
import { initCharacters, loadCharacters, setSelectedCharacter } from '/js/characters.js';
import { initChat, onCharacterSelected, showToast } from '/js/chat.js';

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
