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
  let running = false;    // a run is in flight — every re-run trigger is disabled while true
  let PROV = [];          // provenance objects, referenced by index (no attr-escaping hazard)
  let currentReport = null;  // U6 uncovered-currency report from the last run (drives the rate prompt)
  let currentBase = null;    // the fund base currency applied to the last run (persisted run input)
  // The foreign currencies the CURRENT run is converting into ₹, with the companies each one affects.
  // Populated ONLY from real backend data — the rates the user supplied + the report's own 'sites' — so
  // the progress statements name exactly what is being converted; never a fabricated or guessed line.
  let convertingCurrencies = [];

  // ISO code → the plain currency name a finance reader recognises (falls back to the bare code). Display
  // only — it changes no value, and an unknown code still shows, never hidden.
  const CCY_NAMES = {
    MYR: 'Malaysian Ringgit', SGD: 'Singapore Dollar', USD: 'US Dollar', EUR: 'Euro',
    GBP: 'British Pound', AED: 'UAE Dirham', JPY: 'Japanese Yen', CNY: 'Chinese Yuan',
    HKD: 'Hong Kong Dollar', AUD: 'Australian Dollar', CAD: 'Canadian Dollar', CHF: 'Swiss Franc',
    THB: 'Thai Baht', IDR: 'Indonesian Rupiah', PHP: 'Philippine Peso', LKR: 'Sri Lankan Rupee',
    NPR: 'Nepalese Rupee', BDT: 'Bangladeshi Taka', SAR: 'Saudi Riyal', QAR: 'Qatari Riyal',
    OMR: 'Omani Rial', KWD: 'Kuwaiti Dinar', ZAR: 'South African Rand', NZD: 'New Zealand Dollar',
  };
  const ccyName = (code) => {
    const c = String(code || '').toUpperCase();
    return CCY_NAMES[c] ? `${CCY_NAMES[c]} (${c})` : c;
  };

  // Translate the backend's REAL progress percentage into a plain-English line for the finance team.
  // Each line maps 1:1 to a genuine engine stage boundary (2/5/25/40/45/60/90/100) — no invented steps,
  // no timer; the % it is keyed on comes straight from the job's progress_pct.
  function friendlyStage(pct) {
    const p = Number(pct) || 0;
    if (p >= 100) return 'Done — your consolidated file is ready.';
    if (p >= 90) return 'Putting together your consolidated workbook…';
    if (p >= 60) return 'Reading each company’s figures and converting foreign amounts into ₹…';
    if (p >= 45) return 'Matching each company to its figures…';
    if (p >= 40) return 'Reading the fund’s financial statements…';
    if (p >= 25) return 'Organising the fund’s investment list…';
    if (p >= 5) return 'Reading your uploaded files…';
    return 'Getting things ready…';
  }

  // The currency-conversion statements shown under the bar while a run is in flight. Lists exactly the
  // currencies this run converts and the companies each affects — the real information a finance reviewer
  // wants to watch. Hidden once done (100%) or when nothing foreign is being converted.
  function renderProgressDetail(pct) {
    const el = $('progress-detail');
    if (!el) return;
    const p = Number(pct) || 0;
    if (!convertingCurrencies.length || p >= 100) { el.style.display = 'none'; el.innerHTML = ''; return; }
    el.style.display = 'block';
    el.innerHTML = '<div class="pi3-prog-detail-h">Converting foreign-currency figures into ₹ (Crore):</div>' +
      convertingCurrencies.map((c) => {
        const who = (c.entities && c.entities.length)
          ? ` — for ${c.entities.map(esc).join(', ')}` : '';
        return `<div class="pi3-prog-detail-line"><b>${esc(ccyName(c.code))}</b> &rarr; ₹${who}</div>`;
      }).join('');
  }

  // Place the single progress panel where it belongs for the current action: during the foreign-currency
  // flow (the rate prompt is on screen) put it DIRECTLY BELOW that prompt so the finance user sees the bar
  // in context; otherwise keep it at its home position (above the results). Pure DOM relocation of the one
  // panel — one bar, one set of ids, no duplication.
  function positionProgressPanel() {
    const panel = $('progress-panel'), ccy = $('ccy-panel');
    if (ccy && ccy.style.display && ccy.style.display !== 'none') {
      ccy.insertAdjacentElement('afterend', panel);          // below 'Foreign currency detected…'
    } else {
      $('results').insertAdjacentElement('beforebegin', panel);   // home (above results)
    }
  }

  // Show the progress panel immediately (before the run's own polling begins) so a click never lands on a
  // dead 'Validating…' — the finance user sees continuous, honest feedback from the first moment.
  function showProgress(msg, pct) {
    const p = Number(pct) || 0;
    positionProgressPanel();
    $('progress-panel').style.display = 'block';
    $('progress-msg').textContent = msg;
    $('progress-pct').textContent = p + '%';
    $('progress-bar').style.width = p + '%';
    renderProgressDetail(p);
  }

  // ── cross-refresh persistence ──
  // The backend ALREADY persists every run (PreIngestJob + its uploaded files + results); only this
  // in-memory pointer was lost on a hard refresh, which is why the screen went blank. Remember the active
  // job id so a refresh can re-attach and restore the view. localStorage is per-browser and best-effort —
  // every access is guarded so storage being off/full/blocked can never break the page.
  const JOB_KEY = 'pi3_active_job';
  function saveJob(id) { try { localStorage.setItem(JOB_KEY, id); } catch (e) { /* storage unavailable */ } }
  function clearJob() { try { localStorage.removeItem(JOB_KEY); } catch (e) { /* no-op */ } }

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
    convertingCurrencies = [];   // a brand-new run converts nothing until a rate card is supplied
    try {
      const r = await Auth.apiUpload(API + '/', fd);
      jobId = r.job_id; saveJob(jobId); selected = []; startPolling();
    } catch (e) { notify('Upload failed: ' + e.message, 'error'); $('btn-run').disabled = false; }
  };

  // ── async run polling ──
  // A run is in flight from the moment we trigger/attach until a terminal status returns.
  // setRunning() is the single source of truth: it flips the guard AND greys out every control
  // that would fire another /run/ (Confirm, Delete, rate-card, base-currency), so a duplicate
  // re-run can never be launched against a job that is already processing.
  function setRunning(on) {
    running = on;
    document.querySelectorAll('[data-rerun-trigger]').forEach((el) => { el.disabled = on; });
    const res = $('results');
    if (res) res.classList.toggle('pi3-run-busy', on);
  }

  function startPolling() {
    setRunning(true);
    $('btn-new-run').style.display = '';        // a job is active → offer the explicit reset
    $('btn-proceed').style.display = 'none';    // not ready to hand off until this run completes
    positionProgressPanel();                    // below the currency prompt during the FX flow, else home
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
    $('progress-msg').textContent = friendlyStage(d.progress_pct);   // plain English, keyed on the REAL %
    renderProgressDetail(d.progress_pct);                            // which currencies/companies are converting
    running = !['completed', 'completed_with_errors', 'failed'].includes(d.status);
    renderServerFiles(d.input_files || []);
    if (!running) {
      clearInterval(poll);
      $('progress-panel').style.display = 'none';
      setRunning(false);
      if (d.status === 'failed') notify('Run failed: ' + (d.progress_message || ''), 'error');
      render(d);
    }
  }

  // Never dead-ends: a fresh run starts (200) OR the server says one is already in flight
  // (202) — both mean "watch this run", so we just poll. A 409 (or any 'already running')
  // is likewise treated as attach-and-poll, not a hard error. The guard blocks re-entry.
  async function rerun() {
    if (running) { startPolling(); return; }
    try { await Auth.apiPost(`${API}/${jobId}/run/`, {}); startPolling(); }
    catch (e) {
      if (String(e.message).includes('→ 409')) { startPolling(); return; }
      notify('Re-run failed: ' + e.message, 'error');
    }
  }

  // ── restore the last job across a hard refresh ──
  // Re-attach to the saved job and re-render its state — results if finished, or resume the live bar if
  // still processing — so uploaded files, progress and processed data survive a refresh. Fully guarded: a
  // gone / other-org job (404), an API error, or storage being off all fall back to the normal empty
  // upload state, never a broken page. Reuses the SAME render/poll functions a live run uses (no new paths).
  async function restoreLastJob() {
    let saved = null;
    try { saved = localStorage.getItem(JOB_KEY); } catch (e) { return; }
    if (!saved) return;
    let d;
    try { d = await Auth.apiGet(`${API}/${saved}/`); }
    catch (e) { clearJob(); return; }           // deleted / another org / gone → clear, show empty upload
    jobId = saved;
    $('btn-new-run').style.display = '';
    renderServerFiles(d.input_files || []);
    if (['completed', 'completed_with_errors', 'failed'].includes(d.status)) {
      render(d);                                 // identical to what refresh() renders on completion
      if (d.status === 'failed') notify('Last run failed: ' + (d.progress_message || ''), 'error');
    } else {
      startPolling();                            // still processing → resume the live progress bar
    }
  }

  // Explicit 'start over' — the replacement for the old 'hard-refresh to reset' behaviour, now that a
  // refresh RESTORES instead of clearing. Non-destructive by design: it clears only the local pointer and
  // resets the view to a fresh upload; the previous run stays saved on the server (remove its files with the
  // per-file Delete when you actually want them gone). Never touches the backend.
  function startNewRun() {
    clearInterval(poll);
    clearJob();
    jobId = null; selected = []; running = false;
    currentReport = null; currentBase = null; convertingCurrencies = [];
    setRunning(false);
    $('results').style.display = 'none';
    $('progress-panel').style.display = 'none';
    $('btn-new-run').style.display = 'none';
    $('btn-proceed').style.display = 'none';
    $('btn-download').disabled = true;
    $('queue').innerHTML = '';
    $('btn-run').style.display = '';             // renderServerFiles had hidden it; bring it back
    $('upload-hint').textContent = '';
    renderQueue();                               // jobId is null again → the empty client-side queue
  }

  // ── server-side file list (post-upload) with delete ──
  function renderServerFiles(files) {
    $('queue').innerHTML = files.map((f) => `
      <div class="pi3-file-row">
        <span class="pi3-dot" style="background:var(--accent)"></span>
        <span class="nm">${esc(f.name)}</span>
        <button class="v5-btn v5-btn-ghost" data-rerun-trigger data-del="${esc(f.file_id)}">Delete</button>
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
    $('btn-new-run').style.display = '';        // results are showing → keep the reset available
    $('btn-download').disabled = !d.has_output;
    $('btn-proceed').style.display = d.has_output ? '' : 'none';   // hand-off to the dashboard once ready
    $('kpis').innerHTML = [
      ['Files', c.files], ['Attributed', c.attributed], ['Held for review', c.held_files],
      ['Companies', c.companies], ['Read errors', c.read_errors],
    ].map(([l, v]) => `<div class="pi3-kpi"><div class="lab">${l}</div><div class="val">${v ?? 0}</div></div>`).join('');
    renderFiles(rv.files || []);
    currentReport = rv.currency_report || null;
    currentBase = rv.base_currency || null;
    renderBaseCurrency(currentReport, currentBase);
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
    // Record exactly what this run will convert — the currencies the user just priced, each tied to the
    // companies the backend's report says it affects. Real data only; drives the on-bar statements.
    convertingCurrencies = rows.map((r) => {
      const u = ((currentReport && currentReport.uncovered) || []).find((x) => x.currency === r.currency) || {};
      const entities = (u.sites || []).map((s) => s.entity || s.file || '').filter(Boolean);
      return { code: r.currency, entities };
    });
    $('ccy-submit').disabled = true; $('ccy-hint').textContent = '';
    showProgress('Checking the exchange rates you entered…', 0);   // continuous feedback from the first click
    try {
      afterCardAccepted(await Auth.apiPost(`${API}/${jobId}/ratecard/`, { manual_rates: rows }));
    } catch (e) {
      $('ccy-submit').disabled = false; $('ccy-hint').textContent = '';
      $('progress-panel').style.display = 'none';   // validation refused → no re-run; take the bar back down
      notify('Rate card refused: ' + (e.message || ''), 'error');   // the gate's reason surfaces verbatim
    }
  }

  async function submitScheduleFile(file) {
    const fd = new FormData();
    fd.append('schedule_file', file, file.name);
    // The schedule is priced server-side; the currencies it is meant to cover are the report's uncovered
    // set, each with the companies it affects — real data for the on-bar statements.
    convertingCurrencies = ((currentReport && currentReport.uncovered) || []).map((u) => ({
      code: u.currency, entities: (u.sites || []).map((s) => s.entity || s.file || '').filter(Boolean),
    }));
    $('ccy-hint').textContent = '';
    showProgress('Reading your rate schedule…', 0);
    try {
      afterCardAccepted(await Auth.apiUpload(`${API}/${jobId}/ratecard/`, fd));
    } catch (e) {
      $('ccy-hint').textContent = '';
      $('progress-panel').style.display = 'none';   // refused → no re-run; hide the bar
      notify('Schedule refused: ' + (e.message || ''), 'error');
    }
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

  // ── base reporting currency — the INR twin of the foreign-rate box ──
  // Two roles from ONE report: (A) REVIEW the figures resolved via the confirmed base (condition #2 — a
  // foreign company with no marker must never be silently swept in), and (B) offer the CONFIRM when figures
  // are held for want of any in-file currency evidence. A file carrying its own foreign currency is held by
  // the engine's conflict guard regardless — the base never overrides file evidence, only fills its absence.
  function renderBaseCurrency(report, base) {
    const panel = $('base-ccy-panel'), body = $('base-ccy-body');
    const ambiguous = (report && report.ambiguous) || [];          // held: NO positive currency evidence
    const applied = (report && report.base_currency_applied) || []; // resolved VIA the confirmed base
    if (!ambiguous.length && !applied.length) { panel.style.display = 'none'; return; }
    panel.style.display = 'block';
    let html = '';

    if (applied.length) {   // (A) review surface — one human glance per fund-base-resolved site
      const sites = applied.map((s) => esc(s.entity || s.file || '')).filter(Boolean).join(', ');
      html += `<div class="pi3-base-applied">
          <b>${applied.length} file${applied.length > 1 ? 's were' : ' was'} resolved using your confirmed
          base currency (${esc(base || 'INR')}).</b> Review that none is a foreign company mis-read as
          ${esc(base || 'INR')} — a statement carrying its own foreign currency is held automatically, but a
          foreign company with no marker at all would surface here.
          ${sites ? `<div class="pi3-ccy-sites" style="margin-top:6px;">${sites}</div>` : ''}
          <div class="pi3-ccy-actions" style="margin-top:10px;">
            <button class="v5-btn v5-btn-ghost" id="base-clear">This isn't right — clear base currency &amp; re-run</button>
          </div>
        </div>`;
    }

    if (ambiguous.length && !applied.length) {   // (B) confirm prompt — resolve the no-evidence holds
      const ents = Array.from(new Set(ambiguous.map((a) => esc(a.entity || a.file || '')).filter(Boolean))).join(', ');
      html += `<div class="pi3-ccy-intro">${ambiguous.length} figure${ambiguous.length > 1 ? 's are' : ' is'} held
          because no currency could be determined from the file itself (no symbol, no known domicile). If these
          are reported in your fund's base currency, confirm it to resolve them — anything carrying its own
          foreign currency stays <b>held</b>, never converted by assumption.</div>
        ${ents ? `<div class="pi3-ccy-sites">affects: ${ents}</div>` : ''}
        <div class="pi3-ccy-actions" style="margin-top:10px;">
          <button class="v5-btn v5-btn-primary" id="base-confirm">Confirm base currency is INR &amp; re-run</button>
          <span class="pi3-muted" id="base-hint"></span>
        </div>`;
    }

    body.innerHTML = html;
    if ($('base-confirm')) $('base-confirm').onclick = () => setBaseCurrency('INR');
    if ($('base-clear')) $('base-clear').onclick = () => setBaseCurrency('');
  }

  async function setBaseCurrency(bc) {
    const btn = $('base-confirm') || $('base-clear'), hint = $('base-hint');
    if (btn) btn.disabled = true;
    if (hint) hint.textContent = 'Applying…';
    try {
      const r = await Auth.apiPost(`${API}/${jobId}/base-currency/`, { base_currency: bc });
      notify((r.detail || 'Base currency updated') + ' — re-running', 'success');
      rerun();
    } catch (e) {
      if (btn) btn.disabled = false;
      if (hint) hint.textContent = '';
      notify('Base currency refused: ' + (e.message || ''), 'error');   // the gate's reason surfaces verbatim
    }
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
        <button class="v5-btn v5-btn-primary" data-rerun-trigger data-cf="${i}">Confirm</button>
      </div>`).join('');
    $('review-list').querySelectorAll('[data-cf]').forEach((b) => b.onclick = () => confirmAlias(queue[+b.dataset.cf], +b.dataset.cf));
  }

  async function confirmAlias(rf, i) {
    if (running) return;                          // a run is already in flight — ignore
    const entity_id = $('cand-' + i).value;      // the candidate.id (normalised anchor key)
    if (!entity_id) { notify('Pick a company first', 'error'); return; }
    setRunning(true);                            // close the resolve round-trip against a double-click
    try {
      await Auth.apiPost(`${API}/${jobId}/resolve/`, { identifiers: rf.identifiers, entity_id });
      notify('Alias confirmed — re-running', 'success');
      rerun();
    } catch (e) { setRunning(false); notify('Resolve failed: ' + e.message, 'error'); }
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

  // Proceed to dashboard: hand this run's consolidated workbook to the post-ingestion 'Data Ingestion'
  // tab, which fetches it and auto-imports it (no user click). The pre-ingestion job id rides on the URL;
  // the workbook itself is pulled server-side from that job's output, so no large file crosses via storage.
  $('btn-proceed').onclick = () => {
    if (!jobId) return;
    // Persist the hand-off intent so the Data Ingestion page auto-imports even if the URL param is
    // dropped by an auth-refresh redirect or a back/forward-cache restore. The param is kept too as
    // the primary signal; data-upload.js reads whichever is present.
    try { sessionStorage.setItem('tfai_pi3_ingest', jobId); } catch (e) { /* storage unavailable */ }
    location.href = 'data-upload.html?ingest=' + encodeURIComponent(jobId);
  };

  $('btn-new-run').onclick = startNewRun;

  // On load, re-attach to the last job (if any) so a hard refresh never loses uploaded files or results.
  restoreLastJob();
})();
