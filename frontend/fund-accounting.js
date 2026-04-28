/* ============================================================
   fund-accounting.js
   TrackFundAI Module 4 — Fund Accounting (GP Admin View)
   NAV Records · Carried Interest · Fund Ledger · Management Fees · Chart of Accounts
   Trial Balance · Financial Statements · Tally ERP Sync
============================================================ */

(() => {
  let schemes = [];
  let navRecords = [];
  let carryRecords = [];
  let ledgerEntries = [];
  let feeSchedules = [];
  let coaAccounts = [];
  let modalCallback = null;
  let activeTab = 'nav';

  // ── Formatting ────────────────────────────────────────────
  const esc = (s) => {
    if (s === null || s === undefined) return '—';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  };

  const fmtCurrency = (v) => {
    if (!v && v !== 0) return '—';
    const n = parseFloat(v);
    if (isNaN(n)) return '—';
    if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
    if (Math.abs(n) >= 1e7) return `${(n / 1e7).toFixed(1)}Cr`;
    if (Math.abs(n) >= 1e5) return `${(n / 1e5).toFixed(1)}L`;
    return n.toLocaleString('en-IN');
  };

  const fmtDate = (d) => d ? new Date(d).toLocaleDateString('en-IN', {day: '2-digit', month: 'short', year: 'numeric'}) : '—';

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
    document.getElementById('btn-new-nav').onclick = () => openNAVForm();
    document.getElementById('btn-new-carry').onclick = () => openCarryForm();
    document.getElementById('btn-new-entry').onclick = () => openLedgerForm();
    document.getElementById('btn-new-fee').onclick = () => openFeeForm();
    document.getElementById('btn-new-account').onclick = () => openCOAForm();
    document.getElementById('btn-generate-tb').onclick = () => generateTrialBalance();
    document.getElementById('btn-generate-fin').onclick = () => generateFinancials();
    document.getElementById('btn-export-fin').onclick = () => exportFinancialsPDF();
    document.getElementById('btn-tally-import').onclick = () => tallyImport();
    document.getElementById('btn-tally-export').onclick = () => tallyExport();

    // Tally file picker label
    document.getElementById('tally-import-file').onchange = (e) => {
      const name = e.target.files[0]?.name || 'No file selected';
      document.getElementById('tally-file-name').textContent = name;
    };

    // Filters
    document.getElementById('nav-reconciled-filter').onchange = renderNAV;
    document.getElementById('coa-type-filter').onchange = renderCOA;

    // Modal
    document.getElementById('modal-close').onclick = closeModal;
    document.getElementById('modal-cancel').onclick = closeModal;
    document.getElementById('modal-form').onsubmit = handleModalSubmit;

    await loadSchemes();
    await Promise.all([loadNAV(), loadCarry(), loadLedger(), loadFees(), loadCOA()]);
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
    ['nav', 'carry', 'ledger', 'fees', 'coa', 'trial-balance', 'financials', 'tally'].forEach(t => {
      const el = document.getElementById(`tab-${t}`);
      if (el) el.classList.toggle('hidden', t !== tab);
    });
  }

  // ── Load schemes ──────────────────────────────────────────
  async function loadSchemes() {
    try {
      const funds = await Auth.apiGet('/funds/');
      schemes = [];
      for (const fund of funds) {
        const fundSchemes = await Auth.apiGet(`/funds/${fund.id}/schemes/`);
        for (const s of fundSchemes) {
          schemes.push({ ...s, fund_name: fund.name });
        }
      }

      ['nav-scheme-filter', 'carry-scheme-filter', 'ledger-scheme-filter', 'fee-scheme-filter',
       'tb-scheme-select', 'fin-scheme-select', 'tally-scheme-select'].forEach(id => {
        const sel = document.getElementById(id);
        if (!sel) return;
        schemes.forEach(s => {
          const opt = document.createElement('option');
          opt.value = s.id;
          opt.textContent = `${s.fund_name} → ${s.name}`;
          sel.appendChild(opt);
        });
        sel.onchange = () => {
          if (id === 'nav-scheme-filter') loadNAV();
          else if (id === 'carry-scheme-filter') loadCarry();
          else if (id === 'ledger-scheme-filter') loadLedger();
          else if (id === 'fee-scheme-filter') loadFees();
        };
      });

      const refSel = document.getElementById('ledger-ref-filter');
      if (refSel) refSel.onchange = loadLedger;
    } catch (e) { console.error('Failed to load schemes:', e); }
  }

  // ── Stats ─────────────────────────────────────────────────
  function renderStats() {
    const bar = document.getElementById('stats-bar');
    bar.innerHTML = '';
    const reconciled = navRecords.filter(n => n.depository_reconciled).length;
    const latestNAV = navRecords.length ? navRecords[0]?.nav_per_unit : null;
    const chips = [
      ['NAV Records', navRecords.length],
      ['Reconciled', reconciled],
      ['Latest NAV/Unit', latestNAV ? '₹' + parseFloat(latestNAV).toFixed(4) : '—'],
      ['Ledger Entries', ledgerEntries.length],
      ['Fee Periods', feeSchedules.length],
      ['COA Accounts', coaAccounts.length],
    ];
    chips.forEach(([label, value]) => {
      const div = document.createElement('div');
      div.className = 'hero-stat';
      div.innerHTML = `<span class="hero-stat-label">${label}</span><span class="hero-stat-value mono">${value}</span>`;
      bar.appendChild(div);
    });
  }

  // ═══════════════════════════════════════════════════════════
  // NAV RECORDS
  // ═══════════════════════════════════════════════════════════
  async function loadNAV() {
    try {
      const schemeId = document.getElementById('nav-scheme-filter').value;
      let url = '/accounting/nav/';
      const params = [];
      if (schemeId) params.push(`scheme=${schemeId}`);
      const reconciled = document.getElementById('nav-reconciled-filter').value;
      if (reconciled) params.push(`reconciled=${reconciled}`);
      if (params.length) url += '?' + params.join('&');
      navRecords = await Auth.apiGet(url);
      renderNAV();
    } catch (e) { console.error('Failed to load NAV records:', e); }
  }

  function renderNAV() {
    const container = document.getElementById('nav-list');
    container.innerHTML = '';

    if (!navRecords.length) {
      container.innerHTML = `<div class="acc-empty">No NAV records found. Create one using the button above.</div>`;
      return;
    }

    navRecords.forEach(nav => {
      const reconBadge = nav.depository_reconciled
        ? `<span class="reconciled-yes">✓ ${esc(nav.depository_type?.toUpperCase())} Reconciled</span>`
        : `<span class="reconciled-no">⚠ Unreconciled</span>`;

      const card = document.createElement('div');
      card.className = 'nav-card';
      card.innerHTML = `
        <div class="nav-card-header">
          <div>
            <div class="nav-card-title">${esc(nav.scheme_name)} · ${fmtDate(nav.nav_date)}</div>
            <div class="nav-card-meta">Depository: ${esc(nav.depository_type || '—')} · Posted: ${fmtDate(nav.created_at)}</div>
          </div>
          ${reconBadge}
        </div>
        <div class="nav-card-metrics">
          <div class="nav-metric">
            <span class="label">Total NAV</span>
            <span class="value">₹${fmtCurrency(nav.total_nav)}</span>
          </div>
          <div class="nav-metric">
            <span class="label">Units Outstanding</span>
            <span class="value">${nav.total_units_outstanding ? parseFloat(nav.total_units_outstanding).toLocaleString('en-IN', {maximumFractionDigits: 4}) : '—'}</span>
          </div>
          <div class="nav-metric">
            <span class="label">NAV per Unit</span>
            <span class="value highlight">₹${nav.nav_per_unit ? parseFloat(nav.nav_per_unit).toFixed(4) : '—'}</span>
          </div>
          ${nav.depository_variance_amount ? `
          <div class="nav-metric">
            <span class="label">Depository Variance</span>
            <span class="value" style="color:var(--accent-red);">₹${fmtCurrency(nav.depository_variance_amount)}</span>
          </div>` : ''}
        </div>
        <div class="nav-breakdown">
          <div class="breakdown-item">
            <span class="label">Investments (FV)</span>
            <span class="value">₹${fmtCurrency(nav.investments_at_fair_value)}</span>
          </div>
          <div class="breakdown-item">
            <span class="label">Cash & Equivalents</span>
            <span class="value">₹${fmtCurrency(nav.cash_and_equivalents)}</span>
          </div>
          <div class="breakdown-item">
            <span class="label">Receivables</span>
            <span class="value">₹${fmtCurrency(nav.receivables)}</span>
          </div>
          <div class="breakdown-item">
            <span class="label">Mgmt Fee Payable</span>
            <span class="value" style="color:var(--accent-red);">₹${fmtCurrency(nav.management_fee_payable)}</span>
          </div>
          <div class="breakdown-item">
            <span class="label">Other Liabilities</span>
            <span class="value" style="color:var(--accent-red);">₹${fmtCurrency(nav.other_liabilities)}</span>
          </div>
        </div>
        <div class="card-actions" style="margin-top:12px;">
          <button class="btn-action" data-id="${nav.id}" data-action="edit-nav">Edit</button>
        </div>
      `;
      container.appendChild(card);

      card.querySelector('[data-action="edit-nav"]').onclick = async () => {
        try {
          const detail = await Auth.apiGet(`/accounting/nav/${nav.id}/`);
          openNAVForm(detail);
        } catch (e) { openNAVForm(nav); }
      };
    });
  }

  function openNAVForm(existing = null) {
    const isEdit = !!existing;
    const schemeOpts = [{value: '', label: '— Select Scheme —'}].concat(
      schemes.map(s => ({value: s.id, label: `${s.fund_name} → ${s.name}`}))
    );

    openModal(isEdit ? 'Edit NAV Record' : 'Record NAV', [
      {name: 'scheme', label: 'Scheme', type: 'select', required: true, options: schemeOpts, default: existing?.scheme || ''},
      {name: 'nav_date', label: 'NAV Date', type: 'date', required: true, default: existing?.nav_date || ''},
      {name: 'total_nav', label: 'Total NAV (₹)', type: 'number', required: true, step: '0.01', default: existing?.total_nav || ''},
      {name: 'total_units_outstanding', label: 'Total Units Outstanding', type: 'number', required: true, step: '0.0001', default: existing?.total_units_outstanding || ''},
      {name: 'nav_per_unit', label: 'NAV per Unit (₹)', type: 'number', required: true, step: '0.0001', default: existing?.nav_per_unit || ''},
      {name: 'investments_at_fair_value', label: 'Investments at Fair Value (₹)', type: 'number', step: '0.01', default: existing?.investments_at_fair_value || ''},
      {name: 'cash_and_equivalents', label: 'Cash & Equivalents (₹)', type: 'number', step: '0.01', default: existing?.cash_and_equivalents || ''},
      {name: 'receivables', label: 'Receivables (₹)', type: 'number', step: '0.01', default: existing?.receivables || ''},
      {name: 'management_fee_payable', label: 'Management Fee Payable (₹)', type: 'number', step: '0.01', default: existing?.management_fee_payable || ''},
      {name: 'other_liabilities', label: 'Other Liabilities (₹)', type: 'number', step: '0.01', default: existing?.other_liabilities || ''},
      {name: 'depository_type', label: 'Depository', type: 'select', default: existing?.depository_type || 'cdsl', options: [
        {value: 'cdsl', label: 'CDSL'},
        {value: 'nsdl', label: 'NSDL'},
      ]},
      {name: 'depository_variance_amount', label: 'Depository Variance (₹)', type: 'number', step: '0.01', default: existing?.depository_variance_amount || ''},
    ], async (data) => {
      // Auto-set depository_reconciled based on variance
      const variance = parseFloat(data.depository_variance_amount || 0);
      data.depository_reconciled = variance === 0;
      if (isEdit) {
        await Auth.apiPut(`/accounting/nav/${existing.id}/`, data);
      } else {
        await Auth.apiPost('/accounting/nav/', data);
      }
      await loadNAV();
      renderStats();
    });
  }

  // ═══════════════════════════════════════════════════════════
  // CARRIED INTEREST
  // ═══════════════════════════════════════════════════════════
  async function loadCarry() {
    try {
      const schemeId = document.getElementById('carry-scheme-filter').value;
      const url = schemeId ? `/accounting/carry/?scheme=${schemeId}` : '/accounting/carry/';
      carryRecords = await Auth.apiGet(url);
      renderCarry();
    } catch (e) { console.error('Failed to load carry records:', e); }
  }

  function renderCarry() {
    const container = document.getElementById('carry-list');
    container.innerHTML = '';

    if (!carryRecords.length) {
      container.innerHTML = `<div class="acc-empty">No carried interest records found.</div>`;
      return;
    }

    carryRecords.forEach(carry => {
      const statusCls = `carry-${carry.calculation_status}`;
      const card = document.createElement('div');
      card.className = 'carry-card';
      card.innerHTML = `
        <div class="carry-card-header">
          <div>
            <div class="carry-card-title">${esc(carry.scheme_name)} · Carry Calc</div>
            <div class="carry-card-meta">Calculation Date: ${fmtDate(carry.calculation_date)}</div>
          </div>
          <span class="carry-badge ${statusCls}">${esc(carry.status_display)}</span>
        </div>
        <div class="waterfall-grid">
          <div class="waterfall-step">
            <div class="step-label">Total Distributions</div>
            <div class="step-value">₹${fmtCurrency(carry.total_distributions)}</div>
          </div>
          <div class="waterfall-step">
            <div class="step-label">Called Capital</div>
            <div class="step-value">₹${fmtCurrency(carry.total_called_capital)}</div>
          </div>
          <div class="waterfall-step">
            <div class="step-label">Preferred Return</div>
            <div class="step-value">₹${fmtCurrency(carry.preferred_return_amount)}</div>
          </div>
          <div class="waterfall-step">
            <div class="step-label">Carry Base</div>
            <div class="step-value">₹${fmtCurrency(carry.carry_base)}</div>
          </div>
          <div class="waterfall-step carry-highlight">
            <div class="step-label">Gross Carry</div>
            <div class="step-value">₹${fmtCurrency(carry.carry_amount_gross)}</div>
          </div>
          <div class="waterfall-step carry-highlight">
            <div class="step-label">Net Carry (after clawback)</div>
            <div class="step-value">₹${fmtCurrency(carry.carry_amount_net)}</div>
          </div>
          ${carry.gp_clawback_provision ? `
          <div class="waterfall-step">
            <div class="step-label">GP Clawback Provision</div>
            <div class="step-value" style="color:var(--accent-red);">₹${fmtCurrency(carry.gp_clawback_provision)}</div>
          </div>` : ''}
        </div>
        ${carry.notes ? `<p style="font-size:12px;color:var(--text-muted);margin-bottom:12px;">${esc(carry.notes)}</p>` : ''}
        <div class="card-actions">
          <button class="btn-action" data-id="${carry.id}" data-action="edit-carry">Edit</button>
        </div>
      `;
      container.appendChild(card);

      card.querySelector('[data-action="edit-carry"]').onclick = () => openCarryForm(carry);
    });
  }

  function openCarryForm(existing = null) {
    const isEdit = !!existing;
    const schemeOpts = [{value: '', label: '— Select Scheme —'}].concat(
      schemes.map(s => ({value: s.id, label: `${s.fund_name} → ${s.name}`}))
    );

    openModal(isEdit ? 'Edit Carry Calculation' : 'Compute Carried Interest', [
      {name: 'scheme', label: 'Scheme', type: 'select', required: true, options: schemeOpts, default: existing?.scheme || ''},
      {name: 'calculation_date', label: 'Calculation Date', type: 'date', required: true, default: existing?.calculation_date || ''},
      {name: 'total_distributions', label: 'Total Distributions (₹)', type: 'number', required: true, step: '0.01', default: existing?.total_distributions || ''},
      {name: 'total_called_capital', label: 'Total Called Capital (₹)', type: 'number', required: true, step: '0.01', default: existing?.total_called_capital || ''},
      {name: 'preferred_return_amount', label: 'Preferred Return Amount (₹)', type: 'number', step: '0.01', default: existing?.preferred_return_amount || ''},
      {name: 'carry_base', label: 'Carry Base (₹)', type: 'number', step: '0.01', default: existing?.carry_base || ''},
      {name: 'carry_amount_gross', label: 'Gross Carry Amount (₹)', type: 'number', step: '0.01', default: existing?.carry_amount_gross || ''},
      {name: 'carry_amount_net', label: 'Net Carry Amount (₹)', type: 'number', step: '0.01', default: existing?.carry_amount_net || ''},
      {name: 'gp_clawback_provision', label: 'GP Clawback Provision (₹)', type: 'number', step: '0.01', default: existing?.gp_clawback_provision || ''},
      {name: 'calculation_status', label: 'Status', type: 'select', default: existing?.calculation_status || 'indicative', options: [
        {value: 'indicative', label: 'Indicative'},
        {value: 'crystallised', label: 'Crystallised'},
        {value: 'paid', label: 'Paid'},
      ]},
      {name: 'notes', label: 'Notes', type: 'textarea', default: existing?.notes || ''},
    ], async (data) => {
      if (isEdit) {
        await Auth.apiPut(`/accounting/carry/${existing.id}/`, data);
      } else {
        await Auth.apiPost('/accounting/carry/', data);
      }
      await loadCarry();
    });
  }

  // ═══════════════════════════════════════════════════════════
  // FUND LEDGER
  // ═══════════════════════════════════════════════════════════
  async function loadLedger() {
    try {
      const schemeId = document.getElementById('ledger-scheme-filter').value;
      const refType = document.getElementById('ledger-ref-filter').value;
      const params = [];
      if (schemeId) params.push(`scheme=${schemeId}`);
      if (refType) params.push(`reference_type=${refType}`);
      const url = '/accounting/ledger/' + (params.length ? '?' + params.join('&') : '');
      ledgerEntries = await Auth.apiGet(url);
      renderLedger();
    } catch (e) { console.error('Failed to load ledger:', e); }
  }

  function renderLedger() {
    const tbody = document.getElementById('ledger-tbody');
    tbody.innerHTML = '';

    if (!ledgerEntries.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="acc-empty">No journal entries found.</td></tr>`;
      return;
    }

    ledgerEntries.forEach(entry => {
      const tr = document.createElement('tr');
      if (entry.is_reversed) tr.classList.add('ledger-reversed');
      tr.innerHTML = `
        <td class="ledger-entry-no">${esc(entry.journal_entry_number)}</td>
        <td style="font-size:12px;">${fmtDate(entry.entry_date)}</td>
        <td style="font-size:12px;max-width:200px;">${esc(entry.description)}</td>
        <td style="font-size:12px;">${esc(entry.debit_account_name)}</td>
        <td style="font-size:12px;">${esc(entry.credit_account_name)}</td>
        <td class="ledger-amount">₹${fmtCurrency(entry.amount)}</td>
        <td style="font-size:11px;font-family:var(--font-mono);">${esc(entry.reference_type_display)}</td>
        <td style="font-size:11px;font-family:var(--font-mono);max-width:120px;overflow:hidden;text-overflow:ellipsis;">${entry.reference_id ? esc(entry.reference_id.substring(0, 8)) + '…' : '—'}</td>
        <td>${entry.is_reversed ? '<span class="fee-badge fee-waived">REVERSED</span>' : '<span class="fee-badge fee-paid">POSTED</span>'}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  function openLedgerForm() {
    const schemeOpts = [{value: '', label: '— Select Scheme —'}].concat(
      schemes.map(s => ({value: s.id, label: `${s.fund_name} → ${s.name}`}))
    );
    const acctOpts = [{value: '', label: '— Select Account —'}].concat(
      coaAccounts.map(a => ({value: a.id, label: `${a.account_code} — ${a.account_name}`}))
    );

    openModal('Post Journal Entry', [
      {name: 'scheme', label: 'Scheme', type: 'select', required: true, options: schemeOpts},
      {name: 'entry_date', label: 'Entry Date', type: 'date', required: true},
      {name: 'description', label: 'Description', type: 'textarea', required: true},
      {name: 'debit_account', label: 'Debit Account', type: 'select', required: true, options: acctOpts},
      {name: 'credit_account', label: 'Credit Account', type: 'select', required: true, options: acctOpts},
      {name: 'amount', label: 'Amount (₹)', type: 'number', required: true, step: '0.01'},
      {name: 'reference_type', label: 'Reference Type', type: 'select', default: 'other', options: [
        {value: 'capital_call', label: 'Capital Call'},
        {value: 'distribution', label: 'Distribution'},
        {value: 'investment', label: 'Investment'},
        {value: 'valuation', label: 'Valuation'},
        {value: 'management_fee', label: 'Management Fee'},
        {value: 'carried_interest', label: 'Carried Interest'},
        {value: 'other', label: 'Other'},
      ]},
      {name: 'reference_id', label: 'Reference ID (UUID, optional)', placeholder: 'UUID of linked object'},
    ], async (data) => {
      if (!data.reference_id) delete data.reference_id;
      await Auth.apiPost('/accounting/ledger/', data);
      await loadLedger();
      renderStats();
    });
  }

  // ═══════════════════════════════════════════════════════════
  // MANAGEMENT FEES
  // ═══════════════════════════════════════════════════════════
  async function loadFees() {
    try {
      const schemeId = document.getElementById('fee-scheme-filter').value;
      const url = schemeId ? `/accounting/fees/?scheme=${schemeId}` : '/accounting/fees/';
      feeSchedules = await Auth.apiGet(url);
      renderFees();
    } catch (e) { console.error('Failed to load management fees:', e); }
  }

  function renderFees() {
    const tbody = document.getElementById('fee-tbody');
    tbody.innerHTML = '';

    if (!feeSchedules.length) {
      tbody.innerHTML = `<tr><td colspan="10" class="acc-empty">No fee periods found.</td></tr>`;
      return;
    }

    feeSchedules.forEach(fee => {
      const statusCls = `fee-${fee.fee_status}`;
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>
          <div style="font-weight:600;font-size:13px;">${esc(fee.scheme_name)}</div>
        </td>
        <td style="font-size:12px;font-family:var(--font-mono);">${fmtDate(fee.period_start)} → ${fmtDate(fee.period_end)}</td>
        <td style="font-family:var(--font-mono);font-size:12px;">₹${fmtCurrency(fee.fee_basis_amount)}</td>
        <td style="font-family:var(--font-mono);">${fee.fee_rate ? parseFloat(fee.fee_rate).toFixed(4) + '%' : '—'}</td>
        <td style="font-family:var(--font-mono);font-weight:600;">₹${fmtCurrency(fee.fee_amount)}</td>
        <td style="font-family:var(--font-mono);color:var(--text-muted);">₹${fmtCurrency(fee.gst_amount)}</td>
        <td style="font-family:var(--font-mono);font-weight:700;color:var(--accent-blue);">₹${fmtCurrency(fee.total_fee_with_gst)}</td>
        <td style="font-size:11px;font-family:var(--font-mono);">${esc(fee.invoice_number) || '—'}</td>
        <td><span class="fee-badge ${statusCls}">${esc(fee.status_display)}</span></td>
        <td>
          <button class="btn-action" data-id="${fee.id}" data-action="edit-fee">Edit</button>
        </td>
      `;
      tbody.appendChild(tr);

      tr.querySelector('[data-action="edit-fee"]').onclick = () => openFeeForm(fee);
    });
  }

  function openFeeForm(existing = null) {
    const isEdit = !!existing;
    const schemeOpts = [{value: '', label: '— Select Scheme —'}].concat(
      schemes.map(s => ({value: s.id, label: `${s.fund_name} → ${s.name}`}))
    );

    openModal(isEdit ? 'Edit Fee Period' : 'Add Fee Period', [
      {name: 'scheme', label: 'Scheme', type: 'select', required: true, options: schemeOpts, default: existing?.scheme || ''},
      {name: 'period_start', label: 'Period Start', type: 'date', required: true, default: existing?.period_start || ''},
      {name: 'period_end', label: 'Period End', type: 'date', required: true, default: existing?.period_end || ''},
      {name: 'fee_basis_amount', label: 'Fee Basis Amount (₹)', type: 'number', required: true, step: '0.01', default: existing?.fee_basis_amount || ''},
      {name: 'fee_rate', label: 'Fee Rate (%)', type: 'number', required: true, step: '0.0001', default: existing?.fee_rate || ''},
      {name: 'fee_amount', label: 'Fee Amount (₹)', type: 'number', step: '0.01', default: existing?.fee_amount || ''},
      {name: 'gst_amount', label: 'GST Amount (₹)', type: 'number', step: '0.01', default: existing?.gst_amount || ''},
      {name: 'total_fee_with_gst', label: 'Total Fee with GST (₹)', type: 'number', step: '0.01', default: existing?.total_fee_with_gst || ''},
      {name: 'fee_status', label: 'Status', type: 'select', default: existing?.fee_status || 'draft', options: [
        {value: 'draft', label: 'Draft'},
        {value: 'invoiced', label: 'Invoiced'},
        {value: 'paid', label: 'Paid'},
        {value: 'waived', label: 'Waived'},
      ]},
      {name: 'invoice_number', label: 'Invoice Number', default: existing?.invoice_number || ''},
      {name: 'invoice_date', label: 'Invoice Date', type: 'date', default: existing?.invoice_date || ''},
    ], async (data) => {
      if (!data.invoice_date) delete data.invoice_date;
      if (isEdit) {
        await Auth.apiPut(`/accounting/fees/${existing.id}/`, data);
      } else {
        await Auth.apiPost('/accounting/fees/', data);
      }
      await loadFees();
    });
  }

  // ═══════════════════════════════════════════════════════════
  // CHART OF ACCOUNTS
  // ═══════════════════════════════════════════════════════════
  async function loadCOA() {
    try {
      coaAccounts = await Auth.apiGet('/accounting/chart-of-accounts/');
      renderCOA();
    } catch (e) { console.error('Failed to load chart of accounts:', e); }
  }

  function renderCOA() {
    const typeFilter = document.getElementById('coa-type-filter').value;
    let list = coaAccounts;
    if (typeFilter) list = list.filter(a => a.account_type === typeFilter);

    const tbody = document.getElementById('coa-tbody');
    tbody.innerHTML = '';

    if (!list.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="acc-empty">No accounts found.</td></tr>`;
      return;
    }

    list.forEach(acc => {
      const typeCls = `acc-type-${acc.account_type}`;
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td style="font-family:var(--font-mono);font-size:12px;font-weight:700;">${esc(acc.account_code)}</td>
        <td style="font-weight:600;">${esc(acc.account_name)}</td>
        <td><span class="acc-type-badge ${typeCls}">${esc(acc.account_type_display)}</span></td>
        <td style="font-size:12px;">${esc(acc.parent_account_name) || '—'}</td>
        <td style="font-size:12px;color:var(--text-muted);max-width:200px;">${esc(acc.description) || '—'}</td>
        <td>${acc.is_active
          ? '<span class="fee-badge fee-paid">Active</span>'
          : '<span class="fee-badge fee-waived">Inactive</span>'
        }</td>
        <td>
          <button class="btn-action" data-id="${acc.id}" data-action="edit-account">Edit</button>
        </td>
      `;
      tbody.appendChild(tr);

      tr.querySelector('[data-action="edit-account"]').onclick = () => openCOAForm(acc);
    });
  }

  function openCOAForm(existing = null) {
    const isEdit = !!existing;
    const parentOpts = [{value: '', label: '— None (Top Level) —'}].concat(
      coaAccounts
        .filter(a => !existing || a.id !== existing.id)
        .map(a => ({value: a.id, label: `${a.account_code} — ${a.account_name}`}))
    );

    openModal(isEdit ? 'Edit Account' : 'Add Account', [
      {name: 'account_code', label: 'Account Code', required: true, placeholder: 'e.g., 1001', default: existing?.account_code || ''},
      {name: 'account_name', label: 'Account Name', required: true, default: existing?.account_name || ''},
      {name: 'account_type', label: 'Account Type', type: 'select', required: true, default: existing?.account_type || 'asset', options: [
        {value: 'asset', label: 'Asset'},
        {value: 'liability', label: 'Liability'},
        {value: 'equity', label: 'Equity'},
        {value: 'income', label: 'Income'},
        {value: 'expense', label: 'Expense'},
      ]},
      {name: 'parent_account', label: 'Parent Account (optional)', type: 'select', options: parentOpts, default: existing?.parent_account || ''},
      {name: 'description', label: 'Description', type: 'textarea', default: existing?.description || ''},
    ], async (data) => {
      if (!data.parent_account) delete data.parent_account;
      if (isEdit) {
        await Auth.apiPut(`/accounting/chart-of-accounts/${existing.id}/`, data);
      } else {
        await Auth.apiPost('/accounting/chart-of-accounts/', data);
      }
      await loadCOA();
      renderStats();
    });
  }

  // ═══════════════════════════════════════════════════════════
  // TRIAL BALANCE
  // ═══════════════════════════════════════════════════════════
  async function generateTrialBalance() {
    const schemeId = document.getElementById('tb-scheme-select').value;
    const asOfDate = document.getElementById('tb-date').value;
    const output = document.getElementById('trial-balance-output');

    if (!schemeId) {
      output.innerHTML = `<div class="acc-empty">Please select a scheme to generate the trial balance.</div>`;
      return;
    }

    output.innerHTML = `<div class="acc-empty">Generating trial balance…</div>`;

    try {
      let url = `/accounting/schemes/${schemeId}/trial-balance/`;
      if (asOfDate) url += `?as_of=${asOfDate}`;
      const data = await Auth.apiGet(url);
      renderTrialBalance(data, asOfDate);
    } catch (e) {
      // Fallback: compute from ledger entries already loaded for this scheme
      renderTrialBalanceFallback(schemeId, asOfDate);
    }
  }

  function renderTrialBalance(data, asOfDate) {
    const output = document.getElementById('trial-balance-output');
    const rows = data.accounts || data;
    if (!rows || !rows.length) {
      output.innerHTML = `<div class="acc-empty">No ledger entries found for this scheme.</div>`;
      return;
    }

    const totalDebit = rows.reduce((s, r) => s + parseFloat(r.total_debit || 0), 0);
    const totalCredit = rows.reduce((s, r) => s + parseFloat(r.total_credit || 0), 0);
    const balanced = Math.abs(totalDebit - totalCredit) < 0.01;

    output.innerHTML = `
      <div class="tb-header">
        <span class="tb-title">Trial Balance${asOfDate ? ' — as of ' + fmtDate(asOfDate) : ''}</span>
        <span class="tb-balance-badge ${balanced ? 'balanced' : 'unbalanced'}">
          ${balanced ? '✓ Balanced' : '⚠ Out of Balance by ₹' + fmtCurrency(Math.abs(totalDebit - totalCredit))}
        </span>
      </div>
      <div class="acc-table-wrap">
        <table class="acc-table">
          <thead>
            <tr>
              <th>Account Code</th>
              <th>Account Name</th>
              <th>Type</th>
              <th style="text-align:right;">Debit (₹)</th>
              <th style="text-align:right;">Credit (₹)</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(r => `
              <tr>
                <td style="font-family:var(--font-mono);font-size:12px;">${esc(r.account_code)}</td>
                <td>${esc(r.account_name)}</td>
                <td><span class="acc-type-badge acc-type-${r.account_type}">${esc(r.account_type_display || r.account_type)}</span></td>
                <td style="text-align:right;font-family:var(--font-mono);">${r.total_debit ? '₹' + fmtCurrency(r.total_debit) : '—'}</td>
                <td style="text-align:right;font-family:var(--font-mono);">${r.total_credit ? '₹' + fmtCurrency(r.total_credit) : '—'}</td>
              </tr>
            `).join('')}
            <tr class="tb-totals-row">
              <td colspan="3" style="font-weight:700;">TOTALS</td>
              <td style="text-align:right;font-weight:700;font-family:var(--font-mono);">₹${fmtCurrency(totalDebit)}</td>
              <td style="text-align:right;font-weight:700;font-family:var(--font-mono);">₹${fmtCurrency(totalCredit)}</td>
            </tr>
          </tbody>
        </table>
      </div>
    `;
  }

  function renderTrialBalanceFallback(schemeId, asOfDate) {
    const output = document.getElementById('trial-balance-output');
    // Build from local ledger entries filtered by scheme
    const entries = ledgerEntries.filter(e => {
      if (String(e.scheme) !== String(schemeId)) return false;
      if (asOfDate && e.entry_date > asOfDate) return false;
      return true;
    });

    if (!entries.length) {
      output.innerHTML = `<div class="acc-empty">No ledger entries found for this scheme. Post journal entries first.</div>`;
      return;
    }

    // Aggregate by account from coaAccounts
    const accMap = {};
    const getAcc = (id) => {
      if (!id) return null;
      if (!accMap[id]) {
        const a = coaAccounts.find(c => String(c.id) === String(id)) || { account_code: id, account_name: '(Unknown)', account_type: '' };
        accMap[id] = { ...a, total_debit: 0, total_credit: 0 };
      }
      return accMap[id];
    };

    entries.forEach(e => {
      const debitAcc = getAcc(e.debit_account);
      const creditAcc = getAcc(e.credit_account);
      const amt = parseFloat(e.amount || 0);
      if (debitAcc) debitAcc.total_debit += amt;
      if (creditAcc) creditAcc.total_credit += amt;
    });

    const rows = Object.values(accMap).filter(a => a.total_debit > 0 || a.total_credit > 0);
    renderTrialBalance({ accounts: rows }, asOfDate);
  }

  // ═══════════════════════════════════════════════════════════
  // FINANCIAL STATEMENTS
  // ═══════════════════════════════════════════════════════════
  async function generateFinancials() {
    const schemeId = document.getElementById('fin-scheme-select').value;
    const stmtType = document.getElementById('fin-type').value;
    const output = document.getElementById('financials-output');

    if (!schemeId) {
      output.innerHTML = `<div class="acc-empty">Please select a scheme to generate financials.</div>`;
      return;
    }

    output.innerHTML = `<div class="acc-empty">Generating financial statement…</div>`;

    try {
      const data = await Auth.apiGet(`/accounting/schemes/${schemeId}/financials/${stmtType}/`);
      renderFinancials(stmtType, data);
    } catch (e) {
      renderFinancialsFallback(schemeId, stmtType);
    }
  }

  function renderFinancials(type, data) {
    const output = document.getElementById('financials-output');
    const titles = { bs: 'Balance Sheet', is: 'Income Statement', cf: 'Cash Flow Statement' };
    const title = titles[type] || 'Financial Statement';

    if (type === 'bs') {
      const assets = data.assets || [];
      const liabilities = data.liabilities || [];
      const equity = data.equity || [];
      const totalAssets = data.total_assets || assets.reduce((s, r) => s + parseFloat(r.balance || 0), 0);
      const totalLiab = data.total_liabilities || liabilities.reduce((s, r) => s + parseFloat(r.balance || 0), 0);
      const totalEquity = data.total_equity || equity.reduce((s, r) => s + parseFloat(r.balance || 0), 0);

      output.innerHTML = `
        <div class="fin-header"><span class="fin-title">${title}</span><span class="fin-subtitle">Ind AS 109 Compliant</span></div>
        <div class="fin-columns">
          <div class="fin-section">
            <div class="fin-section-title">Assets</div>
            ${assets.map(r => `<div class="fin-row"><span>${esc(r.account_name)}</span><span class="mono">₹${fmtCurrency(r.balance)}</span></div>`).join('') || '<div class="fin-row muted">No asset accounts</div>'}
            <div class="fin-row fin-total"><span>Total Assets</span><span class="mono">₹${fmtCurrency(totalAssets)}</span></div>
          </div>
          <div class="fin-section">
            <div class="fin-section-title">Liabilities</div>
            ${liabilities.map(r => `<div class="fin-row"><span>${esc(r.account_name)}</span><span class="mono">₹${fmtCurrency(r.balance)}</span></div>`).join('') || '<div class="fin-row muted">No liability accounts</div>'}
            <div class="fin-row fin-total"><span>Total Liabilities</span><span class="mono">₹${fmtCurrency(totalLiab)}</span></div>
            <div class="fin-section-title" style="margin-top:24px;">Equity</div>
            ${equity.map(r => `<div class="fin-row"><span>${esc(r.account_name)}</span><span class="mono">₹${fmtCurrency(r.balance)}</span></div>`).join('') || '<div class="fin-row muted">No equity accounts</div>'}
            <div class="fin-row fin-total"><span>Total Equity</span><span class="mono">₹${fmtCurrency(totalEquity)}</span></div>
            <div class="fin-row fin-total fin-grand-total"><span>Total Liabilities + Equity</span><span class="mono">₹${fmtCurrency(totalLiab + totalEquity)}</span></div>
          </div>
        </div>
      `;
    } else if (type === 'is') {
      const income = data.income || [];
      const expenses = data.expenses || [];
      const totalIncome = data.total_income || income.reduce((s, r) => s + parseFloat(r.balance || 0), 0);
      const totalExpenses = data.total_expenses || expenses.reduce((s, r) => s + parseFloat(r.balance || 0), 0);
      const netIncome = totalIncome - totalExpenses;

      output.innerHTML = `
        <div class="fin-header"><span class="fin-title">${title}</span><span class="fin-subtitle">Ind AS 109 Compliant</span></div>
        <div class="fin-section" style="max-width:700px;">
          <div class="fin-section-title">Income</div>
          ${income.map(r => `<div class="fin-row"><span>${esc(r.account_name)}</span><span class="mono">₹${fmtCurrency(r.balance)}</span></div>`).join('') || '<div class="fin-row muted">No income recorded</div>'}
          <div class="fin-row fin-total"><span>Total Income</span><span class="mono">₹${fmtCurrency(totalIncome)}</span></div>
          <div class="fin-section-title" style="margin-top:24px;">Expenses</div>
          ${expenses.map(r => `<div class="fin-row"><span>${esc(r.account_name)}</span><span class="mono">₹${fmtCurrency(r.balance)}</span></div>`).join('') || '<div class="fin-row muted">No expenses recorded</div>'}
          <div class="fin-row fin-total"><span>Total Expenses</span><span class="mono" style="color:var(--accent-red);">₹${fmtCurrency(totalExpenses)}</span></div>
          <div class="fin-row fin-total fin-grand-total" style="border-top:2px solid var(--accent-cyan);">
            <span>Net Income / (Loss)</span>
            <span class="mono" style="color:${netIncome >= 0 ? 'var(--accent-green)' : 'var(--accent-red)'};">₹${fmtCurrency(Math.abs(netIncome))} ${netIncome < 0 ? '(Loss)' : ''}</span>
          </div>
        </div>
      `;
    } else if (type === 'cf') {
      const operating = data.operating || [];
      const investing = data.investing || [];
      const financing = data.financing || [];
      const netCF = (data.net_cash_flow) || 0;

      output.innerHTML = `
        <div class="fin-header"><span class="fin-title">${title}</span><span class="fin-subtitle">Ind AS 7 Compliant</span></div>
        <div class="fin-section" style="max-width:700px;">
          <div class="fin-section-title">Operating Activities</div>
          ${operating.map(r => `<div class="fin-row"><span>${esc(r.description)}</span><span class="mono">${r.amount < 0 ? '(' : ''}₹${fmtCurrency(Math.abs(r.amount))}${r.amount < 0 ? ')' : ''}</span></div>`).join('') || '<div class="fin-row muted">No operating flows</div>'}
          <div class="fin-section-title" style="margin-top:24px;">Investing Activities</div>
          ${investing.map(r => `<div class="fin-row"><span>${esc(r.description)}</span><span class="mono">${r.amount < 0 ? '(' : ''}₹${fmtCurrency(Math.abs(r.amount))}${r.amount < 0 ? ')' : ''}</span></div>`).join('') || '<div class="fin-row muted">No investing flows</div>'}
          <div class="fin-section-title" style="margin-top:24px;">Financing Activities</div>
          ${financing.map(r => `<div class="fin-row"><span>${esc(r.description)}</span><span class="mono">${r.amount < 0 ? '(' : ''}₹${fmtCurrency(Math.abs(r.amount))}${r.amount < 0 ? ')' : ''}</span></div>`).join('') || '<div class="fin-row muted">No financing flows</div>'}
          <div class="fin-row fin-total fin-grand-total" style="margin-top:16px;border-top:2px solid var(--accent-cyan);">
            <span>Net Change in Cash</span>
            <span class="mono" style="color:${netCF >= 0 ? 'var(--accent-green)' : 'var(--accent-red)'};">₹${fmtCurrency(Math.abs(netCF))} ${netCF < 0 ? '(Outflow)' : '(Inflow)'}</span>
          </div>
        </div>
      `;
    }
  }

  function renderFinancialsFallback(schemeId, type) {
    const output = document.getElementById('financials-output');
    const schemeEntries = ledgerEntries.filter(e => String(e.scheme) === String(schemeId));

    if (!schemeEntries.length) {
      output.innerHTML = `<div class="acc-empty">No ledger entries found for this scheme. Post journal entries first.</div>`;
      return;
    }

    // Build account balances from COA + ledger
    const accBalance = {};
    schemeEntries.forEach(e => {
      const amt = parseFloat(e.amount || 0);
      if (e.debit_account) accBalance[e.debit_account] = (accBalance[e.debit_account] || 0) + amt;
      if (e.credit_account) accBalance[e.credit_account] = (accBalance[e.credit_account] || 0) - amt;
    });

    const rows = Object.entries(accBalance).map(([id, bal]) => {
      const acc = coaAccounts.find(a => String(a.id) === String(id)) || { account_name: id, account_type: 'asset' };
      return { ...acc, balance: Math.abs(bal) };
    });

    const byType = (t) => rows.filter(r => r.account_type === t);

    const data = {
      assets: byType('asset'),
      liabilities: byType('liability'),
      equity: byType('equity'),
      income: byType('income'),
      expenses: byType('expense'),
    };

    renderFinancials(type, data);
  }

  function exportFinancialsPDF() {
    const schemeId = document.getElementById('fin-scheme-select').value;
    const stmtType = document.getElementById('fin-type').value;
    const titles = { bs: 'Balance Sheet', is: 'Income Statement', cf: 'Cash Flow Statement' };
    if (!schemeId) { alert('Please select a scheme and generate the statement first.'); return; }
    const output = document.getElementById('financials-output');
    if (!output.innerHTML.trim() || output.querySelector('.acc-empty')) {
      alert('Please generate the financial statement first before exporting.'); return;
    }
    // PDF export via print dialog (browser handles PDF rendering)
    const printWin = window.open('', '_blank');
    const scheme = schemes.find(s => String(s.id) === String(schemeId));
    printWin.document.write(`
      <!DOCTYPE html><html><head>
      <title>${titles[stmtType]} — ${scheme?.name || 'Scheme'}</title>
      <style>
        body { font-family: sans-serif; margin: 40px; color: #111; }
        h1 { font-size: 20px; margin-bottom: 4px; }
        h2 { font-size: 14px; color: #555; margin-bottom: 24px; }
        .fin-row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #eee; }
        .fin-total { font-weight: bold; border-top: 2px solid #333; }
        .fin-grand-total { border-top: 3px double #333; font-size: 15px; }
        .fin-section-title { font-weight: bold; margin-top: 20px; margin-bottom: 8px; color: #333; text-transform: uppercase; font-size: 12px; letter-spacing: 1px; }
      </style></head><body>
      <h1>${titles[stmtType]}</h1>
      <h2>${scheme?.fund_name || ''} — ${scheme?.name || ''}</h2>
      ${output.innerHTML}
      </body></html>
    `);
    printWin.document.close();
    printWin.print();
  }

  // ═══════════════════════════════════════════════════════════
  // TALLY ERP SYNC
  // ═══════════════════════════════════════════════════════════
  async function tallyImport() {
    const schemeId = document.getElementById('tally-scheme-select').value;
    const fileInput = document.getElementById('tally-import-file');
    const statusEl = document.getElementById('tally-import-status');

    if (!schemeId) { statusEl.innerHTML = `<div class="tally-status tally-error">Please select a scheme first.</div>`; return; }
    if (!fileInput.files[0]) { statusEl.innerHTML = `<div class="tally-status tally-error">Please select a Tally export file.</div>`; return; }

    statusEl.innerHTML = `<div class="tally-status tally-pending">Uploading and parsing file…</div>`;

    const formData = new FormData();
    formData.append('file', fileInput.files[0]);
    formData.append('scheme', schemeId);

    try {
      const result = await Auth.apiUpload(`/accounting/schemes/${schemeId}/tally/import/`, formData);
      statusEl.innerHTML = `
        <div class="tally-status tally-success">
          ✓ Import complete — ${result.entries_created || 0} journal entries created from ${result.rows_processed || 0} rows.
          ${result.warnings?.length ? `<br><span style="color:var(--accent-yellow);">Warnings: ${result.warnings.join('; ')}</span>` : ''}
        </div>`;
      await loadLedger();
    } catch (e) {
      statusEl.innerHTML = `<div class="tally-status tally-error">Import failed: ${esc(e.message)}</div>`;
    }
  }

  async function tallyExport() {
    const schemeId = document.getElementById('tally-scheme-select').value;
    const fromDate = document.getElementById('tally-export-from').value;
    const toDate = document.getElementById('tally-export-to').value;
    const statusEl = document.getElementById('tally-export-status');

    if (!schemeId) { statusEl.innerHTML = `<div class="tally-status tally-error">Please select a scheme.</div>`; return; }

    statusEl.innerHTML = `<div class="tally-status tally-pending">Generating Tally XML…</div>`;

    try {
      const params = [];
      if (fromDate) params.push(`from=${fromDate}`);
      if (toDate) params.push(`to=${toDate}`);
      const url = `/accounting/schemes/${schemeId}/tally/export/${params.length ? '?' + params.join('&') : ''}`;
      const blob = await Auth.apiGetBlob(url);
      const scheme = schemes.find(s => String(s.id) === String(schemeId));
      const filename = `tally_export_${scheme?.name?.replace(/\s+/g, '_') || 'scheme'}_${fromDate || 'all'}.xml`;
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      a.click();
      URL.revokeObjectURL(a.href);

      statusEl.innerHTML = `<div class="tally-status tally-success">✓ Export complete — downloaded as ${filename}</div>`;
    } catch (e) {
      statusEl.innerHTML = `<div class="tally-status tally-error">Export failed: ${esc(e.message)}</div>`;
    }
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
    const textFields = ['description', 'notes', 'account_code', 'account_name', 'invoice_number', 'reference_id', 'entry_description'];
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

  // ── Boot ──────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
