/* ============================================================
   app.js — bootstrap
============================================================ */

document.addEventListener('DOMContentLoaded', () => {
  if (window.Portfolio && typeof window.Portfolio.init === 'function') {
    window.Portfolio.init();
  } else {
    console.error('Portfolio module not loaded.');
  }
});
