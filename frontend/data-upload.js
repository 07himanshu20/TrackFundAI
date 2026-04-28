/* ============================================================
   data-upload.js
   TrackFundAI — Fund Data Upload with Gemini AI Column Mapping
   Drag-and-drop Excel files → SSE progress → result summary.
============================================================ */

(() => {
  let queuedFiles = [];
  let isImporting = false;

  const esc = (s) => {
    if (s === null || s === undefined) return '';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  };

  const fmtSize = (bytes) => {
    if (!bytes) return '0 B';
    if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(1)} MB`;
    if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${bytes} B`;
  };

  // ── Init ──────────────────────────────────────────────────
  function init() {
    if (!Auth.requireAuth()) return;

    const user = Auth.getUser();
    document.getElementById('user-badge').textContent =
      `${user.first_name || user.username} \u00b7 ${user.role.replace('_', ' ').toUpperCase()}`;

    document.getElementById('btn-logout').onclick = () => Auth.logout();

    // Drop zone events
    const dropZone = document.getElementById('drop-zone');
    const fileInput = document.getElementById('file-input');

    dropZone.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropZone.classList.add('drag-over');
    });

    dropZone.addEventListener('dragleave', (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropZone.classList.remove('drag-over');
    });

    dropZone.addEventListener('drop', (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropZone.classList.remove('drag-over');
      handleFiles(e.dataTransfer.files);
    });

    dropZone.addEventListener('click', () => {
      if (!isImporting) fileInput.click();
    });

    fileInput.addEventListener('change', () => {
      handleFiles(fileInput.files);
      fileInput.value = '';
    });

    // Buttons
    document.getElementById('btn-clear').onclick = clearQueue;
    document.getElementById('btn-start').onclick = startUpload;
    document.getElementById('btn-new-import').onclick = resetUI;

    // Load notification count
    loadNotifCount();
  }

  // ── Notifications ─────────────────────────────────────────
  async function loadNotifCount() {
    try {
      const data = await Auth.apiGet('/notifications/unread-count/');
      const badge = document.getElementById('notif-badge');
      badge.textContent = data.unread_count || 0;
      badge.classList.toggle('zero', !data.unread_count);
    } catch (e) {
      console.error('Notification count error:', e);
    }
  }

  // ── File handling ─────────────────────────────────────────
  function handleFiles(fileList) {
    if (isImporting) return;

    for (const file of fileList) {
      const ext = file.name.toLowerCase();
      if (!ext.endsWith('.xlsx') && !ext.endsWith('.xls')) {
        alert(`Invalid file type: ${file.name}\nOnly .xlsx files are accepted.`);
        continue;
      }
      // Check for duplicates
      if (queuedFiles.some(f => f.name === file.name && f.size === file.size)) {
        continue;
      }
      queuedFiles.push(file);
    }

    renderQueue();
  }

  function removeFile(index) {
    if (isImporting) return;
    queuedFiles.splice(index, 1);
    renderQueue();
  }

  function clearQueue() {
    if (isImporting) return;
    queuedFiles = [];
    renderQueue();
  }

  function renderQueue() {
    const container = document.getElementById('file-queue');
    const actions = document.getElementById('upload-actions');

    if (queuedFiles.length === 0) {
      container.innerHTML = '';
      actions.style.display = 'none';
      return;
    }

    actions.style.display = 'flex';
    document.getElementById('file-count').textContent =
      `${queuedFiles.length} file${queuedFiles.length !== 1 ? 's' : ''} selected`;

    container.innerHTML = queuedFiles.map((file, i) => `
      <div class="file-queue-item" data-index="${i}">
        <div class="file-icon">XLSX</div>
        <div class="file-info">
          <div class="file-name">${esc(file.name)}</div>
          <div class="file-size">${fmtSize(file.size)}</div>
        </div>
        <span class="file-status pending" id="file-status-${i}">Queued</span>
        ${isImporting ? '' : `<button class="btn-remove" onclick="event.stopPropagation()">&times;</button>`}
      </div>
    `).join('');

    // Attach remove handlers
    container.querySelectorAll('.btn-remove').forEach((btn, i) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        removeFile(i);
      };
    });
  }

  // ── Upload & Import ───────────────────────────────────────
  async function startUpload() {
    if (isImporting || queuedFiles.length === 0) return;

    isImporting = true;
    document.getElementById('btn-start').disabled = true;
    document.getElementById('btn-clear').style.display = 'none';
    renderQueue();  // Re-render without remove buttons

    // Show progress
    const progressEl = document.getElementById('import-progress');
    progressEl.classList.add('active');
    updateProgress(0, 'Uploading files to server...');

    // Upload files
    const formData = new FormData();
    for (const file of queuedFiles) {
      formData.append('files', file);
    }

    let jobId;
    try {
      const uploadResult = await Auth.apiUpload('/dataimport/upload/', formData);
      jobId = uploadResult.job_id;
      updateProgress(2, 'Files uploaded. Starting AI-powered import...');
    } catch (err) {
      updateProgress(0, 'Upload failed: ' + err.message);
      isImporting = false;
      document.getElementById('btn-start').disabled = false;
      document.getElementById('btn-clear').style.display = '';
      return;
    }

    // Connect to SSE stream
    connectSSE(jobId);
  }

  function connectSSE(jobId) {
    const token = Auth.getToken();
    const API_BASE = (() => {
      const p = window.location.port;
      const same = (p === '8000' || p === '' || p === '80' || p === '443');
      return same ? '' : 'http://127.0.0.1:8000';
    })();

    const url = `${API_BASE}/api/dataimport/jobs/${jobId}/stream/?token=${token}`;

    const source = new EventSource(url);
    let lastPct = 0;

    source.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);

        // Progress update
        if (data.pct !== undefined) {
          lastPct = data.pct;
          updateProgress(data.pct, data.msg || '', data.file || '');

          // Update individual file status
          if (data.file_index !== undefined) {
            updateFileStatus(data.file_index, 'importing', data.msg);
          }
        }

        // File complete
        if (data.event === 'file_complete') {
          if (data.file_index !== undefined) {
            updateFileStatus(data.file_index, 'completed', 'Done');
          }
        }

        // File error
        if (data.event === 'file_error') {
          if (data.file_index !== undefined) {
            updateFileStatus(data.file_index, 'failed', data.error);
          }
        }

        // Job complete
        if (data.event === 'job_complete') {
          source.close();
          onImportComplete(data.results || {}, data.errors || []);
        }

        // Error event
        if (data.event === 'error') {
          source.close();
          updateProgress(lastPct, 'Error: ' + (data.error || 'Unknown error'));
          isImporting = false;
        }

      } catch (e) {
        console.error('SSE parse error:', e);
      }
    };

    source.onerror = () => {
      source.close();
      // Fall back to polling
      console.warn('SSE connection lost, falling back to polling...');
      pollJobStatus(jobId);
    };
  }

  // ── Polling Fallback ──────────────────────────────────────
  function pollJobStatus(jobId) {
    const interval = setInterval(async () => {
      try {
        const data = await Auth.apiGet(`/dataimport/jobs/${jobId}/status/`);

        updateProgress(data.progress_pct || 0, data.progress_message || '');

        if (data.status === 'completed' || data.status === 'completed_with_errors') {
          clearInterval(interval);
          onImportComplete(data.result_summary || {}, data.error_log || []);
        } else if (data.status === 'failed') {
          clearInterval(interval);
          updateProgress(data.progress_pct || 0, 'Import failed');
          isImporting = false;
        }
      } catch (e) {
        console.error('Polling error:', e);
        clearInterval(interval);
        isImporting = false;
      }
    }, 2000);
  }

  // ── Progress UI ───────────────────────────────────────────
  function updateProgress(pct, message, fileName) {
    document.getElementById('progress-percent').textContent = `${Math.round(pct)}%`;
    document.getElementById('progress-bar').style.width = `${pct}%`;
    if (message) {
      document.getElementById('progress-message').textContent = message;
    }
    if (fileName) {
      document.getElementById('progress-file').textContent = fileName;
    }
  }

  function updateFileStatus(index, status, message) {
    const el = document.getElementById(`file-status-${index}`);
    if (!el) return;

    el.className = `file-status ${status}`;

    const labels = {
      pending: 'Queued',
      mapping: 'AI Mapping...',
      importing: 'Importing...',
      completed: 'Done',
      failed: 'Failed',
    };
    el.textContent = labels[status] || status;
  }

  // ── Import Complete ───────────────────────────────────────
  function onImportComplete(results, errors) {
    isImporting = false;

    // Hide progress
    document.getElementById('import-progress').classList.remove('active');

    // Show results
    const resultsEl = document.getElementById('import-results');
    resultsEl.classList.add('active');

    // Aggregate counts across all files
    const totalCounts = {};
    for (const fileName of Object.keys(results)) {
      const fileCounts = results[fileName]?.counts || results[fileName] || {};
      for (const [key, val] of Object.entries(fileCounts)) {
        if (typeof val === 'number') {
          totalCounts[key] = (totalCounts[key] || 0) + val;
        }
      }
    }

    // Render result cards
    const grid = document.getElementById('results-grid');
    const labelMap = {
      funds: 'Funds',
      schemes: 'Schemes',
      investors: 'Investors',
      commitments: 'Commitments',
      capital_calls: 'Capital Calls',
      portfolio_companies: 'Companies',
      investments: 'Investments',
      tranches: 'Tranches',
      valuations: 'Valuations',
      nav_records: 'NAV Records',
      exit_events: 'Exits',
      distributions: 'Distributions',
      sebi_reports: 'SEBI Reports',
      compliance_calendar: 'Calendar Events',
    };

    grid.innerHTML = Object.entries(totalCounts)
      .filter(([, v]) => v > 0)
      .map(([key, count]) => `
        <div class="result-card">
          <div class="count">${count}</div>
          <div class="label">${esc(labelMap[key] || key)}</div>
        </div>
      `).join('');

    // If no counts, show a message
    if (Object.keys(totalCounts).length === 0) {
      grid.innerHTML = '<p style="color:var(--text-secondary); grid-column:1/-1; text-align:center;">No records were imported. Check the files and try again.</p>';
    }

    // Show errors if any
    if (errors.length > 0) {
      const errorsEl = document.getElementById('import-errors');
      errorsEl.classList.add('active');

      document.getElementById('error-list').innerHTML = errors.map(err => `
        <div class="error-item">
          <div class="error-file">${esc(err.file || 'Unknown file')}</div>
          <div class="error-msg">${esc(err.error || 'Unknown error')}</div>
        </div>
      `).join('');

      document.getElementById('results-title').textContent = 'Import Completed with Errors';
    }
  }

  // ── Reset UI ──────────────────────────────────────────────
  function resetUI() {
    queuedFiles = [];
    isImporting = false;

    document.getElementById('file-queue').innerHTML = '';
    document.getElementById('upload-actions').style.display = 'none';
    document.getElementById('import-progress').classList.remove('active');
    document.getElementById('import-results').classList.remove('active');
    document.getElementById('import-errors').classList.remove('active');
    document.getElementById('btn-start').disabled = false;
    document.getElementById('btn-clear').style.display = '';
    document.getElementById('progress-bar').style.width = '0%';
    document.getElementById('progress-percent').textContent = '0%';
    document.getElementById('progress-message').textContent = '';
    document.getElementById('progress-file').textContent = '';
    document.getElementById('results-grid').innerHTML = '';
    document.getElementById('error-list').innerHTML = '';
    document.getElementById('results-title').textContent = 'Import Complete';
  }

  // ── Bootstrap ─────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
