/* Consolidation v2 (document architecture) — testing UI. */
(function () {
  if (!window.Auth || !Auth.requireAuth()) return;
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const cls = (st) => st === 'pass' ? 'ok' : (st === 'fail' ? 'fail' : 'warn');

  const bl = $('btn-logout'); if (bl) bl.onclick = () => Auth.logout();
  let files = [], timer = null;
  const drop = $('drop'), input = $('fileinput'), listEl = $('filelist'), btnRun = $('btn-run');

  drop.onclick = () => input.click();
  ['dragover', 'dragenter'].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add('drag'); }));
  ['dragleave', 'drop'].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove('drag'); }));
  drop.addEventListener('drop', (ev) => add(ev.dataTransfer.files));
  input.addEventListener('change', () => add(input.files));

  function add(fl) {
    for (const f of fl) {
      if (!/\.(xlsx|xls)$/i.test(f.name)) continue;
      if (!files.some((x) => x.name === f.name && x.size === f.size)) files.push(f);
    }
    render();
  }
  function render() {
    listEl.innerHTML = files.map((f, i) =>
      `<div class="pi-fileitem"><span>📄 ${esc(f.name)} <span class="muted">(${(f.size / 1024).toFixed(0)} KB)</span></span><button data-i="${i}">✕</button></div>`).join('');
    listEl.querySelectorAll('button').forEach((b) => b.onclick = () => { files.splice(+b.dataset.i, 1); render(); });
    btnRun.disabled = !files.length;
    $('run-hint').textContent = files.length ? `${files.length} file(s) ready` : '';
  }

  btnRun.onclick = async () => {
    btnRun.disabled = true;
    const fd = new FormData(); files.forEach((f) => fd.append('files', f));
    try {
      const res = await Auth.apiUpload('/dataimport/preingest2/', fd);
      $('progress-card').style.display = 'block'; $('result-card').style.display = 'none';
      poll(res.job_id);
    } catch (e) { alert('Upload failed: ' + (e.message || e)); btnRun.disabled = false; }
  };

  function poll(id) {
    if (timer) clearInterval(timer);
    timer = setInterval(async () => {
      let j; try { j = await Auth.apiGet(`/dataimport/preingest2/${id}/`); } catch (e) { return; }
      $('prog-fill').style.width = (j.progress_pct || 0) + '%';
      $('prog-pct').textContent = (j.progress_pct || 0) + '%';
      $('prog-msg').textContent = j.progress_message || j.status;
      if (['completed', 'completed_with_errors', 'failed'].includes(j.status)) {
        clearInterval(timer); btnRun.disabled = false; result(j);
      }
    }, 1500);
  }

  function result(j) {
    const card = $('result-card'); card.style.display = 'block';
    if (j.status === 'failed') { card.innerHTML = `<h1 class="pi-h1">Failed</h1><p class="fail">${esc(j.progress_message)}</p>`; return; }
    const s = j.summary || {};
    const rc = s.sheet_row_counts || {};
    const populated = Object.values(rc).filter((c) => c).length;
    const sheets = Object.keys(rc).map((n) => `<tr><td>${esc(n)}</td><td class="${rc[n] ? '' : 'muted'}">${rc[n]}</td></tr>`).join('');
    const audit = (s.audit || []).map((a) => `<tr><td>${esc(a.file)}</td><td>${esc(a.file_class)}</td><td>${esc(a.cache)}</td><td class="${a.status === 'ok' ? 'ok' : 'fail'}">${esc(a.status)}</td></tr>`).join('');
    const recon = (s.reconciliation || []).concat(s.verification || []).map((r) => `<tr><td class="${cls(r.status)}">${esc(r.status)}</td><td>${esc(r.check)}</td><td class="muted">${esc(r.detail)}</td></tr>`).join('');

    card.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px">
        <h1 class="pi-h1" style="margin:0">Fund Master Workbook ready ${s.blocked ? '<span class="warn">(review flags)</span>' : '<span class="ok">✓ verified</span>'}</h1>
        <button id="btn-dl" class="pi-btn">⬇ Download TFAI.xlsx</button>
      </div>
      <div class="pi-grid" style="margin:16px 0">
        <div class="pi-stat"><div class="n">${s.files || 0}</div><div class="l">Files</div></div>
        <div class="pi-stat"><div class="n">${s.company_rows || 0}</div><div class="l">Portfolio companies</div></div>
        <div class="pi-stat"><div class="n">${populated}/${Object.keys(rc).length}</div><div class="l">Sheets filled</div></div>
        <div class="pi-stat"><div class="n">${(s.cache || {}).frozen_records || 0}</div><div class="l">Cached (frozen)</div></div>
      </div>
      <div style="display:flex;gap:20px;flex-wrap:wrap">
        <div style="flex:1;min-width:240px"><h3 style="margin:8px 0 4px">Output sheets (rows)</h3>
          <table class="pi-tbl"><thead><tr><th>Sheet</th><th>Rows</th></tr></thead><tbody>${sheets}</tbody></table></div>
        <div style="flex:1;min-width:240px"><h3 style="margin:8px 0 4px">Per-file (S2/S3 + cache)</h3>
          <table class="pi-tbl"><thead><tr><th>File</th><th>Class</th><th>Cache</th><th>Status</th></tr></thead><tbody>${audit}</tbody></table></div>
      </div>
      <h3 style="margin:18px 0 4px">Reconciliation & Verification (S6 · S8)</h3>
      <table class="pi-tbl"><thead><tr><th>Status</th><th>Check</th><th>Detail</th></tr></thead><tbody>${recon}</tbody></table>
      <p class="pi-note" style="margin-top:12px">Re-running the same files serves frozen records from cache (no model calls) → byte-stable output. See the <b>_Reconciliation</b> tab in the workbook.</p>`;
    $('btn-dl').onclick = () => dl(j.job_id);
  }

  async function dl(id) {
    try {
      const blob = await Auth.apiGetBlob(`/dataimport/preingest2/${id}/download/`);
      const url = URL.createObjectURL(blob); const a = document.createElement('a');
      a.href = url; a.download = 'TFAI.xlsx'; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    } catch (e) { alert('Download failed: ' + (e.message || e)); }
  }
})();
