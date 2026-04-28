/* ============================================================
   app.js — bootstrap
============================================================ */

document.addEventListener('DOMContentLoaded', () => {
  // Require login — redirect to login page if not authenticated
  if (!Auth.requireAuth()) return;

  if (window.Portfolio && typeof window.Portfolio.init === 'function') {
    window.Portfolio.init();
  } else {
    console.error('Portfolio module not loaded.');
  }
});
