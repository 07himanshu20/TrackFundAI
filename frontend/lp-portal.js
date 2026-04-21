/* ============================================================
   lp-portal.js
   TrackFundAI — LP (Limited Partner) Portal
   Read-only view for LP users: funds overview, documents, notifications.
============================================================ */

(() => {
  let funds = [];
  let documents = [];
  let notifications = [];

  const esc = (s) => {
    if (s === null || s === undefined) return '—';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  };

  const fmtCurrency = (v) => {
    if (!v) return '—';
    const n = parseFloat(v);
    if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
    if (n >= 1e7) return `${(n / 1e7).toFixed(1)}Cr`;
    if (n >= 1e5) return `${(n / 1e5).toFixed(1)}L`;
    return n.toLocaleString('en-IN');
  };

  const fmtSize = (bytes) => {
    if (!bytes) return '—';
    if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(1)} MB`;
    if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${bytes} B`;
  };

  const fmtDate = (iso) => {
    if (!iso) return '—';
    const d = new Date(iso);
    return d.toLocaleDateString('en-IN', {day: '2-digit', month: 'short', year: 'numeric'});
  };

  const fmtTimeAgo = (iso) => {
    if (!iso) return '';
    const diff = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    if (days < 7) return `${days}d ago`;
    return fmtDate(iso);
  };

  const CATEGORY_ICONS = {
    fund: '&#9670;',
    document: '&#9776;',
    capital_call: '&#9650;',
    distribution: '&#9660;',
    compliance: '&#9888;',
    kpi: '&#9733;',
    system: '&#9881;',
  };

  // ── Init ──────────────────────────────────────────────────
  async function init() {
    if (!Auth.requireAuth()) return;

    const user = Auth.getUser();
    document.getElementById('user-badge').textContent =
      `${user.first_name || user.username} · ${user.role.replace('_', ' ').toUpperCase()}`;
    document.getElementById('org-label').textContent =
      user.organization_name || 'Unknown Organization';
    document.getElementById('lp-name').textContent =
      user.first_name || user.username;

    document.getElementById('btn-logout').onclick = () => Auth.logout();

    // Load everything in parallel
    await Promise.all([
      loadFunds(),
      loadDocuments(),
      loadNotifications(),
    ]);

    renderStats();
    loadNotifCount();
  }

  // ── Notification badge ────────────────────────────────────
  async function loadNotifCount() {
    try {
      const data = await Auth.apiGet('/notifications/unread-count/');
      const badge = document.getElementById('notif-badge');
      badge.textContent = data.unread_count || 0;
      badge.classList.toggle('zero', !data.unread_count);
    } catch (e) {
      console.error('Failed to load notification count:', e);
    }
  }

  // ── Stats ─────────────────────────────────────────────────
  function renderStats() {
    const bar = document.getElementById('stats-bar');
    bar.innerHTML = '';
    const unread = notifications.filter(n => !n.is_read).length;
    const chips = [
      ['Funds', funds.length],
      ['Documents', documents.length],
      ['Unread Alerts', unread],
    ];
    chips.forEach(([label, value]) => {
      const div = document.createElement('div');
      div.className = 'hero-stat';
      div.innerHTML = `<span class="hero-stat-label">${label}</span><span class="hero-stat-value mono">${value}</span>`;
      bar.appendChild(div);
    });
  }

  // ── Funds ─────────────────────────────────────────────────
  async function loadFunds() {
    try {
      funds = await Auth.apiGet('/funds/');
      renderFundGrid();
    } catch (e) {
      console.error('Failed to load funds:', e);
    }
  }

  function renderFundGrid() {
    const grid = document.getElementById('fund-grid');
    grid.innerHTML = '';

    if (funds.length === 0) {
      grid.innerHTML = '<p style="color: var(--text-muted); padding: 40px; text-align: center;">No funds available.</p>';
      return;
    }

    funds.forEach(f => {
      const card = document.createElement('div');
      card.className = 'lp-fund-card';
      card.innerHTML = `
        <div class="lp-fund-name">${esc(f.name)}</div>
        <div class="lp-fund-sebi">${f.sebi_registration_number || 'No SEBI registration'}</div>
        <div class="lp-fund-metrics">
          <div class="lp-fund-metric">
            <span class="label">Category</span>
            <span class="value">${f.category_display}</span>
          </div>
          <div class="lp-fund-metric">
            <span class="label">Structure</span>
            <span class="value">${f.structure_display}</span>
          </div>
          <div class="lp-fund-metric">
            <span class="label">Corpus</span>
            <span class="value">${f.base_currency} ${fmtCurrency(f.corpus_target)}</span>
          </div>
        </div>
      `;
      grid.appendChild(card);
    });
  }

  // ── Documents ─────────────────────────────────────────────
  async function loadDocuments() {
    try {
      documents = await Auth.apiGet('/documents/?visibility=lp_visible');
      renderDocTable();
    } catch (e) {
      console.error('Failed to load documents:', e);
    }
  }

  function renderDocTable() {
    const tbody = document.getElementById('lp-doc-tbody');
    tbody.innerHTML = '';

    if (documents.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="doc-empty">No documents shared with you yet.</td></tr>';
      return;
    }

    documents.forEach(doc => {
      const fundName = doc.fund ? (funds.find(f => f.id === doc.fund)?.name || '—') : '—';
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>
          <div class="doc-title">${esc(doc.title)}</div>
          <div class="doc-filename">${esc(doc.file_name)}</div>
        </td>
        <td><span class="doc-category-badge">${esc(doc.category_display)}</span></td>
        <td>${esc(fundName)}</td>
        <td style="font-family: var(--font-mono); font-size: 12px;">${fmtSize(doc.file_size)}</td>
        <td style="font-size: 12px;">${fmtDate(doc.created_at)}</td>
        <td>
          <button class="doc-action-btn" data-id="${doc.id}">Download</button>
        </td>
      `;
      tbody.appendChild(tr);
    });

    // Bind download buttons
    tbody.querySelectorAll('.doc-action-btn').forEach(btn => {
      btn.onclick = () => downloadDoc(btn.dataset.id);
    });
  }

  async function downloadDoc(docId) {
    try {
      const token = Auth.getToken();
      const base = window.location.port === '8000' || window.location.port === '' ? '' : 'http://127.0.0.1:8000';
      const r = await fetch(`${base}/api/documents/${docId}/download/`, {
        headers: {'Authorization': `Bearer ${token}`},
      });
      if (!r.ok) throw new Error('Download failed');
      const blob = await r.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      const doc = documents.find(d => d.id === docId);
      a.download = doc?.file_name || 'document';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      alert('Download failed: ' + e.message);
    }
  }

  // ── Notifications ─────────────────────────────────────────
  async function loadNotifications() {
    try {
      notifications = await Auth.apiGet('/notifications/');
      renderNotifications();
    } catch (e) {
      console.error('Failed to load notifications:', e);
    }
  }

  function renderNotifications() {
    const list = document.getElementById('notif-list');
    list.innerHTML = '';

    if (notifications.length === 0) {
      list.innerHTML = '<div class="notif-empty">No notifications yet.</div>';
      return;
    }

    // Mark all read button
    const unreadCount = notifications.filter(n => !n.is_read).length;
    if (unreadCount > 0) {
      const markAllBtn = document.createElement('button');
      markAllBtn.className = 'mark-all-read';
      markAllBtn.textContent = `Mark all as read (${unreadCount})`;
      markAllBtn.onclick = async () => {
        await Auth.apiPost('/notifications/mark-all-read/', {});
        await loadNotifications();
        loadNotifCount();
        renderStats();
      };
      list.appendChild(markAllBtn);
    }

    notifications.forEach(n => {
      const card = document.createElement('div');
      card.className = `notif-card ${n.is_read ? 'read' : 'unread'}`;
      card.innerHTML = `
        <div class="notif-icon ${n.category}">${CATEGORY_ICONS[n.category] || '&#9679;'}</div>
        <div class="notif-body">
          <div class="notif-title">${esc(n.title)}</div>
          <div class="notif-message">${esc(n.message)}</div>
          <div class="notif-time">${fmtTimeAgo(n.created_at)}</div>
        </div>
        ${!n.is_read ? `
          <div class="notif-action">
            <button class="notif-mark-read" data-id="${n.id}">Mark read</button>
          </div>
        ` : ''}
      `;
      list.appendChild(card);
    });

    // Bind mark-read buttons
    list.querySelectorAll('.notif-mark-read').forEach(btn => {
      btn.onclick = async () => {
        await Auth.apiPut(`/notifications/${btn.dataset.id}/read/`, {});
        await loadNotifications();
        loadNotifCount();
        renderStats();
      };
    });
  }

  // ── Boot ──────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
