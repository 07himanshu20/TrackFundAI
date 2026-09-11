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
    // Global theme toggle button removed from the navbar (product decision, 2026-09).
    // The saved-theme application above is intentionally kept so any previously chosen
    // preference still renders; only the visible toggle button is no longer injected.
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
