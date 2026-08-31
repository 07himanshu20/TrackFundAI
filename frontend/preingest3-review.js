/* Pre-Ingestion Review Gate — standalone page logic.
 * Binds every UI state to a CIR field the engine actually produces (attributed /
 * held / read_error / gap), drives the async run + the alias write-back loop, and
 * lets a finance user click any emitted number to see its source → sheet → cell.
 */
(function () {
  'use strict';
  const API = '/dataimport/preingest3';
  const $ = (id) => document.getElementById(id);
  const esc = (s) => { const d = document.createElement('div'); d.textContent = String(s ?? ''); return d.innerHTML; };
  const notify = (m, t) => { try { window.Toast && Toast.show(m, t || 'info'); } catch (e) { /* no-op */ } };
  const fmt = (v) => (typeof v === 'number' ? v.toLocaleString('en-IN', { maximumFractionDigits: 2 }) : esc(v));

  let selected = [];      // client-side File[] before upload
  let jobId = null;
  let poll = null;
  let PROV = [];          // provenance objects, referenced by index (no attr-escaping hazard)
  let currentReport = null;  // U6 uncovered-currency report from the last run (drives the rate prompt)

  if (window.Auth && Auth.requireAuth) Auth.requireAuth();
  (function () { const u = window.Auth && Auth.getUser && Auth.getUser(); if (u) $('user-badge').textContent = u.email || u.username || '—'; })();
  $('btn-logout').onclick = () => { try { Auth.logout(); } catch (e) { localStorage.clear(); location.href = 'login.html'; } };

  // ── upload queue (pre-run, client-side) ──
  const drop = $('drop'), fi = $('file-input');
  $('browse').onclick = (e) => { e.stopPropagation(); fi.click(); };
  drop.addEventListener('click', (e) => { if (e.target.id !== 'browse') fi.click(); });
  ['dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('drag-over'); }));
  ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('drag-over'); }));
  drop.addEventListener('drop', (e) => addFiles(e.dataTransfer.files));
  fi.addEventListener('change', () => { addFiles(fi.files); fi.value = ''; });

  function addFiles(list) {
    for (const f of list) { if (/\.(xlsx|xls)$/i.test(f.name)) selected.push(f); }
    renderQueue();
  }

  function renderQueue() {
    if (jobId) return;                       // after a run, server files are shown instead
    $('queue').innerHTML = selected.map((f, i) => `
      <div class="pi3-file-row">
        <span class="pi3-dot" style="background:var(--text3)"></span>
        <span class="nm">${esc(f.name)}</span>
        <span class="pi3-muted">${(f.size / 1024).toFixed(0)} KB</span>
        <button class="v5-btn v5-btn-ghost" data-rm="${i}">Remove</button>
      </div>`).join('');
    $('queue').querySelectorAll('[data-rm]').forEach((b) => b.onclick = () => {
      selected.splice(+b.dataset.rm, 1); renderQueue();
    });
    $('btn-run').disabled = selected.length === 0;
    $('upload-hint').textContent = selected.length ? `${selected.length} file(s) ready` : '';
  }

  $('btn-run').onclick = async () => {
    if (!selected.length) return;
    const fd = new FormData();
    selected.forEach((f) => fd.append('files', f, f.name));
    $('btn-run').disabled = true; $('upload-hint').textContent = 'Uploading…';
    try {
      const r = await Auth.apiUpload(API + '/', fd);
      jobId = r.job_id; selected = []; startPolling();
    } catch (e) { notify('Upload failed: ' + e.message, 'error'); $('btn-run').disabled = false; }
  };

  // ── async run polling ──
  function startPolling() {
    $('progress-panel').style.display = 'block';
    clearInterval(poll);
    poll = setInterval(refresh, 1500);
    refresh();
  }

  async function refresh() {
    if (!jobId) return;
    let d;
    try { d = await Auth.apiGet(`${API}/${jobId}/`); } catch (e) { return; }
    $('progress-pct').textContent = (d.progress_pct || 0) + '%';
    $('progress-bar').style.width = (d.progress_pct || 0) + '%';
    $('progress-msg').textContent = d.progress_message || 'Working…';
    renderServerFiles(d.input_files || []);
    if (['completed', 'completed_with_errors', 'failed'].includes(d.status)) {
      clearInterval(poll);
      $('progress-panel').style.display = 'none';
      if (d.status === 'failed') notify('Run failed: ' + (d.progress_message || ''), 'error');
      render(d);
    }
  }

  async function rerun() {
    try { await Auth.apiPost(`${API}/${jobId}/run/`, {}); startPolling(); }
    catch (e) { notify('Re-run failed: ' + e.message, 'error'); }
  }

  // ── server-side file list (post-upload) with delete ──
  function renderServerFiles(files) {
    $('queue').innerHTML = files.map((f) => `
      <div class="pi3-file-row">
        <span class="pi3-dot" style="background:var(--accent)"></span>
        <span class="nm">${esc(f.name)}</span>
        <button class="v5-btn v5-btn-ghost" data-del="${esc(f.file_id)}">Delete</button>
      </div>`).join('');
    $('btn-run').style.display = 'none';
    $('upload-hint').textContent = 'Drop more files above to start a new run.';
    $('queue').querySelectorAll('[data-del]').forEach((b) => b.onclick = async () => {
      if (!confirm('Delete this file from the run?')) return;
      try { await Auth.apiDelete(`${API}/files/${b.dataset.del}/`); notify('File deleted — re-running', 'success'); rerun(); }
      catch (e) { notify('Delete failed: ' + e.message, 'error'); }
    });
  }

  // ── results render ──
  function render(d) {
    const rv = d.review || {};
    const c = rv.counts || {};
    $('results').style.display = 'block';
    $('btn-download').disabled = !d.has_output;
    $('kpis').innerHTML = [
      ['Files', c.files], ['Attributed', c.attributed], ['Held for review', c.held_files],
      ['Companies', c.companies], ['Read errors', c.read_errors],
    ].map(([l, v]) => `<div class="pi3-kpi"><div class="lab">${l}</div><div class="val">${v ?? 0}</div></div>`).join('');
    renderFiles(rv.files || []);
    currentReport = rv.currency_report || null;
    renderCurrencyPrompt(currentReport);
    renderReview(rv.review_queue || []);
    renderCompanies(rv.companies || []);
    renderDisclosures(rv.disclosures || []);
  }

  // ── U6 currency-coverage prompt — conditional on detection (never an always-rule) ──
  // REQUESTS a rate for each uncovered currency (the fail-closed manual gate; the server re-validates);
  // DISCLOSES foreign exposure a rate cannot surface (inform, not request). Skipping keeps figures HELD.
  function renderCurrencyPrompt(report) {
    const panel = $('ccy-panel'), body = $('ccy-body');
    const uncovered = (report && report.uncovered) || [];
    const fdu = (report && report.foreign_domicile_unresolved) || [];
    const fdcu = (report && report.foreign_domicile_currency_unmapped) || [];
    if (!uncovered.length && !fdu.length && !fdcu.length) { panel.style.display = 'none'; return; }
    panel.style.display = 'block';
    const asOf = (report && report.as_of) || '';

    const reqRows = uncovered.map((u) => {
      const sites = (u.sites || []).map((s) => esc(s.entity || s.file || '')).filter(Boolean).join(', ');
      return `<div class="pi3-rate-row">
          <span class="ccy">${esc(u.currency)}</span>
          <input type="number" step="any" min="0" placeholder="INR per 1 ${esc(u.currency)}" data-ccy="${esc(u.currency)}" />
          <input type="text" placeholder="source (e.g. RBI reference)" data-src="${esc(u.currency)}" />
          ${sites ? `<div class="pi3-ccy-sites" style="grid-column:1/-1;">affects: ${sites}</div>` : ''}
        </div>`;
    }).join('');

    const request = uncovered.length ? `
      <div class="pi3-ccy-intro">These figures reported in a foreign currency and no rate is on file. Enter
        the exchange rate <b>as of ${esc(asOf)}</b> to convert and emit them — anything you skip stays
        <b>held</b>, never guessed. Manual entry and an uploaded rate schedule are equally trusted; both are
        validated before anything converts.</div>
      ${reqRows}
      <div class="pi3-ccy-actions">
        <button class="v5-btn v5-btn-primary" id="ccy-submit">Supply rates &amp; re-run</button>
        <label class="v5-btn v5-btn-ghost" style="cursor:pointer;">&#8681; Upload a rate schedule instead
          <input type="file" id="ccy-file" accept=".xlsx,.xls" style="display:none;" /></label>
        <span class="pi3-muted" id="ccy-hint"></span>
      </div>` : '';

    const items = fdu.map((e) =>
        `<div><b>${esc(e.entity)}</b> (${esc(e.implied_currency || '')}) — held upstream; a rate won't surface it.
          Resolve the statement separately.</div>`)
      .concat(fdcu.map((e) =>
        `<div><b>${esc(e.entity)}</b> — domicile <i>${esc(e.domicile || '')}</i> maps to no known currency.
          Add the domicile&rarr;currency mapping, then a rate.</div>`));
    const disclose = items.length
      ? `<div class="pi3-ccy-disclose"><b>Also foreign, but a rate won't help — for your awareness:</b>${items.join('')}</div>`
      : '';

    body.innerHTML = request + disclose;
    if (uncovered.length) {
      $('ccy-submit').onclick = submitManualRates;
      $('ccy-file').onchange = (ev) => { if (ev.target.files[0]) submitScheduleFile(ev.target.files[0]); };
    }
  }

  async function submitManualRates() {
    const asOf = currentReport && currentReport.as_of;
    const rows = [];
    document.querySelectorAll('#ccy-body [data-ccy]').forEach((inp) => {
      const ccy = inp.dataset.ccy, rate = String(inp.value || '').trim();
      const srcEl = document.querySelector(`#ccy-body [data-src="${ccy}"]`);
      const src = srcEl ? String(srcEl.value || '').trim() : '';
      if (rate) rows.push({ currency: ccy, rate: rate, date: asOf, source: src });
    });
    if (!rows.length) { notify('Enter at least one rate', 'error'); return; }
    $('ccy-submit').disabled = true; $('ccy-hint').textContent = 'Validating…';
    try {
      afterCardAccepted(await Auth.apiPost(`${API}/${jobId}/ratecard/`, { manual_rates: rows }));
    } catch (e) {
      $('ccy-submit').disabled = false; $('ccy-hint').textContent = '';
      notify('Rate card refused: ' + (e.message || ''), 'error');   // the gate's reason surfaces verbatim
    }
  }

  async function submitScheduleFile(file) {
    const fd = new FormData();
    fd.append('schedule_file', file, file.name);
    $('ccy-hint').textContent = 'Reading schedule…';
    try {
      afterCardAccepted(await Auth.apiUpload(`${API}/${jobId}/ratecard/`, fd));
    } catch (e) { $('ccy-hint').textContent = ''; notify('Schedule refused: ' + (e.message || ''), 'error'); }
  }

  function afterCardAccepted(r) {
    const noop = (r.noop_currencies || []).map((n) => n.currency).join(', ');
    const still = (r.still_uncovered || []).join(', ');
    let msg = 'Rate card accepted — re-running';
    if (noop) msg += ` · noted, not needed this run: ${noop}`;
    if (still) msg += ` · still uncovered: ${still}`;
    notify(msg, still ? 'info' : 'success');
    rerun();
  }

  function statusClass(s) { return s === 'attributed' ? 'active' : s === 'read_error' ? 'rejected' : 'pending'; }
  function dotColor(s) {
    return s === 'attributed' ? 'var(--green,#10b981)' : s === 'read_error' ? 'var(--red,#ef4444)'
      : s === 'held' ? 'var(--gold,#f59e0b)' : 'var(--text3)';
  }

  function renderFiles(files) {
    $('files-list').innerHTML = files.length ? files.map((f) => `
      <div class="pi3-file-row">
        <span class="pi3-dot" style="background:${dotColor(f.status)}"></span>
        <span class="nm">${esc(f.label)} <span class="pi3-muted">· ${esc(f.role)}</span></span>
        <span class="v5-status ${statusClass(f.status)}">${esc(f.status)}</span>
        <span class="pi3-muted" style="flex:2;text-align:right;">${esc(f.reason || f.entity_id || '')}</span>
      </div>`).join('') : '<div class="pi3-muted">No files.</div>';
  }

  function renderReview(queue) {
    const panel = $('review-panel');
    if (!queue.length) { panel.style.display = 'none'; return; }
    panel.style.display = 'block';
    $('review-list').innerHTML = queue.map((rf, i) => `
      <div class="pi3-resolve-row">
        <div><b>${esc(rf.label)}</b><div class="pi3-muted">${esc(rf.reason)}</div></div>
        <select class="v5-select" id="cand-${i}">
          <option value="">— choose company —</option>
          ${(rf.candidates || []).map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')}
        </select>
        <button class="v5-btn v5-btn-primary" data-cf="${i}">Confirm</button>
      </div>`).join('');
    $('review-list').querySelectorAll('[data-cf]').forEach((b) => b.onclick = () => confirmAlias(queue[+b.dataset.cf], +b.dataset.cf));
  }

  async function confirmAlias(rf, i) {
    const entity_id = $('cand-' + i).value;      // the candidate.id (normalised anchor key)
    if (!entity_id) { notify('Pick a company first', 'error'); return; }
    try {
      await Auth.apiPost(`${API}/${jobId}/resolve/`, { identifiers: rf.identifiers, entity_id });
      notify('Alias confirmed — re-running', 'success');
      rerun();
    } catch (e) { notify('Resolve failed: ' + e.message, 'error'); }
  }

  function figCell(fj) {
    if (!fj) return '<span class="pi3-gap">—</span>';
    if (fj.state === 'held') return `<span class="pi3-held" title="${esc(fj.reason)}">⚠ HELD</span>`;
    if (fj.state === 'gap' || fj.value_cr === null) return '<span class="pi3-gap">—</span>';
    const idx = PROV.push(fj) - 1;
    return `<span class="pi3-num" data-pi="${idx}">${fmt(fj.value_cr)}</span>`;
  }

  function renderCompanies(rows) {
    PROV = [];
    $('companies-body').innerHTML = rows.length ? rows.map((r) => {
      const g = (k) => figCell(r.figures[k]);
      return `<tr><td class="td-bold">${esc(r.company)}</td>
        <td class="td-right">${g('revenue')}</td>
        <td class="td-right">${g('ebitda')}</td>
        <td class="td-right">${g('cash')}</td>
        <td class="td-right">${g('headcount')}</td></tr>`;
    }).join('') : '<tr><td colspan="5" style="text-align:center;color:var(--text3);padding:16px;">No companies extracted yet.</td></tr>';
    $('companies-body').querySelectorAll('.pi3-num[data-pi]').forEach((el) =>
      el.onclick = (e) => showProv(e, PROV[+el.dataset.pi]));
  }

  function renderDisclosures(rows) {
    $('disclosures-body').innerHTML = rows.length ? rows.map((d) => `
      <tr><td>${esc(d.kind)}</td><td>${esc(d.entity)}</td><td>${esc(d.detail)}</td></tr>`).join('')
      : '<tr><td colspan="3" style="text-align:center;color:var(--text3);padding:12px;">None.</td></tr>';
  }

  // ── provenance popover (the trust feature) ──
  let popEl = null;
  function hideProv() { if (popEl) { popEl.remove(); popEl = null; } }
  function showProv(e, fj) {
    hideProv();
    popEl = document.createElement('div');
    popEl.className = 'pi3-pop';
    popEl.innerHTML = `<div><b>${esc(fj.concept)}</b> = ₹${fmt(fj.value_cr)} Cr</div>
      <div style="margin-top:5px;">source: <span class="mono">${esc(fj.source || '—')}</span></div>
      <div>cell: <span class="mono">${esc(fj.sheet || '')}${fj.cell ? ('!' + esc(fj.cell)) : ''}</span></div>
      ${fj.row_label ? `<div>row label: ${esc(fj.row_label)}</div>` : ''}
      <div>basis: ${esc(fj.basis || '—')}${fj.months ? (' · ' + fj.months + 'm') : ''}</div>`;
    document.body.appendChild(popEl);
    const r = e.target.getBoundingClientRect();
    popEl.style.left = Math.min(r.left, window.innerWidth - 340) + 'px';
    popEl.style.top = (r.bottom + 6) + 'px';
    setTimeout(() => document.addEventListener('click', hideProv, { once: true }), 0);
  }

  $('btn-download').onclick = async () => {
    try {
      const blob = await Auth.apiGetBlob(`${API}/${jobId}/download/`);
      const u = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = u; a.download = 'TFAI.xlsx'; a.click();
      URL.revokeObjectURL(u);
    } catch (e) { notify('Download failed: ' + e.message, 'error'); }
  };
})();
