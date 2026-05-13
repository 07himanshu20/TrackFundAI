/* ============================================================
   data-upload.js
   TrackFundAI — Fund Data Upload with Gemini AI Column Mapping
   Drag-and-drop Excel files → SSE progress → result summary.
   Uploaded files listing with per-file delete (cascading).
============================================================ */

(() => {
  let queuedFiles = [];
  let isImporting = false;
  let deleteTargetFileId = null;
  let deleteTargetFundName = '';

  // Progress interpolation state
  let targetFilePct = 0;
  let displayFilePct = 0;
  let targetOverallPct = 0;
  let displayOverallPct = 0;
  let interpolationTimer = null;
  let totalFilesInBatch = 1;
  // Anti-backwards + creep state
  let lastConfirmedFilePct = 0;   // highest SSE pct ever received for current file
  let lastSseTimestamp = 0;       // ms timestamp of last SSE data event
  let currentPhase = 'file_upload'; // current import phase label

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

  const fmtDate = (iso) => {
    if (!iso) return '';
    const d = new Date(iso);
    const day = d.getDate().toString().padStart(2, '0');
    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const mon = months[d.getMonth()];
    const year = d.getFullYear();
    const h = d.getHours().toString().padStart(2, '0');
    const m = d.getMinutes().toString().padStart(2, '0');
    return `${day} ${mon} ${year}, ${h}:${m}`;
  };

  // ── Init ─────────────────────────────────────────���────────
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

    // Modal buttons
    document.getElementById('modal-close').onclick = closeDeleteModal;
    document.getElementById('modal-cancel').onclick = closeDeleteModal;
    document.getElementById('modal-confirm').onclick = confirmDelete;

    // Close modal on overlay click
    document.getElementById('delete-modal').addEventListener('click', (e) => {
      if (e.target === e.currentTarget) closeDeleteModal();
    });

    // Load stuck imports first (shows warnings), then completed files, then notifications
    loadStuckImports();
    loadUploadedFiles();
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

  // ── Stuck / Partial Imports ───────────────────────────────
  async function loadStuckImports() {
    const section = document.getElementById('stuck-section');
    const listEl  = document.getElementById('stuck-list');
    const badge   = document.getElementById('stuck-count-badge');

    try {
      const items = await Auth.apiGet('/dataimport/stuck-imports/');

      if (!items || items.length === 0) {
        section.style.display = 'none';
        return;
      }

      badge.textContent = items.length;
      section.style.display = 'block';

      const statusLabel = { importing: 'Stuck mid-import', mapping: 'Stuck during AI mapping', failed: 'Failed' };

      listEl.innerHTML = items.map(item => {
        const counts = item.data_in_db || {};
        const hasData = Object.keys(counts).length > 0;

        const countPills = hasData
          ? Object.entries(counts).map(([k, v]) =>
              `<span class="stuck-data-pill">${v} ${k}</span>`
            ).join('')
          : '<span class="stuck-data-pill stuck-data-pill--none">No data written yet</span>';

        const fundDisplay = item.fund_name || item.original_filename;

        return `
        <div class="stuck-item" data-file-id="${item.id}">
          <div class="stuck-item-left">
            <div class="stuck-item-icon">
              <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
                <path d="M10 2L2 17h16L10 2z" stroke="#f59e0b" stroke-width="1.6" stroke-linejoin="round"/>
                <path d="M10 8v4M10 14.5v.5" stroke="#f59e0b" stroke-width="1.8" stroke-linecap="round"/>
              </svg>
            </div>
            <div class="stuck-item-info">
              <div class="stuck-item-fund">
                ${esc(fundDisplay)}
                <span class="stuck-status-pill">${esc(statusLabel[item.status] || item.status)}</span>
              </div>
              <div class="stuck-item-meta">
                <span>${esc(item.original_filename)}</span>
                <span class="meta-sep">&bull;</span>
                <span>Started ${fmtDate(item.uploaded_at)}</span>
                <span class="meta-sep">&bull;</span>
                <span>by ${esc(item.uploaded_by)}</span>
              </div>
              <div class="stuck-data-counts">
                <span class="stuck-data-label">${hasData ? 'Partial data already in DB:' : 'Partial data in DB:'}</span>
                ${countPills}
              </div>
            </div>
          </div>
          <button class="btn-stuck-delete" data-file-id="${item.id}"
                  data-fund-name="${esc(fundDisplay)}"
                  title="Delete this partial import and all its data">
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
              <path d="M5.5 2H10.5M2 4H14M12.667 4L12.11 12.591C12.049 13.468 11.317 14.167 10.438 14.167H5.562C4.683 14.167 3.951 13.468 3.89 12.591L3.333 4M6.333 7V11M9.667 7V11" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            Delete &amp; Clean Up
          </button>
        </div>`;
      }).join('');

      // Wire delete buttons — reuses the same modal + confirmDelete flow
      listEl.querySelectorAll('.btn-stuck-delete').forEach(btn => {
        btn.onclick = (e) => {
          e.stopPropagation();
          openDeleteModal(btn.dataset.fileId, btn.dataset.fundName);
        };
      });

    } catch (e) {
      console.error('Failed to load stuck imports:', e);
      section.style.display = 'none';
    }
  }

  // ── Previously Uploaded Files ─────────────────────────────
  async function loadUploadedFiles() {
    const section = document.getElementById('uploaded-section');
    const listEl = document.getElementById('uploaded-list');
    const emptyEl = document.getElementById('uploaded-empty');

    try {
      const files = await Auth.apiGet('/dataimport/uploaded-files/');

      if (!files || files.length === 0) {
        section.style.display = 'block';
        listEl.style.display = 'none';
        emptyEl.style.display = 'flex';
        return;
      }

      section.style.display = 'block';
      listEl.style.display = 'block';
      emptyEl.style.display = 'none';

      listEl.innerHTML = files.map(f => {
        const isStuck = f.status === 'importing' || f.status === 'mapping';
        const isFailed = f.status === 'failed';
        const statusBadge = isStuck
          ? `<span class="file-status-badge status-stuck">Stuck — data may be partial</span>`
          : isFailed
          ? `<span class="file-status-badge status-failed">Failed</span>`
          : '';
        return `
        <div class="uploaded-item${isStuck ? ' uploaded-item--stuck' : ''}" data-file-id="${f.id}">
          <div class="uploaded-item-icon">
            <span class="xlsx-badge">XLSX</span>
          </div>
          <div class="uploaded-item-info">
            <div class="uploaded-item-fund">${esc(f.fund_name || f.original_filename)}${statusBadge}</div>
            <div class="uploaded-item-meta">
              <span class="meta-file">${esc(f.original_filename)}</span>
              <span class="meta-sep">&bull;</span>
              <span class="meta-size">${fmtSize(f.file_size)}</span>
              <span class="meta-sep">&bull;</span>
              <span class="meta-by">Uploaded by ${esc(f.uploaded_by)}</span>
              <span class="meta-sep">&bull;</span>
              <span class="meta-date">${fmtDate(f.uploaded_at)}</span>
            </div>
          </div>
          <button class="btn-delete-file" data-file-id="${f.id}"
                  data-fund-name="${esc(f.fund_name || f.original_filename)}"
                  title="Delete this fund and all its data">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M5.5 2H10.5M2 4H14M12.667 4L12.11 12.591C12.049 13.468 11.317 14.167 10.438 14.167H5.562C4.683 14.167 3.951 13.468 3.89 12.591L3.333 4M6.333 7V11M9.667 7V11" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            Delete
          </button>
        </div>`;
      }).join('');

      // Attach delete handlers
      listEl.querySelectorAll('.btn-delete-file').forEach(btn => {
        btn.onclick = (e) => {
          e.stopPropagation();
          openDeleteModal(btn.dataset.fileId, btn.dataset.fundName);
        };
      });

    } catch (e) {
      console.error('Failed to load uploaded files:', e);
      section.style.display = 'none';
    }
  }

  // ── Delete Modal ──────────────────────────────────────────
  function openDeleteModal(fileId, fundName) {
    deleteTargetFileId = fileId;
    deleteTargetFundName = fundName;
    document.getElementById('modal-fund-name').textContent = fundName;
    document.getElementById('delete-modal').style.display = 'flex';
    document.getElementById('modal-confirm').disabled = false;
    document.getElementById('modal-confirm').textContent = 'Delete All Data';
  }

  function closeDeleteModal() {
    document.getElementById('delete-modal').style.display = 'none';
    deleteTargetFileId = null;
    deleteTargetFundName = '';
  }

  async function confirmDelete() {
    if (!deleteTargetFileId) return;

    const confirmBtn = document.getElementById('modal-confirm');
    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Deleting...';

    try {
      const result = await Auth.apiDelete(
        `/dataimport/files/${deleteTargetFileId}/`
      );

      closeDeleteModal();

      // Show a brief success message
      showToast(`${deleteTargetFundName} and all its data have been deleted.`);

      // Refresh both panels
      loadStuckImports();
      loadUploadedFiles();

    } catch (e) {
      console.error('Delete failed:', e);
      confirmBtn.disabled = false;
      confirmBtn.textContent = 'Delete All Data';
      showToast(`Delete failed: ${e.message || 'Unknown error'}`, 'error');
    }
  }

  function showToast(message, type = 'success') {
    // Remove any existing toast
    const existing = document.querySelector('.upload-toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.className = `upload-toast ${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);

    // Animate in
    requestAnimationFrame(() => toast.classList.add('visible'));

    // Remove after 4 seconds
    setTimeout(() => {
      toast.classList.remove('visible');
      setTimeout(() => toast.remove(), 300);
    }, 4000);
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
      if (same) return '';
      const backendPort = localStorage.getItem('tfai_backend_port') || '8000';
      return `http://127.0.0.1:${backendPort}`;
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
          lastSseTimestamp = Date.now();   // record when we last heard from backend

          const nFiles = data.total_files || 1;
          const fileIdx = data.file_index !== undefined ? data.file_index : 0;
          totalFilesInBatch = nFiles;

          // Overall target — only ever increases
          if (data.pct > targetOverallPct) targetOverallPct = data.pct;

          // Per-file target — reverse the backend's file-span math
          let newFilePct;
          if (nFiles > 1) {
            const fileSpan = 100 / nFiles;
            const fileBase = fileIdx * fileSpan;
            newFilePct = Math.max(0, Math.min(100,
              ((data.pct - fileBase) / fileSpan) * 100));
          } else {
            newFilePct = data.pct;
          }

          // Never go backwards: only advance if SSE reports higher than current max
          if (newFilePct > lastConfirmedFilePct) {
            lastConfirmedFilePct = newFilePct;
            targetFilePct = newFilePct;
          }

          // Phase — update step indicator from backend label
          if (data.phase && PHASE_STEP_IDX[data.phase] !== undefined) {
            currentPhase = data.phase;
          }

          // Show/configure overall bar for multi-file batches
          if (nFiles > 1) {
            document.getElementById('progress-overall-section').style.display = 'block';
            document.getElementById('progress-title-text').textContent =
              `Current File (${fileIdx + 1} of ${nFiles})`;
            document.getElementById('progress-batch-info').textContent =
              `File ${fileIdx + 1} of ${nFiles}`;
          }

          // Update message and file label
          if (data.msg) {
            document.getElementById('progress-message').textContent = data.msg;
            const phaseEl = document.getElementById('progress-phase');
            if (phaseEl) phaseEl.textContent = data.msg;
          }
          if (data.file) {
            document.getElementById('progress-file').textContent = data.file;
          }

          // Kick off smooth decimal interpolation (idempotent — won't restart if running)
          startInterpolation();

          // Update individual file status badge
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
  const PROGRESS_STEPS = ['pstep-upload', 'pstep-scan', 'pstep-ai', 'pstep-import', 'pstep-done'];

  // Maps backend phase strings → step index
  const PHASE_STEP_IDX = {
    file_upload: 0,
    sheet_scan:  1,
    ai_mapping:  2,
    data_import: 3,
    complete:    4,
  };

  function updateProgressSteps(phase) {
    const activeIdx = PHASE_STEP_IDX[phase] ?? 0;
    PROGRESS_STEPS.forEach((id, i) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (i < activeIdx) {
        el.classList.add('done');
        el.classList.remove('active');
      } else if (i === activeIdx) {
        el.classList.add('active');
        el.classList.remove('done');
      } else {
        el.classList.remove('active', 'done');
      }
    });
  }

  function renderProgressBars() {
    const fp = Math.max(0, Math.min(100, displayFilePct));
    document.getElementById('progress-percent').textContent = `${fp.toFixed(1)}%`;
    document.getElementById('progress-bar').style.width = `${fp}%`;

    if (totalFilesInBatch > 1) {
      const op = Math.max(0, Math.min(100, displayOverallPct));
      document.getElementById('progress-overall-percent').textContent = `${op.toFixed(1)}%`;
      document.getElementById('progress-overall-bar').style.width = `${op}%`;
    }

    updateProgressSteps(currentPhase);
  }

  function startInterpolation() {
    if (interpolationTimer) return;
    interpolationTimer = setInterval(() => {
      const now = Date.now();
      const msSinceSse = lastSseTimestamp > 0 ? (now - lastSseTimestamp) : 0;

      // ── Creep mode ────────────────────────────────────────────────────
      // When the import is running but no SSE event has arrived for 2+
      // seconds (e.g. during a Gemini API call or large DB transaction),
      // inch the bar forward so it never looks frozen.
      // Ceiling: lastConfirmedFilePct + 10%, hard cap at 98%.
      if (msSinceSse > 2000 && lastConfirmedFilePct < 98) {
        const creepCeiling = Math.min(lastConfirmedFilePct + 10, 98);
        if (targetFilePct < creepCeiling) {
          // 0.5 % per second = 0.05 per 100 ms tick
          targetFilePct = Math.min(targetFilePct + 0.05, creepCeiling);
          if (totalFilesInBatch <= 1) targetOverallPct = targetFilePct;
        }
      }

      let moved = false;

      if (Math.abs(displayFilePct - targetFilePct) > 0.05) {
        displayFilePct = displayFilePct < targetFilePct
          ? Math.min(displayFilePct + 0.1, targetFilePct)
          : Math.max(displayFilePct - 0.1, targetFilePct);
        moved = true;
      }

      if (totalFilesInBatch > 1 && Math.abs(displayOverallPct - targetOverallPct) > 0.05) {
        displayOverallPct = displayOverallPct < targetOverallPct
          ? Math.min(displayOverallPct + 0.1, targetOverallPct)
          : Math.max(displayOverallPct - 0.1, targetOverallPct);
        moved = true;
      }

      if (moved) renderProgressBars();
    }, 100);
  }

  function stopInterpolation() {
    if (interpolationTimer) {
      clearInterval(interpolationTimer);
      interpolationTimer = null;
    }
  }

  function updateProgress(pct, message, fileName) {
    // Only move forward — never let external calls regress the bar
    if (pct >= lastConfirmedFilePct) {
      lastConfirmedFilePct = pct;
      targetFilePct = pct;
      if (totalFilesInBatch <= 1) targetOverallPct = pct;
    }

    // Immediate render while not yet in SSE interpolation mode
    if (!interpolationTimer) {
      displayFilePct = Math.max(displayFilePct, pct);
      displayOverallPct = Math.max(displayOverallPct, pct);
      renderProgressBars();
    }

    if (message) {
      document.getElementById('progress-message').textContent = message;
      const phaseEl = document.getElementById('progress-phase');
      if (phaseEl) phaseEl.textContent = message;
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
    stopInterpolation();
    isImporting = false;

    // Snap both bars to 100% and step to Complete before hiding
    currentPhase = 'complete';
    lastConfirmedFilePct = 100;
    displayFilePct = 100;
    displayOverallPct = 100;
    renderProgressBars();

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

    // Refresh both panels
    loadStuckImports();
    loadUploadedFiles();
  }

  // ── Reset UI ──────────────────────────────────────────────
  function resetUI() {
    stopInterpolation();
    targetFilePct = 0;
    displayFilePct = 0;
    targetOverallPct = 0;
    displayOverallPct = 0;
    totalFilesInBatch = 1;
    lastConfirmedFilePct = 0;
    lastSseTimestamp = 0;
    currentPhase = 'file_upload';
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
    document.getElementById('progress-percent').textContent = '0.0%';
    document.getElementById('progress-message').textContent = '';
    document.getElementById('progress-file').textContent = '';
    document.getElementById('results-grid').innerHTML = '';
    document.getElementById('error-list').innerHTML = '';
    document.getElementById('results-title').textContent = 'Import Complete';
    document.getElementById('progress-overall-section').style.display = 'none';
    document.getElementById('progress-overall-bar').style.width = '0%';
    document.getElementById('progress-overall-percent').textContent = '0.0%';
    document.getElementById('progress-title-text').textContent = 'Importing with Gemini AI';
    // Reset step indicators
    PROGRESS_STEPS.forEach(id => {
      const el = document.getElementById(id);
      if (el) el.classList.remove('active', 'done');
    });
    updateProgressSteps('file_upload');
  }

  // ── Bootstrap ───────────────────────────────────���─────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
