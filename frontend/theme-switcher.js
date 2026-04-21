/* ============================================================
   theme-switcher.js
   Global theme toggle for TrackFundAI.
   Persists preference to localStorage.
   Auto-injects toggle button into the navbar on every page.
============================================================ */

(() => {
  const STORAGE_KEY = 'tfai_theme';

  // Apply saved theme immediately (before DOM renders) to avoid flash
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
  }

  function getTheme() {
    return document.documentElement.getAttribute('data-theme') || 'dark';
  }

  function setTheme(theme) {
    if (theme === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
    localStorage.setItem(STORAGE_KEY, theme);
  }

  function toggleTheme() {
    setTheme(getTheme() === 'dark' ? 'light' : 'dark');
  }

  function injectToggle() {
    const navActions = document.querySelector('.nav-actions');
    if (!navActions) return;

    // Don't inject twice
    if (navActions.querySelector('.theme-toggle')) return;

    const btn = document.createElement('button');
    btn.className = 'theme-toggle';
    btn.title = 'Toggle light/dark theme';
    btn.setAttribute('aria-label', 'Toggle theme');

    const knob = document.createElement('span');
    knob.className = 'theme-toggle-knob';
    btn.appendChild(knob);

    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleTheme();
    });

    // Insert before the first child (before notification badge)
    navActions.insertBefore(btn, navActions.firstChild);
  }

  // Inject when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectToggle);
  } else {
    injectToggle();
  }

  // Expose for programmatic use
  window.ThemeSwitcher = { getTheme, setTheme, toggleTheme };
})();
