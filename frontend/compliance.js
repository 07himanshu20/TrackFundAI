/* ============================================================
   compliance.js
   TrackFundAI Module 5 — SEBI Compliance (GP Admin View)
   SEBI Reports · AML · CTR (with checklist) · Equity Threshold Alerts · Compliance Calendar
============================================================ */

(() => {
  let funds = [];
  let schemes = [];
  let investors = [];
  let investments = [];
  let reports = [];
  let amlRecords = [];
  let ctrReports = [];
  let thresholdAlerts = [];
  let calendarEvents = [];
  let ppmAmendments = [];
  let sebiCirculars = [];
  let modalCallback = null;
  let activeTab = 'reports';

  // ── Formatting ────────────────────────────────────────────
  const esc = (s) => {
    if (s === null || s === undefined) return '—';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  };

  const fmtDate = (d) => {
    if (!d) return '—';
    return new Date(d).toLocaleDateString('en-IN', {day: '2-digit', month: 'short', year: 'numeric'});
  };

  const dueDateClass = (dateStr) => {
    if (!dateStr) return '';
    const diff = new Date(dateStr) - Date.now();
    const days = Math.floor(diff / 86400000);
    if (days < 0) return 'due-urgent';
    if (days <= 14) return 'due-soon';
    return 'due-ok';
  };

  const boolCell = (v) => v
    ? '<span class="bool-yes">YES</span>'
    : '<span class="bool-no">No</span>';

  // ── Init ──────────────────────────────────────────────────
  async function init() {
    if (!Auth.requireAuth()) return;

    const user = Auth.getUser();
    document.getElementById('user-badge').textContent =
      `${user.first_name || user.username} · ${user.role.replace('_', ' ').toUpperCase()}`;
    document.getElementById('org-label').textContent =
      user.organization_name || 'Unknown Organization';

    document.getElementById('btn-logout').onclick = () => Auth.logout();

    // Tabs
    document.querySelectorAll('[data-tab]').forEach(tab => {
      tab.onclick = () => switchTab(tab.dataset.tab);
    });

    // Buttons
    document.getElementById('btn-new-report').onclick = () => openReportForm();
    document.getElementById('btn-new-aml').onclick = () => openAMLForm();
    document.getElementById('btn-new-ctr').onclick = () => openCTRForm();
    document.getElementById('btn-new-alert').onclick = () => openAlertForm();
    document.getElementById('btn-new-event').onclick = () => openCalendarForm();
    document.getElementById('btn-new-ppm').onclick = () => openPPMForm();
    document.getElementById('btn-new-circular').onclick = () => openCircularForm();

    // Filters
    document.getElementById('report-status-filter').onchange = renderReports;
    document.getElementById('aml-risk-filter').onchange = renderAML;
    document.getElementById('cal-status-filter').onchange = renderCalendar;
    document.getElementById('ppm-status-filter').onchange = renderPPM;
    document.getElementById('circular-impact-filter').onchange = renderCirculars;

    // Modal
    document.getElementById('modal-close').onclick = closeModal;
    document.getElementById('modal-cancel').onclick = closeModal;
    document.getElementById('modal-form').onsubmit = handleModalSubmit;

    await loadFunds();
    await Promise.all([loadReports(), loadAML(), loadCTR(), loadAlerts(), loadCalendar(), loadInvestors(), loadInvestments(), loadPPM(), loadCirculars()]);
    renderStats();
    loadNotifCount();
  }

  // ── Notification badge ────────────────────────────────────
  async function loadNotifCount() {
    try {
      const data = await Auth.apiGet('/notifications/unread-count/');
      const el = document.getElementById('notif-badge');
      if (el) { el.textContent = data.unread_count || 0; el.classList.toggle('zero', !data.unread_count); }
    } catch (e) { console.error('Notif count failed:', e); }
  }

  // ── Tab switching ─────────────────────────────────────────
  function switchTab(tab) {
    activeTab = tab;
    document.querySelectorAll('[data-tab]').forEach(t => {
      t.classList.toggle('active', t.dataset.tab === tab);
    });
    ['reports', 'aml', 'ctr', 'alerts', 'calendar', 'ppm', 'circulars'].forEach(t => {
      const el = document.getElementById(`tab-${t}`);
      if (el) el.classList.toggle('hidden', t !== tab);
    });
  }

  // ── Load data ─────────────────────────────────────────────
  async function loadFunds() {
    try {
      funds = await Auth.apiGet('/funds/');
      schemes = [];
      for (const f of funds) {
        const fundSchemes = await Auth.apiGet(`/funds/${f.id}/schemes/`);
        for (const s of fundSchemes) {
          schemes.push({ ...s, fund_name: f.name });
        }
      }

      // Populate fund filters
      const fundSel = document.getElementById('report-fund-filter');
      const ppmFundSel = document.getElementById('ppm-fund-filter');
      funds.forEach(f => {
        [fundSel, ppmFundSel].forEach(sel => {
          const opt = document.createElement('option');
          opt.value = f.id;
          opt.textContent = f.name;
          sel.appendChild(opt);
        });
      });
      fundSel.onchange = loadReports;
      ppmFundSel.onchange = loadPPM;

      // Populate scheme filter for CTR
      const ctrSel = document.getElementById('ctr-scheme-filter');
      schemes.forEach(s => {
        const opt = document.createElement('option');
        opt.value = s.id;
        opt.textContent = `${s.fund_name} → ${s.name}`;
        ctrSel.appendChild(opt);
      });
      ctrSel.onchange = loadCTR;
    } catch (e) { console.error('Failed to load funds/schemes:', e); }
  }

  async function loadInvestors() {
    try { investors = await Auth.apiGet('/lp/investors/'); }
    catch (e) { console.error('Failed to load investors:', e); }
  }

  async function loadInvestments() {
    try {
      investments = [];
      for (const s of schemes) {
        try {
          const invs = await Auth.apiGet(`/schemes/${s.id}/investments/`);
          investments.push(...invs);
        } catch {}
      }
    } catch (e) { console.error('Failed to load investments:', e); }
  }

  // ── Stats ─────────────────────────────────────────────────
  function renderStats() {
    const bar = document.getElementById('stats-bar');
    bar.innerHTML = '';
    const overdue = reports.filter(r => r.filing_status === 'draft' && r.due_date && new Date(r.due_date) < new Date()).length;
    const unresolved = thresholdAlerts.filter(a => !a.resolved).length;
    const chips = [
      ['SEBI Reports', reports.length],
      ['Overdue Reports', overdue],
      ['AML Assessments', amlRecords.length],
      ['CTR Reports', ctrReports.length],
      ['Unresolved Alerts', unresolved],
      ['Calendar Events', calendarEvents.length],
    ];
    chips.forEach(([label, value]) => {
      const div = document.createElement('div');
      div.className = 'hero-stat';
      div.innerHTML = `<span class="hero-stat-label">${label}</span><span class="hero-stat-value mono">${value}</span>`;
      bar.appendChild(div);
    });
  }

  // ═══════════════════════════════════════════════════════════
  // SEBI REPORTS
  // ═══════════════════════════════════════════════════════════
  async function loadReports() {
    try {
      const fundId = document.getElementById('report-fund-filter').value;
      const url = fundId ? `/compliance/reports/?fund=${fundId}` : '/compliance/reports/';
      reports = await Auth.apiGet(url);
      renderReports();
    } catch (e) { console.error('Failed to load SEBI reports:', e); }
  }

  function renderReports() {
    const statusFilter = document.getElementById('report-status-filter').value;
    let list = reports;
    if (statusFilter) list = list.filter(r => r.filing_status === statusFilter);

    const container = document.getElementById('reports-list');
    container.innerHTML = '';

    if (!list.length) {
      container.innerHTML = `<div class="comp-empty">No SEBI reports found. Create one using the button above.</div>`;
      return;
    }

    list.forEach(report => {
      const statusCls = `status-${report.filing_status}`;
      const isDue = report.due_date && new Date(report.due_date) < new Date() && report.filing_status !== 'submitted' && report.filing_status !== 'accepted';
      const dCls = dueDateClass(report.due_date);

      const card = document.createElement('div');
      card.className = `report-card ${isDue ? 'status-overdue' : 'status-' + report.filing_status}`;
      card.innerHTML = `
        <div class="report-card-header">
          <div>
            <div class="report-card-title">${esc(report.report_type_display)} — ${esc(report.fund_name)}</div>
            <div class="report-card-meta">
              Period: ${fmtDate(report.reporting_period_start)} → ${fmtDate(report.reporting_period_end)}
              ${report.si_portal_reference_number ? ' · SI Portal Ref: ' + esc(report.si_portal_reference_number) : ''}
            </div>
          </div>
          <span class="comp-badge ${statusCls}">${esc(report.status_display)}</span>
        </div>
        <div class="report-card-metrics">
          <div class="report-metric">
            <span class="label">Due Date</span>
            <span class="value ${dCls}">${fmtDate(report.due_date)}</span>
          </div>
          <div class="report-metric">
            <span class="label">Filed Date</span>
            <span class="value">${fmtDate(report.filed_date)}</span>
          </div>
          <div class="report-metric">
            <span class="label">NAV Reconciled</span>
            <span class="value">${report.nav_reconciled_with_depository ? 'Yes' : 'No'}</span>
          </div>
          <div class="report-metric">
            <span class="label">IVCA Version</span>
            <span class="value">${esc(report.ivca_format_version) || '—'}</span>
          </div>
        </div>
        <div class="card-actions">
          <button class="btn-action" data-id="${report.id}" data-action="edit-report">Edit</button>
          ${report.filing_status !== 'submitted' && report.filing_status !== 'accepted' ? `
          <button class="btn-action" data-id="${report.id}" data-action="submit-report"
            style="border-color:var(--accent-green);color:var(--accent-green);">Mark Submitted</button>` : ''}
        </div>
      `;
      container.appendChild(card);

      card.querySelector('[data-action="edit-report"]').onclick = async () => {
        try {
          const detail = await Auth.apiGet(`/compliance/reports/${report.id}/`);
          openReportForm(detail);
        } catch (e) { openReportForm(report); }
      };

      const submitBtn = card.querySelector('[data-action="submit-report"]');
      if (submitBtn) {
        submitBtn.onclick = async () => {
          try {
            await Auth.apiPut(`/compliance/reports/${report.id}/`, {
              ...report,
              filing_status: 'submitted',
              filed_date: new Date().toISOString().split('T')[0],
            });
            await loadReports();
          } catch (e) { alert('Error: ' + e.message); }
        };
      }
    });
  }

  function openReportForm(existing = null) {
    const isEdit = !!existing;
    const fundOpts = [{value: '', label: '— Select Fund —'}].concat(funds.map(f => ({value: f.id, label: f.name})));
    const schemeOpts = [{value: '', label: '— None —'}].concat(schemes.map(s => ({value: s.id, label: `${s.fund_name} → ${s.name}`})));

    openModal(isEdit ? 'Edit SEBI Report' : 'Create SEBI Report', [
      {name: 'fund', label: 'Fund', type: 'select', required: true, options: fundOpts, default: existing?.fund || ''},
      {name: 'scheme', label: 'Scheme (optional)', type: 'select', options: schemeOpts, default: existing?.scheme || ''},
      {name: 'report_type', label: 'Report Type', type: 'select', required: true, default: existing?.report_type || 'qar', options: [
        {value: 'qar', label: 'QAR — Quarterly Activity Report'},
        {value: 'aar', label: 'AAR — Annual Activity Report'},
      ]},
      {name: 'reporting_period_start', label: 'Reporting Period Start', type: 'date', required: true, default: existing?.reporting_period_start || ''},
      {name: 'reporting_period_end', label: 'Reporting Period End', type: 'date', required: true, default: existing?.reporting_period_end || ''},
      {name: 'due_date', label: 'Due Date', type: 'date', required: true, default: existing?.due_date || ''},
      {name: 'filing_status', label: 'Filing Status', type: 'select', default: existing?.filing_status || 'draft', options: [
        {value: 'draft', label: 'Draft'},
        {value: 'prepared', label: 'Prepared'},
        {value: 'submitted', label: 'Submitted'},
        {value: 'accepted', label: 'Accepted'},
        {value: 'rejected', label: 'Rejected'},
      ]},
      {name: 'filed_date', label: 'Filed Date', type: 'date', default: existing?.filed_date || ''},
      {name: 'si_portal_reference_number', label: 'SI Portal Reference Number', default: existing?.si_portal_reference_number || ''},
      {name: 'ivca_format_version', label: 'IVCA Format Version', default: existing?.ivca_format_version || ''},
    ], async (data) => {
      if (!data.scheme) delete data.scheme;
      if (!data.filed_date) delete data.filed_date;
      if (isEdit) {
        await Auth.apiPut(`/compliance/reports/${existing.id}/`, data);
      } else {
        await Auth.apiPost('/compliance/reports/', data);
      }
      await loadReports();
      renderStats();
    });
  }

  // ═══════════════════════════════════════════════════════════
  // AML DUE DILIGENCE
  // ═══════════════════════════════════════════════════════════
  async function loadAML() {
    try {
      amlRecords = await Auth.apiGet('/compliance/aml/');
      renderAML();
    } catch (e) { console.error('Failed to load AML records:', e); }
  }

  function renderAML() {
    const riskFilter = document.getElementById('aml-risk-filter').value;
    let list = amlRecords;
    if (riskFilter) list = list.filter(a => a.risk_rating === riskFilter);

    const tbody = document.getElementById('aml-tbody');
    tbody.innerHTML = '';

    if (!list.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="comp-empty">No AML assessments found.</td></tr>`;
      return;
    }

    list.forEach(aml => {
      const riskCls = `risk-${aml.risk_rating}`;
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>
          <div style="font-weight:600;font-size:13px;">${esc(aml.investor_name)}</div>
        </td>
        <td><span class="comp-badge ${riskCls}">${esc(aml.risk_rating_display)}</span></td>
        <td>${boolCell(aml.is_land_border_country_investor)}</td>
        <td>${boolCell(aml.exceeds_50pct_threshold)}</td>
        <td>${aml.beneficial_owner_identified ? boolCell(true) : boolCell(false)}</td>
        <td style="font-size:12px;font-family:var(--font-mono);">${fmtDate(aml.risk_assessment_date)}</td>
        <td>${boolCell(aml.custodian_reported)}</td>
        <td>${aml.str_filed ? `<span class="bool-yes">YES</span><div style="font-size:10px;font-family:var(--font-mono);color:var(--text-muted);">${esc(aml.str_reference) || ''}</div>` : '<span class="bool-no">No</span>'}</td>
        <td>
          <button class="btn-action" data-id="${aml.id}" data-action="edit-aml">Edit</button>
        </td>
      `;
      tbody.appendChild(tr);

      tr.querySelector('[data-action="edit-aml"]').onclick = () => openAMLForm(aml);
    });
  }

  function openAMLForm(existing = null) {
    const isEdit = !!existing;
    const investorOpts = [{value: '', label: '— Select Investor —'}].concat(
      investors.map(i => ({value: i.id, label: i.investor_name}))
    );

    openModal(isEdit ? 'Edit AML Assessment' : 'New AML Assessment', [
      {name: 'investor', label: 'Investor', type: 'select', required: true, options: investorOpts, default: existing?.investor || ''},
      {name: 'risk_rating', label: 'Risk Rating', type: 'select', required: true, default: existing?.risk_rating || 'low', options: [
        {value: 'low', label: 'Low'},
        {value: 'medium', label: 'Medium'},
        {value: 'high', label: 'High'},
        {value: 'very_high', label: 'Very High'},
      ]},
      {name: 'risk_assessment_date', label: 'Assessment Date', type: 'date', default: existing?.risk_assessment_date || ''},
      {name: 'beneficial_owner_details', label: 'Beneficial Owner Details', type: 'textarea', default: existing?.beneficial_owner_details || ''},
      {name: 'risk_notes', label: 'Risk Notes', type: 'textarea', default: existing?.risk_notes || ''},
      {name: 'str_reference', label: 'STR Reference (if STR filed)', default: existing?.str_reference || ''},
      {name: 'custodian_report_date', label: 'Custodian Report Date', type: 'date', default: existing?.custodian_report_date || ''},
    ], async (data) => {
      if (!data.risk_assessment_date) delete data.risk_assessment_date;
      if (!data.custodian_report_date) delete data.custodian_report_date;
      if (isEdit) {
        await Auth.apiPut(`/compliance/aml/${existing.id}/`, data);
      } else {
        await Auth.apiPost('/compliance/aml/', data);
      }
      await loadAML();
      renderStats();
    });
  }

  // ═══════════════════════════════════════════════════════════
  // CTR (Compliance Test Reports)
  // ═══════════════════════════════════════════════════════════
  async function loadCTR() {
    try {
      const schemeId = document.getElementById('ctr-scheme-filter').value;
      const url = schemeId ? `/compliance/ctr/?scheme=${schemeId}` : '/compliance/ctr/';
      ctrReports = await Auth.apiGet(url);
      renderCTR();
    } catch (e) { console.error('Failed to load CTR reports:', e); }
  }

  function renderCTR() {
    const container = document.getElementById('ctr-list');
    container.innerHTML = '';

    if (!ctrReports.length) {
      container.innerHTML = `<div class="comp-empty">No compliance test reports found.</div>`;
      return;
    }

    ctrReports.forEach(ctr => {
      const compCls = `status-${ctr.overall_compliance_status}`;
      const repCls = `status-${ctr.report_status}`;
      const card = document.createElement('div');
      card.className = 'ctr-card';
      card.innerHTML = `
        <div class="ctr-card-header">
          <div>
            <div class="ctr-card-title">${esc(ctr.scheme_name)} — FY ${esc(ctr.financial_year)}</div>
            <div class="ctr-card-meta">
              Submitted to Trustee: ${fmtDate(ctr.submitted_to_trustee_at)}
            </div>
          </div>
          <div style="display:flex;gap:8px;flex-direction:column;align-items:flex-end;">
            <span class="comp-badge ${compCls}">${esc(ctr.compliance_display)}</span>
            <span class="comp-badge ${repCls}">${esc(ctr.report_status_display)}</span>
          </div>
        </div>
        <div class="card-actions">
          <button class="btn-toggle-items" data-id="${ctr.id}">View Checklist</button>
          <button class="btn-action" data-id="${ctr.id}" data-action="edit-ctr">Edit</button>
          <button class="btn-action" data-id="${ctr.id}" data-action="add-checklist"
            style="border-color:var(--accent-blue);color:var(--accent-blue);">+ Add Check Item</button>
        </div>
        <div class="checklist-wrap" id="checklist-${ctr.id}"></div>
      `;
      container.appendChild(card);

      card.querySelector('.btn-toggle-items').onclick = () => toggleChecklist(ctr.id);
      card.querySelector('[data-action="edit-ctr"]').onclick = () => openCTRForm(ctr);
      card.querySelector('[data-action="add-checklist"]').onclick = () => openChecklistItemForm(ctr.id);
    });
  }

  async function toggleChecklist(ctrId) {
    const wrap = document.getElementById(`checklist-${ctrId}`);
    if (wrap.classList.contains('visible')) {
      wrap.classList.remove('visible');
      return;
    }
    try {
      const items = await Auth.apiGet(`/compliance/ctr/${ctrId}/checklist/`);
      wrap.innerHTML = '';
      if (!items.length) {
        wrap.innerHTML = '<p style="padding:12px 0;color:var(--text-muted);font-size:12px;">No checklist items yet. Use "+ Add Check Item".</p>';
      } else {
        items.forEach(it => {
          const compCls = `status-${it.compliance_status}`;
          const div = document.createElement('div');
          div.className = 'checklist-item';
          div.innerHTML = `
            <div class="checklist-num">#${it.check_number || '—'}</div>
            <div class="checklist-content">
              <div class="checklist-reg">${esc(it.regulation_reference) || 'Regulation'}</div>
              <div class="checklist-desc">${esc(it.description)}</div>
              ${it.evidence ? `<div style="font-size:11px;color:var(--text-muted);margin-top:3px;">Evidence: ${esc(it.evidence)}</div>` : ''}
              ${it.remarks ? `<div style="font-size:11px;color:var(--text-muted);">Remarks: ${esc(it.remarks)}</div>` : ''}
            </div>
            <div class="checklist-status">
              <span class="comp-badge ${compCls}">${esc(it.status_display)}</span>
            </div>
          `;
          wrap.appendChild(div);
        });
      }
      wrap.classList.add('visible');
    } catch (e) { console.error('Failed to load checklist:', e); }
  }

  function openCTRForm(existing = null) {
    const isEdit = !!existing;
    const schemeOpts = [{value: '', label: '— Select Scheme —'}].concat(
      schemes.map(s => ({value: s.id, label: `${s.fund_name} → ${s.name}`}))
    );

    openModal(isEdit ? 'Edit CTR' : 'New Compliance Test Report', [
      {name: 'scheme', label: 'Scheme', type: 'select', required: true, options: schemeOpts, default: existing?.scheme || ''},
      {name: 'financial_year', label: 'Financial Year (e.g. 2024-25)', required: true, placeholder: '2024-25', default: existing?.financial_year || ''},
      {name: 'overall_compliance_status', label: 'Overall Compliance Status', type: 'select', default: existing?.overall_compliance_status || 'compliant', options: [
        {value: 'compliant', label: 'Compliant'},
        {value: 'partially_compliant', label: 'Partially Compliant'},
        {value: 'non_compliant', label: 'Non-Compliant'},
      ]},
      {name: 'report_status', label: 'Report Status', type: 'select', default: existing?.report_status || 'draft', options: [
        {value: 'draft', label: 'Draft'},
        {value: 'submitted', label: 'Submitted to Trustee'},
        {value: 'acknowledged', label: 'Trustee Acknowledged'},
      ]},
      {name: 'submitted_to_trustee_at', label: 'Submitted to Trustee Date', type: 'date', default: existing?.submitted_to_trustee_at?.split('T')[0] || ''},
      {name: 'observations', label: 'Observations', type: 'textarea', default: existing?.observations || ''},
      {name: 'remediation_plan', label: 'Remediation Plan', type: 'textarea', default: existing?.remediation_plan || ''},
    ], async (data) => {
      if (!data.submitted_to_trustee_at) delete data.submitted_to_trustee_at;
      if (isEdit) {
        await Auth.apiPut(`/compliance/ctr/${existing.id}/`, data);
      } else {
        await Auth.apiPost('/compliance/ctr/', data);
      }
      await loadCTR();
      renderStats();
    });
  }

  function openChecklistItemForm(ctrId) {
    openModal('Add Checklist Item', [
      {name: 'check_number', label: 'Check Number', type: 'number', placeholder: '1'},
      {name: 'regulation_reference', label: 'Regulation Reference', placeholder: 'e.g., SEBI AIF Reg. 15(1)(a)'},
      {name: 'description', label: 'Description', type: 'textarea', required: true},
      {name: 'compliance_status', label: 'Compliance Status', type: 'select', default: 'compliant', options: [
        {value: 'compliant', label: 'Compliant'},
        {value: 'non_compliant', label: 'Non-Compliant'},
        {value: 'not_applicable', label: 'Not Applicable'},
        {value: 'under_review', label: 'Under Review'},
      ]},
      {name: 'evidence', label: 'Evidence', type: 'textarea'},
      {name: 'remarks', label: 'Remarks', type: 'textarea'},
    ], async (data) => {
      data.compliance_test_report = ctrId;
      await Auth.apiPost(`/compliance/ctr/${ctrId}/checklist/`, data);
      // Re-render checklist if expanded
      const wrap = document.getElementById(`checklist-${ctrId}`);
      if (wrap.classList.contains('visible')) {
        wrap.classList.remove('visible');
        await toggleChecklist(ctrId);
      }
    });
  }

  // ═══════════════════════════════════════════════════════════
  // EQUITY THRESHOLD ALERTS
  // ═══════════════════════════════════════════════════════════
  async function loadAlerts() {
    try {
      thresholdAlerts = await Auth.apiGet('/compliance/alerts/');
      renderAlerts();
    } catch (e) { console.error('Failed to load threshold alerts:', e); }
  }

  function renderAlerts() {
    const tbody = document.getElementById('alerts-tbody');
    tbody.innerHTML = '';

    if (!thresholdAlerts.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="comp-empty">No threshold alerts logged.</td></tr>`;
      return;
    }

    thresholdAlerts.forEach(alert => {
      const dCls = dueDateClass(alert.custodian_notification_deadline);
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td style="font-weight:600;">${esc(alert.company_name)}</td>
        <td style="font-family:var(--font-mono);font-weight:700;color:var(--accent-red);">${alert.threshold_breached ? parseFloat(alert.threshold_breached).toFixed(2) + '%' : '—'}</td>
        <td style="font-family:var(--font-mono);">${alert.stake_percentage ? parseFloat(alert.stake_percentage).toFixed(2) + '%' : '—'}</td>
        <td style="font-size:12px;font-family:var(--font-mono);">${fmtDate(alert.breach_date)}</td>
        <td style="font-size:12px;font-family:var(--font-mono);" class="${dCls}">${fmtDate(alert.custodian_notification_deadline)}</td>
        <td>${boolCell(alert.custodian_notified)}</td>
        <td style="font-size:11px;font-family:var(--font-mono);">${esc(alert.custodian_reference) || '—'}</td>
        <td>${alert.resolved
          ? '<span class="comp-badge status-completed">Resolved</span>'
          : '<span class="comp-badge status-pending">Open</span>'}</td>
        <td>
          <button class="btn-action" data-id="${alert.id}" data-action="edit-alert">Edit</button>
        </td>
      `;
      tbody.appendChild(tr);

      tr.querySelector('[data-action="edit-alert"]').onclick = () => openAlertForm(alert);
    });
  }

  function openAlertForm(existing = null) {
    const isEdit = !!existing;
    const invOpts = [{value: '', label: '— Select Investment —'}].concat(
      investments.map(i => ({value: i.id, label: i.company_name}))
    );

    openModal(isEdit ? 'Edit Threshold Alert' : 'Log Equity Threshold Alert', [
      {name: 'investment', label: 'Investment / Company', type: 'select', required: true, options: invOpts, default: existing?.investment || ''},
      {name: 'threshold_breached', label: 'Threshold Breached (%)', type: 'number', required: true, step: '0.01', default: existing?.threshold_breached || ''},
      {name: 'breach_date', label: 'Breach Date', type: 'date', required: true, default: existing?.breach_date || ''},
      {name: 'stake_percentage', label: 'Current Stake (%)', type: 'number', step: '0.01', default: existing?.stake_percentage || ''},
      {name: 'custodian_notification_deadline', label: 'Custodian Notification Deadline', type: 'date', default: existing?.custodian_notification_deadline || ''},
      {name: 'custodian_notified_date', label: 'Custodian Notified Date', type: 'date', default: existing?.custodian_notified_date || ''},
      {name: 'custodian_reference', label: 'Custodian Reference', default: existing?.custodian_reference || ''},
    ], async (data) => {
      if (!data.custodian_notification_deadline) delete data.custodian_notification_deadline;
      if (!data.custodian_notified_date) delete data.custodian_notified_date;
      if (isEdit) {
        await Auth.apiPut(`/compliance/alerts/${existing.id}/`, data);
      } else {
        await Auth.apiPost('/compliance/alerts/', data);
      }
      await loadAlerts();
      renderStats();
    });
  }

  // ═══════════════════════════════════════════════════════════
  // COMPLIANCE CALENDAR
  // ═══════════════════════════════════════════════════════════
  async function loadCalendar() {
    try {
      calendarEvents = await Auth.apiGet('/compliance/calendar/');
      renderCalendar();
    } catch (e) { console.error('Failed to load calendar:', e); }
  }

  function renderCalendar() {
    const statusFilter = document.getElementById('cal-status-filter').value;
    let list = calendarEvents;
    if (statusFilter) list = list.filter(e => e.status === statusFilter);
    // Sort by due date
    list = [...list].sort((a, b) => new Date(a.due_date) - new Date(b.due_date));

    const container = document.getElementById('calendar-list');
    container.innerHTML = '';

    if (!list.length) {
      container.innerHTML = `<div class="comp-empty">No compliance events found.</div>`;
      return;
    }

    list.forEach(ev => {
      const dueDate = ev.due_date ? new Date(ev.due_date) : null;
      const day = dueDate ? dueDate.getDate().toString().padStart(2, '0') : '—';
      const month = dueDate ? dueDate.toLocaleString('en-IN', {month: 'short'}).toUpperCase() : '';
      const isOverdue = dueDate && dueDate < new Date() && ev.status !== 'completed';

      const div = document.createElement('div');
      div.className = `cal-event ${isOverdue ? 'overdue' : ''} ${ev.status === 'completed' ? 'completed' : ''}`;
      div.innerHTML = `
        <div class="cal-event-date">
          <div class="cal-event-day">${day}</div>
          <div class="cal-event-month">${month}</div>
        </div>
        <div class="cal-event-body">
          <div class="cal-event-title">${esc(ev.title)}</div>
          <div class="cal-event-type">${esc(ev.type_display)} · ${esc(ev.recurrence_display)}</div>
          <div class="cal-event-meta">
            ${ev.assigned_to_name ? `<span>Assigned: ${esc(ev.assigned_to_name)}</span>` : ''}
            ${ev.advance_reminder_days ? `<span>Reminder: ${ev.advance_reminder_days} days before</span>` : ''}
            <span class="comp-badge status-${ev.status}">${esc(ev.status_display)}</span>
          </div>
          ${ev.description ? `<p style="font-size:12px;color:var(--text-muted);margin-top:6px;">${esc(ev.description)}</p>` : ''}
        </div>
        <div class="cal-event-actions">
          <button class="btn-action" data-id="${ev.id}" data-action="edit-event">Edit</button>
          ${ev.status !== 'completed' ? `
          <button class="btn-action" data-id="${ev.id}" data-action="complete-event"
            style="border-color:var(--accent-green);color:var(--accent-green);">Done</button>` : ''}
        </div>
      `;
      container.appendChild(div);

      div.querySelector('[data-action="edit-event"]').onclick = () => openCalendarForm(ev);
      const doneBtn = div.querySelector('[data-action="complete-event"]');
      if (doneBtn) {
        doneBtn.onclick = async () => {
          try {
            await Auth.apiPut(`/compliance/calendar/${ev.id}/`, {
              ...ev,
              status: 'completed',
              completed_date: new Date().toISOString().split('T')[0],
            });
            await loadCalendar();
          } catch (e) { alert('Error: ' + e.message); }
        };
      }
    });
  }

  function openCalendarForm(existing = null) {
    const isEdit = !!existing;
    const fundOpts = [{value: '', label: '— None —'}].concat(funds.map(f => ({value: f.id, label: f.name})));
    const schemeOpts = [{value: '', label: '— None —'}].concat(schemes.map(s => ({value: s.id, label: `${s.fund_name} → ${s.name}`})));

    openModal(isEdit ? 'Edit Compliance Event' : 'Add Compliance Event', [
      {name: 'title', label: 'Title', required: true, default: existing?.title || ''},
      {name: 'compliance_type', label: 'Compliance Type', type: 'select', required: true, default: existing?.compliance_type || 'sebi_filing', options: [
        {value: 'sebi_filing', label: 'SEBI Filing'},
        {value: 'audit', label: 'Audit'},
        {value: 'board_meeting', label: 'Board Meeting'},
        {value: 'aml_review', label: 'AML Review'},
        {value: 'tax_filing', label: 'Tax Filing'},
        {value: 'roc_filing', label: 'RoC Filing'},
        {value: 'trustee_report', label: 'Trustee Report'},
        {value: 'other', label: 'Other'},
      ]},
      {name: 'due_date', label: 'Due Date', type: 'date', required: true, default: existing?.due_date || ''},
      {name: 'recurrence', label: 'Recurrence', type: 'select', default: existing?.recurrence || 'once', options: [
        {value: 'once', label: 'One-Time'},
        {value: 'monthly', label: 'Monthly'},
        {value: 'quarterly', label: 'Quarterly'},
        {value: 'half_yearly', label: 'Half-Yearly'},
        {value: 'annually', label: 'Annually'},
      ]},
      {name: 'advance_reminder_days', label: 'Advance Reminder (days)', type: 'number', default: existing?.advance_reminder_days || '7'},
      {name: 'status', label: 'Status', type: 'select', default: existing?.status || 'pending', options: [
        {value: 'pending', label: 'Pending'},
        {value: 'in_progress', label: 'In Progress'},
        {value: 'completed', label: 'Completed'},
        {value: 'overdue', label: 'Overdue'},
      ]},
      {name: 'fund', label: 'Fund (optional)', type: 'select', options: fundOpts, default: existing?.fund || ''},
      {name: 'scheme', label: 'Scheme (optional)', type: 'select', options: schemeOpts, default: existing?.scheme || ''},
      {name: 'description', label: 'Description', type: 'textarea', default: existing?.description || ''},
      {name: 'notes', label: 'Notes', type: 'textarea', default: existing?.notes || ''},
    ], async (data) => {
      if (!data.fund) delete data.fund;
      if (!data.scheme) delete data.scheme;
      if (isEdit) {
        await Auth.apiPut(`/compliance/calendar/${existing.id}/`, data);
      } else {
        await Auth.apiPost('/compliance/calendar/', data);
      }
      await loadCalendar();
      renderStats();
    });
  }

  // ═══════════════════════════════════════════════════════════
  // MODAL ENGINE
  // ═══════════════════════════════════════════════════════════
  function openModal(title, fields, callback) {
    document.getElementById('modal-title').textContent = title;
    const container = document.getElementById('modal-fields');
    container.innerHTML = '';

    fields.forEach(f => {
      const div = document.createElement('div');
      div.className = 'modal-field';
      let input;
      if (f.type === 'select') {
        input = `<select name="${f.name}" ${f.required ? 'required' : ''}>
          ${f.options.map(o => `<option value="${o.value}" ${o.value == f.default ? 'selected' : ''}>${o.label}</option>`).join('')}
        </select>`;
      } else if (f.type === 'textarea') {
        input = `<textarea name="${f.name}" placeholder="${f.placeholder || ''}" ${f.required ? 'required' : ''}>${f.default || ''}</textarea>`;
      } else {
        input = `<input type="${f.type || 'text'}" name="${f.name}" value="${f.default || ''}" placeholder="${f.placeholder || ''}" ${f.required ? 'required' : ''} ${f.step ? `step="${f.step}"` : ''} />`;
      }
      div.innerHTML = `<label>${f.label}</label>${input}`;
      container.appendChild(div);
    });

    modalCallback = callback;
    document.getElementById('modal-overlay').classList.remove('hidden');
  }

  function closeModal() {
    document.getElementById('modal-overlay').classList.add('hidden');
    modalCallback = null;
  }

  async function handleModalSubmit(e) {
    e.preventDefault();
    if (!modalCallback) return;
    const form = document.getElementById('modal-form');
    const data = {};
    const textFields = [
      'description', 'notes', 'financial_year', 'title',
      'regulation_reference', 'evidence', 'remarks', 'si_portal_reference_number',
      'ivca_format_version', 'custodian_reference', 'str_reference',
      'beneficial_owner_details', 'risk_notes', 'observations', 'remediation_plan',
      'sebi_acknowledgement_number', 'document_url', 'circular_number',
      'summary', 'sebi_url', 'full_text', 'action_title', 'action_description',
      'completion_notes', 'deferred_reason',
    ];
    new FormData(form).forEach((v, k) => {
      if (v === '') return;
      const n = Number(v);
      if (!isNaN(n) && v.trim() !== '' && !textFields.includes(k)) {
        data[k] = n;
      } else {
        data[k] = v;
      }
    });

    const btn = document.getElementById('modal-submit');
    btn.disabled = true;
    btn.textContent = 'Saving...';
    try {
      await modalCallback(data);
      closeModal();
    } catch (err) {
      alert('Error: ' + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Save';
    }
  }

  // ── PPM Amendments ─────────────────────────────────────────

  async function loadPPM() {
    try {
      const fundId = document.getElementById('ppm-fund-filter').value;
      const params = fundId ? `?fund=${fundId}` : '';
      ppmAmendments = await Auth.apiGet(`/compliance/ppm/${params}`);
      renderPPM();
    } catch (e) { console.error('PPM load failed:', e); }
  }

  function renderPPM() {
    const statusFilter = document.getElementById('ppm-status-filter').value;
    let list = ppmAmendments;
    if (statusFilter) list = list.filter(a => a.approval_status === statusFilter);

    const container = document.getElementById('ppm-list');
    if (!list.length) {
      container.innerHTML = '<p class="muted-hint">No PPM amendments found.</p>';
      return;
    }

    const statusColors = {
      draft: 'comp-badge status-draft',
      under_review: 'comp-badge status-pending',
      trustee_approved: 'comp-badge status-in_progress',
      sebi_filed: 'comp-badge status-submitted',
      investor_notified: 'comp-badge status-accepted',
      effective: 'comp-badge status-compliant',
    };

    container.innerHTML = list.map(a => {
      const cls = statusColors[a.approval_status] || 'comp-badge';
      const exitExpiry = a.investor_exit_window_expiry
        ? `<span class="${dueDateClass(a.investor_exit_window_expiry)}">${fmtDate(a.investor_exit_window_expiry)}</span>`
        : '—';
      return `
        <div class="report-card">
          <div class="report-card-header">
            <div>
              <div class="report-card-title">Amendment #${esc(a.amendment_number)}: ${esc(a.title)}</div>
              <div class="report-card-meta">
                ${esc(a.fund_name)} ${a.scheme_name ? '→ ' + esc(a.scheme_name) : ''} &nbsp;·&nbsp;
                ${esc(a.amendment_type_display)}
              </div>
            </div>
            <span class="${cls}">${esc(a.status_display)}</span>
          </div>
          <div class="report-card-dates">
            <div><label>Board Approval</label><span>${fmtDate(a.board_approval_date)}</span></div>
            <div><label>Trustee Approval</label><span>${fmtDate(a.trustee_approval_date)}</span></div>
            <div><label>SEBI Filing</label><span>${fmtDate(a.sebi_filing_date)}</span></div>
            <div><label>Investor Notified</label><span>${fmtDate(a.investor_notification_date)}</span></div>
            <div><label>Effective Date</label><span>${fmtDate(a.effective_date)}</span></div>
            <div><label>LP Exit Window Expires</label>${exitExpiry}</div>
          </div>
          ${a.sebi_acknowledgement_number ? `<div class="report-card-meta" style="margin-top:6px;">SEBI Ref: <strong>${esc(a.sebi_acknowledgement_number)}</strong></div>` : ''}
          <div class="report-card-actions">
            <button class="btn-ghost small" onclick="window._editPPM('${a.id}')">Edit</button>
            ${a.document_url ? `<a class="btn-ghost small" href="${esc(a.document_url)}" target="_blank">View Document</a>` : ''}
          </div>
        </div>`;
    }).join('');

    window._editPPM = (id) => {
      const a = ppmAmendments.find(x => x.id === id);
      if (a) openPPMForm(a);
    };
  }

  function openPPMForm(existing = null) {
    const fundOptions = funds.map(f =>
      `<option value="${f.id}" ${existing && existing.fund === f.id ? 'selected' : ''}>${esc(f.name)}</option>`
    ).join('');
    const schemeOptions = '<option value="">None (Fund-level)</option>' + schemes.map(s =>
      `<option value="${s.id}" ${existing && existing.scheme === s.id ? 'selected' : ''}>${esc(s.fund_name)} → ${esc(s.name)}</option>`
    ).join('');

    openModal(existing ? 'Edit PPM Amendment' : 'New PPM Amendment', [
      { name: 'fund', label: 'Fund', type: 'select', options: fundOptions, required: true },
      { name: 'scheme', label: 'Scheme (optional)', type: 'select', options: schemeOptions },
      { name: 'amendment_number', label: 'Amendment Number', type: 'number', value: existing?.amendment_number || '', required: true },
      { name: 'amendment_type', label: 'Amendment Type', type: 'select', options: [
        ['investment_strategy','Investment Strategy Change'],['fee_structure','Fee Structure Change'],
        ['key_personnel','Key Personnel Change'],['scheme_tenure','Scheme Tenure Change'],
        ['corpus_limit','Target Corpus Change'],['investment_restrictions','Investment Restrictions Change'],
        ['distribution_policy','Distribution Policy Change'],['other','Other Material Change'],
      ].map(([v,l]) => `<option value="${v}" ${existing?.amendment_type===v?'selected':''}>${l}</option>`).join(''), required: true },
      { name: 'title', label: 'Title', type: 'text', value: existing?.title || '', required: true },
      { name: 'description', label: 'Description', type: 'textarea', value: existing?.description || '', required: true },
      { name: 'approval_status', label: 'Approval Status', type: 'select', options: [
        ['draft','Draft'],['under_review','Under Review'],['trustee_approved','Trustee Approved'],
        ['sebi_filed','Filed with SEBI'],['investor_notified','Investors Notified'],['effective','Effective'],
      ].map(([v,l]) => `<option value="${v}" ${existing?.approval_status===v?'selected':''}>${l}</option>`).join('') },
      { name: 'board_approval_date', label: 'Board Approval Date', type: 'date', value: existing?.board_approval_date || '' },
      { name: 'trustee_approval_date', label: 'Trustee Approval Date', type: 'date', value: existing?.trustee_approval_date || '' },
      { name: 'sebi_filing_date', label: 'SEBI Filing Date', type: 'date', value: existing?.sebi_filing_date || '' },
      { name: 'investor_notification_date', label: 'Investor Notification Date', type: 'date', value: existing?.investor_notification_date || '' },
      { name: 'effective_date', label: 'Effective Date', type: 'date', value: existing?.effective_date || '' },
      { name: 'investor_exit_window_days', label: 'LP Exit Window (days)', type: 'number', value: existing?.investor_exit_window_days || 30 },
      { name: 'investor_exit_window_expiry', label: 'LP Exit Window Expiry', type: 'date', value: existing?.investor_exit_window_expiry || '' },
      { name: 'sebi_acknowledgement_number', label: 'SEBI Acknowledgement Number', type: 'text', value: existing?.sebi_acknowledgement_number || '' },
      { name: 'document_url', label: 'Document URL', type: 'text', value: existing?.document_url || '' },
      { name: 'notes', label: 'Notes', type: 'textarea', value: existing?.notes || '' },
    ], async (data) => {
      if (!data.scheme) delete data.scheme;
      if (existing) {
        await Auth.apiPut(`/compliance/ppm/${existing.id}/`, data);
      } else {
        await Auth.apiPost('/compliance/ppm/', data);
      }
      await loadPPM();
    });
  }

  // ── SEBI Circulars ──────────────────────────────────────────

  async function loadCirculars() {
    try {
      sebiCirculars = await Auth.apiGet('/compliance/circulars/');
      renderCirculars();
    } catch (e) { console.error('Circulars load failed:', e); }
  }

  function renderCirculars() {
    const impactFilter = document.getElementById('circular-impact-filter').value;
    let list = sebiCirculars;
    if (impactFilter) list = list.filter(c => c.impact_level === impactFilter);

    const container = document.getElementById('circulars-list');
    if (!list.length) {
      container.innerHTML = '<p class="muted-hint">No SEBI circulars found. Add circulars to track compliance actions.</p>';
      return;
    }

    const impactColors = {
      low: 'comp-badge status-compliant',
      medium: 'comp-badge status-pending',
      high: 'comp-badge status-in_review',
      critical: 'comp-badge status-rejected',
    };

    container.innerHTML = list.map(c => {
      const cls = impactColors[c.impact_level] || 'comp-badge';
      const pendingBadge = c.pending_actions_count > 0
        ? `<span class="comp-badge status-pending">${c.pending_actions_count} pending actions</span>`
        : '<span class="comp-badge status-compliant">All actions done</span>';
      return `
        <div class="report-card${c.is_superseded ? ' superseded' : ''}">
          <div class="report-card-header">
            <div>
              <div class="report-card-title">${esc(c.circular_number)}</div>
              <div class="report-card-meta" style="max-width:600px;">${esc(c.title)}</div>
              <div class="report-card-meta">${fmtDate(c.circular_date)} &nbsp;·&nbsp; ${esc(c.applicability_display)}</div>
            </div>
            <div style="display:flex;flex-direction:column;gap:6px;align-items:flex-end;">
              <span class="${cls}">${esc(c.impact_display)}</span>
              ${pendingBadge}
              ${c.is_superseded ? '<span class="comp-badge status-rejected">Superseded</span>' : ''}
            </div>
          </div>
          <div class="report-card-dates">
            <div><label>Circular Date</label><span>${fmtDate(c.circular_date)}</span></div>
            <div><label>Compliance Deadline</label>
              <span class="${dueDateClass(c.compliance_deadline)}">${fmtDate(c.compliance_deadline)}</span>
            </div>
            <div><label>AI Parsed</label><span>${c.ai_parsed ? 'Yes' : 'No'}</span></div>
          </div>
          <div class="report-card-actions">
            <button class="btn-ghost small" onclick="window._editCircular('${c.id}')">Edit</button>
            <button class="btn-ghost small" onclick="window._showActions('${c.id}')">Actions (${c.pending_actions_count})</button>
            ${c.sebi_url ? `<a class="btn-ghost small" href="${esc(c.sebi_url)}" target="_blank">SEBI Website</a>` : ''}
          </div>
          <div class="circular-actions-wrap hidden" id="actions-${c.id}"></div>
        </div>`;
    }).join('');

    window._editCircular = (id) => {
      const c = sebiCirculars.find(x => x.id === id);
      if (c) openCircularForm(c);
    };
    window._showActions = async (id) => {
      const wrap = document.getElementById(`actions-${id}`);
      if (!wrap) return;
      if (!wrap.classList.contains('hidden')) { wrap.classList.add('hidden'); return; }
      wrap.classList.remove('hidden');
      await renderCircularActions(id, wrap);
    };
  }

  async function renderCircularActions(circularId, wrap) {
    try {
      const actions = await Auth.apiGet(`/compliance/circulars/${circularId}/actions/`);
      const actStatusColors = {
        pending: 'comp-badge status-pending',
        in_progress: 'comp-badge status-in_review',
        completed: 'comp-badge status-compliant',
        not_applicable: 'comp-badge',
        deferred: 'comp-badge status-rejected',
      };
      const priorityIcon = { low: '🟢', medium: '🟡', high: '🔴', critical: '🚨' };

      wrap.innerHTML = `
        <div class="checklist-wrap" style="margin-top:12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <strong style="font-size:13px;">Compliance Actions</strong>
            <button class="btn-ghost small" onclick="window._addAction('${circularId}')">+ Add Action</button>
          </div>
          ${actions.length ? actions.map(a => `
            <div class="checklist-item">
              <span class="checklist-num">${priorityIcon[a.priority] || '·'}</span>
              <div style="flex:1;">
                <div style="font-weight:600;font-size:13px;">${esc(a.action_title)}</div>
                <div style="font-size:12px;color:var(--muted);">${esc(a.action_description)}</div>
                ${a.fund_name ? `<div style="font-size:11px;color:var(--muted);">Fund: ${esc(a.fund_name)}</div>` : ''}
                ${a.due_date ? `<div style="font-size:11px;" class="${dueDateClass(a.due_date)}">Due: ${fmtDate(a.due_date)}</div>` : ''}
              </div>
              <span class="${actStatusColors[a.status] || 'comp-badge'}">${esc(a.status_display)}</span>
              <button class="btn-ghost small" onclick="window._updateAction('${a.id}','${circularId}')">Update</button>
            </div>`).join('') : '<p class="muted-hint" style="font-size:12px;">No actions yet.</p>'}
        </div>`;

      window._addAction = (cid) => openActionForm(cid);
      window._updateAction = async (actionId, cid) => {
        const actionList = await Auth.apiGet(`/compliance/circulars/${cid}/actions/`);
        const action = actionList.find(x => x.id === actionId);
        if (action) openActionForm(cid, action);
      };
    } catch (e) {
      wrap.innerHTML = '<p class="muted-hint">Failed to load actions.</p>';
    }
  }

  function openCircularForm(existing = null) {
    openModal(existing ? 'Edit SEBI Circular' : 'Add SEBI Circular', [
      { name: 'circular_number', label: 'Circular Number', type: 'text', value: existing?.circular_number || '', required: true },
      { name: 'circular_date', label: 'Circular Date', type: 'date', value: existing?.circular_date || '', required: true },
      { name: 'title', label: 'Title / Subject', type: 'textarea', value: existing?.title || '', required: true },
      { name: 'summary', label: 'Summary (AI-generated or manual)', type: 'textarea', value: existing?.summary || '' },
      { name: 'applicability', label: 'Applicability', type: 'select', options: [
        ['all_aif','All AIFs'],['cat_i','Category I Only'],['cat_ii','Category II Only'],
        ['cat_iii','Category III Only'],['cat_i_ii','Category I & II'],
        ['gift_city','GIFT City / IFSC'],['specific','Specific Funds'],
      ].map(([v,l]) => `<option value="${v}" ${existing?.applicability===v?'selected':''}>${l}</option>`).join('') },
      { name: 'impact_level', label: 'Impact Level', type: 'select', options: [
        ['low','Low — Informational'],['medium','Medium — Process Change'],
        ['high','High — Immediate Action'],['critical','Critical — Regulatory Deadline'],
      ].map(([v,l]) => `<option value="${v}" ${existing?.impact_level===v?'selected':''}>${l}</option>`).join('') },
      { name: 'compliance_deadline', label: 'Compliance Deadline', type: 'date', value: existing?.compliance_deadline || '' },
      { name: 'sebi_url', label: 'SEBI Website URL', type: 'text', value: existing?.sebi_url || '' },
    ], async (data) => {
      if (existing) {
        await Auth.apiPut(`/compliance/circulars/${existing.id}/`, data);
      } else {
        await Auth.apiPost('/compliance/circulars/', data);
      }
      await loadCirculars();
    });
  }

  function openActionForm(circularId, existing = null) {
    const fundOptions = '<option value="">All Org Funds</option>' + funds.map(f =>
      `<option value="${f.id}" ${existing?.fund===f.id?'selected':''}>${esc(f.name)}</option>`
    ).join('');

    openModal(existing ? 'Update Action' : 'Add Action Item', [
      { name: 'action_title', label: 'Action Title', type: 'text', value: existing?.action_title || '', required: true },
      { name: 'action_description', label: 'Description', type: 'textarea', value: existing?.action_description || '', required: true },
      { name: 'fund', label: 'Fund (leave blank for all)', type: 'select', options: fundOptions },
      { name: 'priority', label: 'Priority', type: 'select', options: [
        ['low','Low'],['medium','Medium'],['high','High'],['critical','Critical'],
      ].map(([v,l]) => `<option value="${v}" ${existing?.priority===v?'selected':''}>${l}</option>`).join('') },
      { name: 'due_date', label: 'Due Date', type: 'date', value: existing?.due_date || '' },
      { name: 'status', label: 'Status', type: 'select', options: [
        ['pending','Pending'],['in_progress','In Progress'],['completed','Completed'],
        ['not_applicable','Not Applicable'],['deferred','Deferred'],
      ].map(([v,l]) => `<option value="${v}" ${existing?.status===v?'selected':''}>${l}</option>`).join('') },
      { name: 'completion_notes', label: 'Completion Notes / Evidence', type: 'textarea', value: existing?.completion_notes || '' },
      { name: 'deferred_reason', label: 'Deferred Reason (if deferred)', type: 'textarea', value: existing?.deferred_reason || '' },
    ], async (data) => {
      if (!data.fund) delete data.fund;
      if (existing) {
        await Auth.apiPut(`/compliance/circular-actions/${existing.id}/`, data);
      } else {
        await Auth.apiPost(`/compliance/circulars/${circularId}/actions/`, data);
      }
      // Re-render action list inline
      const wrap = document.getElementById(`actions-${circularId}`);
      if (wrap && !wrap.classList.contains('hidden')) {
        await renderCircularActions(circularId, wrap);
      }
    });
  }

  // ── Boot ──────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
