/* Pre-ingestion consolidation UI — upload many client files, watch the run,
   view the report, download the consolidated TFAI.xlsx. */
(function () {
  if (!window.Auth || !Auth.requireAuth()) return;

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const btnLogout = $('btn-logout');
  if (btnLogout) btnLogout.onclick = () => Auth.logout();

  let files = [];
  let pollTimer = null;

  const drop = $('drop'), input = $('fileinput'), listEl = $('filelist'), btnRun = $('btn-run');

  drop.onclick = () => input.click();
  ['dragover', 'dragenter'].forEach((e) => drop.addEventListener(e, (ev) => {
    ev.preventDefault(); drop.classList.add('drag');
  }));
  ['dragleave', 'drop'].forEach((e) => drop.addEventListener(e, (ev) => {
    ev.preventDefault(); drop.classList.remove('drag');
  }));
  drop.addEventListener('drop', (ev) => addFiles(ev.dataTransfer.files));
  input.addEventListener('change', () => addFiles(input.files));

  function addFiles(fl) {
    for (const f of fl) {
      if (!/\.(xlsx|xls)$/i.test(f.name)) continue;
      if (!files.some((x) => x.name === f.name && x.size === f.size)) files.push(f);
    }
    renderList();
  }

  function renderList() {
    listEl.innerHTML = files.map((f, i) =>
      `<div class="pi-fileitem"><span>📄 ${esc(f.name)} <span class="pi-muted">(${(f.size / 1024).toFixed(0)} KB)</span></span>
       <button data-i="${i}" title="remove">✕</button></div>`).join('');
    listEl.querySelectorAll('button').forEach((b) => b.onclick = () => {
      files.splice(+b.dataset.i, 1); renderList();
    });
    btnRun.disabled = files.length === 0;
    $('run-hint').textContent = files.length
      ? `${files.length} file${files.length > 1 ? 's' : ''} ready` : '';
  }

  btnRun.onclick = async () => {
    if (!files.length) return;
    btnRun.disabled = true;
    const fd = new FormData();
    files.forEach((f) => fd.append('files', f));
    try {
      const res = await Auth.apiUpload('/dataimport/preingest/', fd);
      $('progress-card').style.display = 'block';
      $('result-card').style.display = 'none';
      poll(res.job_id);
    } catch (e) {
      alert('Upload failed: ' + (e.message || e));
      btnRun.disabled = false;
    }
  };

  function poll(jobId) {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      let job;
      try { job = await Auth.apiGet(`/dataimport/preingest/${jobId}/`); }
      catch (e) { return; }
      $('prog-fill').style.width = (job.progress_pct || 0) + '%';
      $('prog-pct').textContent = (job.progress_pct || 0) + '%';
      $('prog-msg').textContent = job.progress_message || job.status;
      if (['completed', 'completed_with_errors', 'failed'].includes(job.status)) {
        clearInterval(pollTimer);
        btnRun.disabled = false;
        renderResult(job);
      }
    }, 1500);
  }

  function renderResult(job) {
    const card = $('result-card');
    card.style.display = 'block';
    if (job.status === 'failed') {
      card.innerHTML = `<h1 class="pi-h1">Consolidation failed</h1>
        <p class="pi-review">${esc(job.progress_message)}</p>`;
      return;
    }
    const s = job.summary || {};
    const rowCounts = s.sheet_row_counts || {};
    const sheetRows = Object.keys(rowCounts).map((name) =>
      `<tr><td>${esc(name)}</td><td class="${rowCounts[name] ? '' : 'pi-muted'}">${rowCounts[name]}</td></tr>`).join('');
    const audit = (s.audit || []).map((a) =>
      `<tr><td>${esc(a.file)}</td><td>${esc(a.kind)}</td><td>${a.records_extracted}</td>
       <td class="${a.status === 'ok' ? '' : 'pi-review'}">${esc(a.status)}</td></tr>`).join('');
    const populated = Object.values(rowCounts).filter((c) => c).length;

    card.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px">
        <h1 class="pi-h1" style="margin:0">Fund Master Workbook ready</h1>
        <button id="btn-dl" class="pi-btn">⬇ Download TFAI.xlsx</button>
      </div>
      ${job.status === 'completed_with_errors'
        ? '<p class="pi-review">Completed with some files flagged — see the audit tab.</p>' : ''}
      <div class="pi-grid" style="margin:16px 0">
        <div class="pi-stat"><div class="n">${s.files || 0}</div><div class="l">Files</div></div>
        <div class="pi-stat"><div class="n">${s.company_rows || 0}</div><div class="l">Portfolio companies</div></div>
        <div class="pi-stat"><div class="n">${populated}/${Object.keys(rowCounts).length}</div><div class="l">Fund sheets filled</div></div>
      </div>
      <div style="display:flex;gap:24px;flex-wrap:wrap">
        <div style="flex:1;min-width:260px">
          <h3 style="margin:10px 0 4px">Output sheets (rows)</h3>
          <table class="pi-tbl"><thead><tr><th>Sheet</th><th>Rows</th></tr></thead><tbody>${sheetRows}</tbody></table>
        </div>
        <div style="flex:1;min-width:260px">
          <h3 style="margin:10px 0 4px">Per-file audit</h3>
          <table class="pi-tbl"><thead><tr><th>File</th><th>Kind</th><th>Recs</th><th>Status</th></tr></thead><tbody>${audit}</tbody></table>
        </div>
      </div>
      <p class="pi-note" style="margin-top:14px">
        The workbook mirrors the TFAI Fund Master format — fixed sheets (LP Register, Investments,
        Tranches, Valuations, Distributions, Exits, Portfolio_KPI, NAV, Waterfall, Fees, SEBI…).
        Each company MIS collapses to <b>one Portfolio_KPI row</b> (TTM). Gemini maps columns; Python
        moves values verbatim. See the <b>_Audit</b> tab for per-file coverage.</p>`;

    $('btn-dl').onclick = () => downloadOutput(job.job_id, job.output_name || 'TFAI.xlsx');
  }

  async function downloadOutput(jobId, name) {
    try {
      const blob = await Auth.apiGetBlob(`/dataimport/preingest/${jobId}/download/`);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = name; document.body.appendChild(a); a.click();
      a.remove(); URL.revokeObjectURL(url);
    } catch (e) {
      alert('Download failed: ' + (e.message || e));
    }
  }
})();
