/* ============================================================
   notification-drawer.js
   macOS Tahoe-style notification drawer — shared across all pages.
   Slides in from the right on badge click, fetches real data.
============================================================ */

(() => {
  const CATEGORY_ICONS = {
    fund:         '&#9670;',   // diamond
    document:     '&#9783;',   // doc
    capital_call: '&#10548;',  // arrow up
    distribution: '&#10549;',  // arrow down
    compliance:   '&#9888;',   // warning
    kpi:          '&#9733;',   // star
    system:       '&#9679;',   // circle
  };

  let notifications = [];
  let activeFilter = 'all';
  let isOpen = false;

  // ── Time-ago formatter ──────────────────────────────────
  function fmtTimeAgo(iso) {
    const d = new Date(iso);
    const now = new Date();
    const sec = Math.floor((now - d) / 1000);
    if (sec < 60)    return 'just now';
    if (sec < 3600)  return `${Math.floor(sec / 60)}m ago`;
    if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
    if (sec < 604800) return `${Math.floor(sec / 86400)}d ago`;
    return d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
  }

  // ── Escape HTML ─────────────────────────────────────────
  function esc(s) {
    if (s === null || s === undefined) return '';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  }

  // ── Inject drawer HTML into DOM ─────────────────────────
  function injectDrawer() {
    // Backdrop
    const backdrop = document.createElement('div');
    backdrop.className = 'notif-drawer-backdrop';
    backdrop.id = 'notif-drawer-backdrop';
    document.body.appendChild(backdrop);

    // Drawer panel
    const drawer = document.createElement('div');
    drawer.className = 'notif-drawer';
    drawer.id = 'notif-drawer';
    drawer.innerHTML = `
      <div class="notif-drawer-header">
        <div style="display:flex;align-items:center;">
          <span class="notif-drawer-title">Notifications</span>
          <span class="notif-drawer-count" id="notif-drawer-count">0</span>
        </div>
        <div class="notif-drawer-actions">
          <button class="notif-drawer-mark-all" id="notif-drawer-mark-all">Mark all read</button>
          <button class="notif-drawer-close" id="notif-drawer-close">&times;</button>
        </div>
      </div>
      <div class="notif-drawer-filters" id="notif-drawer-filters">
        <button class="notif-filter-btn active" data-filter="all">All</button>
        <button class="notif-filter-btn" data-filter="fund">Funds</button>
        <button class="notif-filter-btn" data-filter="document">Documents</button>
        <button class="notif-filter-btn" data-filter="capital_call">Capital Calls</button>
        <button class="notif-filter-btn" data-filter="compliance">Compliance</button>
        <button class="notif-filter-btn" data-filter="kpi">KPI</button>
        <button class="notif-filter-btn" data-filter="system">System</button>
      </div>
      <div class="notif-drawer-list" id="notif-drawer-list">
        <div class="notif-drawer-loading"><div class="notif-drawer-spinner"></div></div>
      </div>
    `;
    document.body.appendChild(drawer);

    // Event listeners
    backdrop.addEventListener('click', close);
    document.getElementById('notif-drawer-close').addEventListener('click', close);
    document.getElementById('notif-drawer-mark-all').addEventListener('click', markAllRead);

    // Filter tabs
    document.getElementById('notif-drawer-filters').addEventListener('click', (e) => {
      const btn = e.target.closest('.notif-filter-btn');
      if (!btn) return;
      activeFilter = btn.dataset.filter;
      document.querySelectorAll('.notif-filter-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.filter === activeFilter)
      );
      renderList();
    });

    // Escape key
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && isOpen) close();
    });
  }

  // ── Open / Close ────────────────────────────────────────
  function open() {
    isOpen = true;
    document.getElementById('notif-drawer').classList.add('open');
    document.getElementById('notif-drawer-backdrop').classList.add('open');
    document.body.style.overflow = 'hidden';
    fetchNotifications();
  }

  function close() {
    isOpen = false;
    document.getElementById('notif-drawer').classList.remove('open');
    document.getElementById('notif-drawer-backdrop').classList.remove('open');
    document.body.style.overflow = '';
  }

  function toggle() {
    isOpen ? close() : open();
  }

  // ── Fetch notifications from API ────────────────────────
  async function fetchNotifications() {
    const list = document.getElementById('notif-drawer-list');
    list.innerHTML = '<div class="notif-drawer-loading"><div class="notif-drawer-spinner"></div></div>';

    try {
      notifications = await Auth.apiGet('/notifications/');
      renderList();
      updateBadge();
    } catch (e) {
      console.error('Failed to fetch notifications:', e);
      list.innerHTML = `
        <div class="notif-drawer-empty">
          <div class="notif-drawer-empty-icon">&#9888;</div>
          <div class="notif-drawer-empty-text">Failed to load notifications</div>
        </div>`;
    }
  }

  // ── Render notification list ────────────────────────────
  function renderList() {
    const list = document.getElementById('notif-drawer-list');
    list.innerHTML = '';

    const filtered = activeFilter === 'all'
      ? notifications
      : notifications.filter(n => n.category === activeFilter);

    // Update unread count in drawer header
    const unread = notifications.filter(n => !n.is_read).length;
    document.getElementById('notif-drawer-count').textContent = unread;

    // Show/hide mark all button
    const markAllBtn = document.getElementById('notif-drawer-mark-all');
    markAllBtn.style.display = unread > 0 ? '' : 'none';

    if (filtered.length === 0) {
      list.innerHTML = `
        <div class="notif-drawer-empty">
          <div class="notif-drawer-empty-icon">&#128276;</div>
          <div class="notif-drawer-empty-text">
            ${activeFilter === 'all' ? 'No notifications yet' : 'No ' + activeFilter.replace('_', ' ') + ' notifications'}
          </div>
        </div>`;
      return;
    }

    filtered.forEach(n => {
      const item = document.createElement('div');
      item.className = `notif-drawer-item ${n.is_read ? '' : 'unread'}`;
      item.dataset.id = n.id;

      const priorityHtml = (n.priority === 'high' || n.priority === 'urgent')
        ? `<span class="notif-drawer-item-priority ${n.priority}">${esc(n.priority)}</span>`
        : '';

      item.innerHTML = `
        <div class="notif-drawer-icon ${n.category}">${CATEGORY_ICONS[n.category] || '&#9679;'}</div>
        <div class="notif-drawer-content">
          <div class="notif-drawer-item-title">${esc(n.title)}</div>
          <div class="notif-drawer-item-message">${esc(n.message)}</div>
          <div class="notif-drawer-item-meta">
            <span class="notif-drawer-item-time">${fmtTimeAgo(n.created_at)}</span>
            ${priorityHtml}
          </div>
        </div>
        ${!n.is_read ? '<button class="notif-drawer-item-read" title="Mark as read">&#10003;</button>' : ''}
      `;

      // Mark individual as read
      const readBtn = item.querySelector('.notif-drawer-item-read');
      if (readBtn) {
        readBtn.addEventListener('click', async (e) => {
          e.stopPropagation();
          await markOneRead(n.id);
        });
      }

      list.appendChild(item);
    });
  }

  // ── Mark single notification read ───────────────────────
  async function markOneRead(id) {
    try {
      await Auth.apiPut(`/notifications/${id}/read/`, {});
      const n = notifications.find(x => x.id === id);
      if (n) n.is_read = true;
      renderList();
      updateBadge();
    } catch (e) {
      console.error('Failed to mark notification read:', e);
    }
  }

  // ── Mark all read ───────────────────────────────────────
  async function markAllRead() {
    try {
      await Auth.apiPost('/notifications/mark-all-read/', {});
      notifications.forEach(n => n.is_read = true);
      renderList();
      updateBadge();
    } catch (e) {
      console.error('Failed to mark all read:', e);
    }
  }

  // ── Update the navbar badge ─────────────────────────────
  function updateBadge() {
    const unread = notifications.filter(n => !n.is_read).length;
    const badge = document.getElementById('notif-badge');
    if (badge) {
      badge.textContent = unread;
      badge.classList.toggle('zero', !unread);
    }
  }

  // ── Wire badge click ────────────────────────────────────
  function bindBadge() {
    const badge = document.getElementById('notif-badge');
    if (badge) {
      badge.addEventListener('click', toggle);
    }
  }

  // ── Initialize ──────────────────────────────────────────
  function init() {
    injectDrawer();
    bindBadge();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Public API
  window.NotifDrawer = { open, close, toggle, fetchNotifications };
})();
