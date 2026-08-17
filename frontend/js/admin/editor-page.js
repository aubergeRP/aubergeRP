/**
 * admin/editor-page.js — full-page editors for the Admin UI.
 *
 * Edit forms used to be overlay dialogs closing on a backdrop click, so a
 * misclick threw away everything that had been typed. They are now real pages:
 * opening one hides the section list and shows the editor in the content area,
 * and the only ways out are Back, Cancel or Save.
 *
 * The editors live inside <main id="admin-content"> next to the sections, so
 * showing one is just a matter of swapping which block is displayed.
 */

const contentEl = document.getElementById('admin-content');

/** Editor currently displayed, if any. */
let activeEditor = null;

/** Show *el* as a full page, hiding the section behind it. */
export function openEditorPage(el) {
  if (!el) return;
  if (activeEditor && activeEditor !== el) closeEditorPage(activeEditor);

  // Remember what was on screen so closing restores exactly that.
  el._restoreSections = [];
  contentEl?.querySelectorAll(':scope > .admin-section').forEach((section) => {
    if (section.style.display !== 'none') {
      el._restoreSections.push(section);
      section.style.display = 'none';
    }
  });

  el.style.display = 'block';
  document.body.classList.add('editor-page-open');
  activeEditor = el;

  // A page starts at the top, and the first field is where editing begins.
  window.scrollTo({ top: 0 });
  el.querySelector('.editor-body input, .editor-body select, .editor-body textarea')?.focus();
}

/** Hide *el* and bring the section that was visible back. */
export function closeEditorPage(el) {
  if (!el) return;
  el.style.display = 'none';
  (el._restoreSections || []).forEach((section) => { section.style.display = ''; });
  el._restoreSections = [];
  if (activeEditor === el) {
    activeEditor = null;
    document.body.classList.remove('editor-page-open');
  }
}

/** True while any editor page is open (used to keep nav switching coherent). */
export function isEditorPageOpen() {
  return activeEditor !== null;
}

/** Close whatever editor is open — used when the user navigates elsewhere. */
export function closeActiveEditorPage() {
  if (activeEditor) closeEditorPage(activeEditor);
}
