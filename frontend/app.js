/* ============================================================
   app.js — TrackFundAI bootstrap
   Runs on every page. Handles:
   - Auth guard
   - User badge display
   - Fund selector mount
   - Portfolio module init (on index.html only)
============================================================ */

document.addEventListener('DOMContentLoaded', () => {
  // Auth guard
  if (typeof Auth !== 'undefined' && !Auth.isLoggedIn()) {
    window.location.href = 'login.html';
    return;
  }

  // Display user badge
  const userBadge = document.getElementById('user-badge');
  if (userBadge && typeof Auth !== 'undefined') {
    const user = Auth.getUser() || {};
    userBadge.textContent =
      (user.first_name || user.username || '—') + ' · ' +
      (user.role || 'user').replace('_', ' ').toUpperCase();
  }

  // Logout button
  const logoutBtn = document.getElementById('btn-logout');
  if (logoutBtn && typeof Auth !== 'undefined') {
    logoutBtn.onclick = () => Auth.logout();
  }

  // Mount fund selector on every page that has the mount point
  if (typeof FundSelector !== 'undefined') {
    FundSelector.mount('fund-selector-mount');
  }

  // Init portfolio module (index.html only)
  if (window.Portfolio && typeof window.Portfolio.init === 'function') {
    window.Portfolio.init();
  }
});
