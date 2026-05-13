/**
 * toast.js — Contextual toast notification system for TrackFundAI.
 *
 * Usage:
 *   Toast.show('Operation successful', 'success');
 *   Toast.show('Upload failed — invalid format', 'error');
 *   Toast.show('Processing import...', 'info', 0);   // persistent (duration=0)
 *   Toast.show('NAV computed', 'success', 4000);
 *
 * Types: 'success' | 'error' | 'warning' | 'info'
 */

const Toast = (() => {
  const CONTAINER_ID = 'tfai-toast-container';
  const DEFAULT_DURATION = 4000;

  const ICONS = {
    success: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>`,
    error:   `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`,
    warning: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`,
    info:    `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`,
  };

  function _getContainer() {
    let el = document.getElementById(CONTAINER_ID);
    if (!el) {
      el = document.createElement('div');
      el.id = CONTAINER_ID;
      document.body.appendChild(el);
    }
    return el;
  }

  function show(message, type = 'info', duration = DEFAULT_DURATION) {
    const container = _getContainer();

    const toast = document.createElement('div');
    toast.className = `tfai-toast tfai-toast--${type}`;
    toast.innerHTML = `
      <span class="tfai-toast__icon">${ICONS[type] || ICONS.info}</span>
      <span class="tfai-toast__message">${message}</span>
      <button class="tfai-toast__close" aria-label="Dismiss">&times;</button>
    `;

    const closeBtn = toast.querySelector('.tfai-toast__close');
    closeBtn.addEventListener('click', () => _dismiss(toast));

    container.appendChild(toast);

    // Animate in
    requestAnimationFrame(() => toast.classList.add('tfai-toast--visible'));

    if (duration > 0) {
      setTimeout(() => _dismiss(toast), duration);
    }

    return toast;
  }

  function _dismiss(toast) {
    toast.classList.remove('tfai-toast--visible');
    toast.classList.add('tfai-toast--exit');
    setTimeout(() => toast.remove(), 350);
  }

  // Convenience wrappers
  const success = (msg, dur) => show(msg, 'success', dur);
  const error   = (msg, dur) => show(msg, 'error',   dur);
  const warning = (msg, dur) => show(msg, 'warning', dur);
  const info    = (msg, dur) => show(msg, 'info',    dur);

  return { show, success, error, warning, info };
})();

window.Toast = Toast;
