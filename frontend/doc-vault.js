/* ============================================================
   doc-vault.js
   TrackFundAI — Document Vault UI
   Lists, uploads, downloads, and tracks document access.
============================================================ */

(() => {
  let documents = [];
  let funds = [];

  const esc = (s) => {
    if (s === null || s === undefined) return '—';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
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

  // ── Init ──────────────────────────────────────────────────
  async function init() {
    if (!Auth.requireAuth()) return;

    const user = Auth.getUser();
    document.getElementById('user-badge').textContent =
      `${user.first_name || user.username} · ${user.role.replace('_', ' ').toUpperCase()}`;
    document.getElementById('org-label').textContent =
      user.organization_name || 'Unknown Organization';

    document.getElementById('btn-logout').onclick = () => Auth.logout();
    document.getElementById('btn-upload').onclick = openUploadModal;
    document.getElementById('upload-modal-close').onclick = closeUploadModal;
    document.getElementById('upload-cancel').onclick = closeUploadModal;
    document.getElementById('upload-form').onsubmit = handleUpload;
    document.getElementById('access-modal-close').onclick = closeAccessModal;

    // Filters
    document.getElementById('filter-category').onchange = loadDocuments;
    document.getElementById('filter-fund').onchange = loadDocuments;
    document.getElementById('filter-search').oninput = debounce(loadDocuments, 300);

    // Load notification count
    loadNotifCount();

    // Load funds for filter + upload dropdowns
    await loadFunds();
    await loadDocuments();
  }

  function debounce(fn, delay) {
    let timer;
    return () => { clearTimeout(timer); timer = setTimeout(fn, delay); };
  }

  // ── Notifications badge ───────────────────────────────────
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

  // ── Load funds for dropdowns ──────────────────────────────
  async function loadFunds() {
    try {
      funds = await Auth.apiGet('/funds/');
      const filterSelect = document.getElementById('filter-fund');
      const uploadSelect = document.getElementById('upload-fund');

      funds.forEach(f => {
        const opt1 = new Option(f.name, f.id);
        const opt2 = new Option(f.name, f.id);
        filterSelect.appendChild(opt1);
        uploadSelect.appendChild(opt2);
      });
    } catch (e) {
      console.error('Failed to load funds:', e);
    }
  }

  // ── Load documents ────────────────────────────────────────
  async function loadDocuments() {
    try {
      let path = '/documents/?';
      const category = document.getElementById('filter-category').value;
      const fund = document.getElementById('filter-fund').value;
      const search = document.getElementById('filter-search').value.trim();

      if (category) path += `category=${category}&`;
      if (fund) path += `fund=${fund}&`;
      if (search) path += `search=${encodeURIComponent(search)}&`;

      documents = await Auth.apiGet(path);
      renderTable();
      renderStats();
    } catch (e) {
      console.error('Failed to load documents:', e);
    }
  }

  function renderStats() {
    const bar = document.getElementById('stats-bar');
    bar.innerHTML = '';
    const totalSize = documents.reduce((a, d) => a + (d.file_size || 0), 0);
    const chips = [
      ['Total Documents', documents.length],
      ['Total Size', fmtSize(totalSize)],
      ['LP Visible', documents.filter(d => d.visibility === 'lp_visible').length],
    ];
    chips.forEach(([label, value]) => {
      const div = document.createElement('div');
      div.className = 'hero-stat';
      div.innerHTML = `<span class="hero-stat-label">${label}</span><span class="hero-stat-value mono">${value}</span>`;
      bar.appendChild(div);
    });
  }

  function renderTable() {
    const tbody = document.getElementById('doc-tbody');
    tbody.innerHTML = '';

    if (documents.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="doc-empty">No documents found. Upload your first document to get started.</td></tr>';
      return;
    }

    documents.forEach(doc => {
      const fundName = doc.fund ? (funds.find(f => f.id === doc.fund)?.name || 'Unknown Fund') : '—';
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>
          <div class="doc-title">${esc(doc.title)}</div>
          <div class="doc-filename">${esc(doc.file_name)}</div>
        </td>
        <td><span class="doc-category-badge">${esc(doc.category_display)}</span></td>
        <td>${esc(fundName)}</td>
        <td><span class="doc-visibility-badge ${doc.visibility}">${esc(doc.visibility_display)}</span></td>
        <td style="font-family: var(--font-mono); font-size: 12px;">${fmtSize(doc.file_size)}</td>
        <td style="font-size: 12px; white-space: nowrap;">${fmtDate(doc.created_at)}<br><span style="color: var(--text-muted); font-size: 11px;">${esc(doc.uploaded_by_name)}</span></td>
        <td>
          <div class="doc-actions">
            <button class="doc-action-btn" data-action="download" data-id="${doc.id}">Download</button>
            <button class="doc-action-btn" data-action="access-log" data-id="${doc.id}" data-title="${esc(doc.title)}">Log</button>
            <button class="doc-action-btn delete" data-action="delete" data-id="${doc.id}" data-title="${esc(doc.title)}">Delete</button>
          </div>
        </td>
      `;
      tbody.appendChild(tr);
    });

    // Bind action buttons
    tbody.querySelectorAll('.doc-action-btn').forEach(btn => {
      btn.onclick = () => handleDocAction(btn.dataset.action, btn.dataset.id, btn.dataset.title);
    });
  }

  // ── Document actions ──────────────────────────────────────
  async function handleDocAction(action, docId, title) {
    if (action === 'download') {
      // Open download URL in new tab
      const token = Auth.getToken();
      const base = window.location.port === '8000' || window.location.port === '' ? '' : 'http://127.0.0.1:8000';
      const url = `${base}/api/documents/${docId}/download/`;

      // Use fetch to download with auth header
      try {
        const r = await fetch(url, {
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
    } else if (action === 'access-log') {
      await showAccessLog(docId, title);
    } else if (action === 'delete') {
      if (!confirm(`Delete "${title}"? This cannot be undone.`)) return;
      try {
        await Auth.apiDelete(`/documents/${docId}/delete/`);
        await loadDocuments();
      } catch (e) {
        alert('Delete failed: ' + e.message);
      }
    }
  }

  // ── Access log modal ──────────────────────────────────────
  async function showAccessLog(docId, title) {
    try {
      const logs = await Auth.apiGet(`/documents/${docId}/access-log/`);
      document.getElementById('access-modal-title').textContent = `Access Log — ${title}`;
      const content = document.getElementById('access-log-content');

      if (logs.length === 0) {
        content.innerHTML = '<p style="color: var(--text-muted); padding: 20px; text-align: center;">No access records yet.</p>';
      } else {
        content.innerHTML = `
          <table class="access-log-table">
            <thead>
              <tr><th>User</th><th>Action</th><th>IP Address</th><th>Timestamp</th></tr>
            </thead>
            <tbody>
              ${logs.map(l => `
                <tr>
                  <td>${esc(l.user_name)}</td>
                  <td>${esc(l.action)}</td>
                  <td style="font-family: var(--font-mono);">${esc(l.ip_address)}</td>
                  <td>${fmtDate(l.timestamp)}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        `;
      }

      document.getElementById('access-modal').classList.remove('hidden');
    } catch (e) {
      alert('Failed to load access log: ' + e.message);
    }
  }

  function closeAccessModal() {
    document.getElementById('access-modal').classList.add('hidden');
  }

  // ── Upload modal ──────────────────────────────────────────
  function openUploadModal() {
    document.getElementById('upload-form').reset();
    document.getElementById('upload-modal').classList.remove('hidden');
  }

  function closeUploadModal() {
    document.getElementById('upload-modal').classList.add('hidden');
  }

  async function handleUpload(e) {
    e.preventDefault();
    const btn = document.getElementById('upload-submit');
    btn.disabled = true;
    btn.textContent = 'Uploading...';

    try {
      const formData = new FormData();
      formData.append('file', document.getElementById('upload-file').files[0]);
      formData.append('title', document.getElementById('upload-title').value.trim());
      formData.append('description', document.getElementById('upload-desc').value.trim());
      formData.append('category', document.getElementById('upload-category').value);
      formData.append('visibility', document.getElementById('upload-visibility').value);

      const fundId = document.getElementById('upload-fund').value;
      if (fundId) formData.append('fund_id', fundId);

      await Auth.apiUpload('/documents/upload/', formData);
      closeUploadModal();
      await loadDocuments();
    } catch (err) {
      alert('Upload failed: ' + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Upload';
    }
  }

  // ── Boot ──────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
