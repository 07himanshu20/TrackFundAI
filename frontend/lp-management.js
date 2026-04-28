/* ============================================================
   lp-management.js
   TrackFundAI Module 2 — LP Management (GP Admin View)
   Investors · Commitments · Capital Calls · Distributions · LP Capital Accounts
============================================================ */

(() => {
  let schemes = [];
  let investors = [];
  let commitments = [];
  let capitalCalls = [];
  let distributions = [];
  let capitalAccounts = [];
  let modalCallback = null;
  let activeTab = 'investors';

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

  const fmtPct = (v) => v ? parseFloat(v).toFixed(2) + '%' : '—';

  const badge = (text, cls) => `<span class="lp-badge ${cls}">${esc(text)}</span>`;

  // ── Init ──────────────────────────────────────────────────
  async function init() {
    if (!Auth.requireAuth()) return;

    const user = Auth.getUser();
    document.getElementById('user-badge').textContent =
      `${user.first_name || user.username} · ${user.role.replace('_', ' ').toUpperCase()}`;
    document.getElementById('org-label').textContent =
      user.organization_name || 'Unknown Organization';

    document.getElementById('btn-logout').onclick = () => Auth.logout();

    // Tab navigation
    document.querySelectorAll('[data-tab]').forEach(tab => {
      tab.onclick = () => switchTab(tab.dataset.tab);
    });

    // Buttons
    document.getElementById('btn-new-investor').onclick = () => openInvestorForm();
    document.getElementById('btn-new-commitment').onclick = () => openCommitmentForm();
    document.getElementById('btn-new-call').onclick = () => openCapitalCallForm();
    document.getElementById('btn-new-distribution').onclick = () => openDistributionForm();
    document.getElementById('btn-new-capital-account').onclick = () => openCapitalAccountForm();
    document.getElementById('btn-allot-units').onclick = () => openAllotUnitsForm();

    // Filters
    document.getElementById('investor-type-filter').onchange = renderInvestors;
    document.getElementById('kyc-filter').onchange = renderInvestors;

    // Modal
    document.getElementById('modal-close').onclick = closeModal;
    document.getElementById('modal-cancel').onclick = closeModal;
    document.getElementById('modal-form').onsubmit = handleModalSubmit;

    await loadSchemes();
    await Promise.all([loadInvestors(), loadCommitments(), loadCapitalCalls(), loadDistributions(), loadCapitalAccounts()]);

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
    ['investors', 'commitments', 'capital-calls', 'distributions', 'capital-accounts', 'waterfall'].forEach(t => {
      const el = document.getElementById(`tab-${t}`);
      if (el) el.classList.toggle('hidden', t !== tab);
    });
  }

  // ── Load schemes for dropdowns ────────────────────────────
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

      // Populate all scheme selects
      ['commitment-scheme-filter', 'call-scheme-filter', 'dist-scheme-filter', 'acc-scheme-filter'].forEach(id => {
        const sel = document.getElementById(id);
        if (!sel) return;
        schemes.forEach(s => {
          const opt = document.createElement('option');
          opt.value = s.id;
          opt.textContent = `${s.fund_name} → ${s.name}`;
          sel.appendChild(opt);
        });
        sel.onchange = () => {
          if (id === 'commitment-scheme-filter') loadCommitments();
          else if (id === 'call-scheme-filter') loadCapitalCalls();
          else if (id === 'dist-scheme-filter') loadDistributions();
          else if (id === 'acc-scheme-filter') loadCapitalAccounts();
        };
      });
    } catch (e) { console.error('Failed to load schemes:', e); }
  }

  // ── Stats bar ─────────────────────────────────────────────
  function renderStats() {
    const bar = document.getElementById('stats-bar');
    bar.innerHTML = '';
    const kycApproved = investors.filter(i => i.kyc_status === 'approved').length;
    const totalCommitted = commitments.reduce((a, c) => a + parseFloat(c.commitment_amount || 0), 0);
    const chips = [
      ['Total Investors', investors.length],
      ['KYC Approved', kycApproved],
      ['Commitments', commitments.length],
      ['Total Committed', '₹' + fmtCurrency(totalCommitted)],
      ['Capital Calls', capitalCalls.length],
      ['Distributions', distributions.length],
    ];
    chips.forEach(([label, value]) => {
      const div = document.createElement('div');
      div.className = 'hero-stat';
      div.innerHTML = `<span class="hero-stat-label">${label}</span><span class="hero-stat-value mono">${value}</span>`;
      bar.appendChild(div);
    });
  }

  // ═══════════════════════════════════════════════════════════
  // INVESTORS
  // ═══════════════════════════════════════════════════════════
  async function loadInvestors() {
    try {
      investors = await Auth.apiGet('/lp/investors/');
      renderInvestors();
    } catch (e) { console.error('Failed to load investors:', e); }
  }

  function renderInvestors() {
    const typeFilter = document.getElementById('investor-type-filter').value;
    const kycFilter  = document.getElementById('kyc-filter').value;
    let list = investors;
    if (typeFilter) list = list.filter(i => i.investor_type === typeFilter);
    if (kycFilter)  list = list.filter(i => i.kyc_status === kycFilter);

    const tbody = document.getElementById('investor-tbody');
    tbody.innerHTML = '';

    if (!list.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="lp-empty">No investors found.</td></tr>`;
      return;
    }

    list.forEach(inv => {
      const kycCls = `kyc-${inv.kyc_status || 'pending'}`;
      const flags = [
        inv.is_politically_exposed ? `<span class="flag-badge flag-pep">PEP</span>` : '',
        inv.is_land_border_country  ? `<span class="flag-badge flag-lbc">LBC</span>` : '',
        inv.is_accredited_investor  ? `<span class="flag-badge flag-accredited">ACCREDITED</span>` : '',
      ].filter(Boolean).join('') || '—';

      const kycBtnLabel = inv.kyc_status === 'completed' ? 'KYC ✓' :
        inv.kyc_status === 'in_progress' ? 'KYC Pending...' : 'Verify KYC';
      const kycBtnDisabled = inv.kyc_status === 'completed' ? 'disabled' : '';

      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>
          <div class="primary">${esc(inv.investor_name)}</div>
          <div class="secondary">${esc(inv.email || inv.phone || '')}</div>
        </td>
        <td>${esc(inv.investor_type_display)}</td>
        <td style="font-family:var(--font-mono);font-size:12px;">${esc(inv.pan) || '—'}</td>
        <td>${badge(inv.kyc_status_display, kycCls)}</td>
        <td style="font-size:11px;font-family:var(--font-mono);">${esc(inv.fatca_status || '—')}</td>
        <td>${flags}</td>
        <td>
          <button class="btn-action" data-id="${inv.id}" data-action="edit-investor">Edit</button>
          <button class="btn-action btn-kyc" data-id="${inv.id}" data-action="verify-kyc" ${kycBtnDisabled}>${kycBtnLabel}</button>
          <button class="btn-action" data-id="${inv.id}" data-action="verify-bank">Verify Bank</button>
        </td>
      `;
      tbody.appendChild(tr);
    });

    tbody.querySelectorAll('[data-action="edit-investor"]').forEach(btn => {
      btn.onclick = () => {
        const inv = investors.find(i => i.id === btn.dataset.id);
        if (inv) openInvestorForm(inv);
      };
    });

    tbody.querySelectorAll('[data-action="verify-kyc"]').forEach(btn => {
      btn.onclick = () => {
        const inv = investors.find(i => i.id === btn.dataset.id);
        if (inv) openKYCVerifyModal(inv);
      };
    });

    tbody.querySelectorAll('[data-action="verify-bank"]').forEach(btn => {
      btn.onclick = () => {
        const inv = investors.find(i => i.id === btn.dataset.id);
        if (inv) verifyBank(inv);
      };
    });
  }

  function openInvestorForm(existing = null) {
    const isEdit = !!existing;
    openModal(isEdit ? 'Edit Investor' : 'Add Investor', [
      {name: 'investor_name', label: 'Investor Name', required: true, default: existing?.investor_name || ''},
      {name: 'investor_type', label: 'Investor Type', type: 'select', required: true,
        options: [
          {value: 'individual_resident', label: 'Individual (Resident)'},
          {value: 'individual_nri', label: 'Individual (NRI)'},
          {value: 'huf', label: 'HUF'},
          {value: 'family_office', label: 'Family Office'},
          {value: 'corporate', label: 'Corporate'},
          {value: 'bank', label: 'Bank'},
          {value: 'insurance', label: 'Insurance Company'},
          {value: 'mf', label: 'Mutual Fund'},
          {value: 'pension_fund', label: 'Pension Fund'},
          {value: 'endowment', label: 'Endowment'},
          {value: 'fpi', label: 'FPI'},
          {value: 'trust', label: 'Trust'},
          {value: 'nbfc', label: 'NBFC'},
          {value: 'vc_fund', label: 'VC Fund'},
          {value: 'pe_fund', label: 'PE Fund'},
          {value: 'other', label: 'Other'},
        ].map(o => ({...o, selected: o.value === existing?.investor_type})),
        default: existing?.investor_type || 'individual_resident'},
      {name: 'contact_person', label: 'Contact Person', default: existing?.contact_person || ''},
      {name: 'email', label: 'Email', type: 'email', default: existing?.email || ''},
      {name: 'phone', label: 'Phone', default: existing?.phone || ''},
      {name: 'pan', label: 'PAN', required: true, default: existing?.pan || '', placeholder: 'ABCDE1234F'},
      {name: 'aadhaar_last_4', label: 'Aadhaar Last 4 Digits', default: existing?.aadhaar_last_4 || '', placeholder: 'XXXX'},
      {name: 'ckyc_number', label: 'CKYC Number', default: existing?.ckyc_number || ''},
      {name: 'kyc_status', label: 'KYC Status', type: 'select', default: existing?.kyc_status || 'pending', options: [
        {value: 'pending', label: 'Pending'},
        {value: 'in_review', label: 'In Review'},
        {value: 'approved', label: 'Approved'},
        {value: 'rejected', label: 'Rejected'},
        {value: 'expired', label: 'Expired'},
      ]},
      {name: 'kyc_completed_date', label: 'KYC Completed Date', type: 'date', default: existing?.kyc_completed_date || ''},
      {name: 'kyc_expiry_date', label: 'KYC Expiry Date', type: 'date', default: existing?.kyc_expiry_date || ''},
      {name: 'fatca_status', label: 'FATCA Status', type: 'select', default: existing?.fatca_status || 'not_applicable', options: [
        {value: 'not_applicable', label: 'Not Applicable'},
        {value: 'us_person', label: 'US Person'},
        {value: 'non_us_person', label: 'Non-US Person'},
        {value: 'pending', label: 'Pending'},
      ]},
      {name: 'address', label: 'Address', type: 'textarea', default: existing?.address || ''},
      {name: 'city', label: 'City', default: existing?.city || ''},
      {name: 'state', label: 'State', default: existing?.state || ''},
      {name: 'country', label: 'Country', default: existing?.country || 'India'},
    ], async (data) => {
      if (isEdit) {
        await Auth.apiPut(`/lp/investors/${existing.id}/`, data);
      } else {
        await Auth.apiPost('/lp/investors/', data);
      }
      await loadInvestors();
      renderStats();
    });
  }

  // ── KYC Verification ───────────────────────────────────────
  function openKYCVerifyModal(inv) {
    openModal(`Verify KYC — ${inv.investor_name}`, [
      {name: 'action', label: 'KYC Action', type: 'select', required: true, default: 'approve', options: [
        {value: 'approve', label: 'Approve KYC'},
        {value: 'reject', label: 'Reject KYC'},
        {value: 'request_review', label: 'Request Review'},
      ]},
      {name: 'kyc_expiry_date', label: 'KYC Expiry Date (for approval)', type: 'date', default: ''},
    ], async (data) => {
      await Auth.apiPost(`/lp/investors/${inv.id}/verify-kyc/`, data);
      await loadInvestors();
      renderStats();
    });
  }

  // ── Bank Verification (Penny Drop) ────────────────────────
  async function verifyBank(inv) {
    if (!confirm(`Verify bank account for ${inv.investor_name}?\nThis checks account details are complete and links the account.`)) return;
    try {
      const result = await Auth.apiPost(`/lp/investors/${inv.id}/verify-bank/`, {});
      alert(`Bank verification: ${result.detail}`);
      await loadInvestors();
    } catch (e) {
      alert('Bank verification failed: ' + e.message);
    }
  }

  // ═══════════════════════════════════════════════════════════
  // COMMITMENTS
  // ═══════════════════════════════════════════════════════════
  async function loadCommitments() {
    try {
      const schemeId = document.getElementById('commitment-scheme-filter').value;
      const url = schemeId ? `/lp/commitments/?scheme=${schemeId}` : '/lp/commitments/';
      commitments = await Auth.apiGet(url);
      renderCommitments();
    } catch (e) { console.error('Failed to load commitments:', e); }
  }

  function renderCommitments() {
    const tbody = document.getElementById('commitment-tbody');
    tbody.innerHTML = '';

    if (!commitments.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="lp-empty">No commitments found.</td></tr>`;
      return;
    }

    commitments.forEach(c => {
      const statusCls = `status-${c.commitment_status}`;
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>
          <div class="primary">${esc(c.investor_name)}</div>
        </td>
        <td>${esc(c.scheme_name)}</td>
        <td style="font-family:var(--font-mono);font-weight:600;">₹${fmtCurrency(c.commitment_amount)}</td>
        <td style="font-size:12px;">${esc(c.close_type_display)}</td>
        <td style="font-family:var(--font-mono);font-size:12px;">${c.units_allocated ? parseFloat(c.units_allocated).toLocaleString() : '—'}</td>
        <td>${badge(c.status_display, statusCls)}</td>
        <td>${c.side_letter_exists ? '<span class="flag-badge flag-pep">YES</span>' : '<span style="color:var(--text-muted);font-size:12px;">No</span>'}</td>
        <td>
          <button class="btn-action" data-id="${c.id}" data-action="edit-commitment">Edit</button>
        </td>
      `;
      tbody.appendChild(tr);
    });

    tbody.querySelectorAll('[data-action="edit-commitment"]').forEach(btn => {
      btn.onclick = () => {
        const c = commitments.find(x => x.id === btn.dataset.id);
        if (c) openCommitmentForm(c);
      };
    });
  }

  function openCommitmentForm(existing = null) {
    const isEdit = !!existing;
    const investorOpts = [{value: '', label: '— Select Investor —'}].concat(
      investors.map(i => ({value: i.id, label: i.investor_name}))
    );
    const schemeOpts = [{value: '', label: '— Select Scheme —'}].concat(
      schemes.map(s => ({value: s.id, label: `${s.fund_name} → ${s.name}`}))
    );

    openModal(isEdit ? 'Edit Commitment' : 'New Commitment', [
      {name: 'investor', label: 'Investor', type: 'select', required: true, options: investorOpts, default: existing?.investor || ''},
      {name: 'scheme', label: 'Scheme', type: 'select', required: true, options: schemeOpts, default: existing?.scheme || ''},
      {name: 'commitment_amount', label: 'Commitment Amount (₹)', type: 'number', required: true, step: '0.01', default: existing?.commitment_amount || ''},
      {name: 'commitment_date', label: 'Commitment Date', type: 'date', default: existing?.commitment_date || ''},
      {name: 'close_type', label: 'Close Type', type: 'select', default: existing?.close_type || 'first_close', options: [
        {value: 'first_close', label: 'First Close'},
        {value: 'second_close', label: 'Second Close'},
        {value: 'final_close', label: 'Final Close'},
      ]},
      {name: 'units_allocated', label: 'Units Allocated', type: 'number', step: '0.0001', default: existing?.units_allocated || ''},
      {name: 'commitment_status', label: 'Status', type: 'select', default: existing?.commitment_status || 'active', options: [
        {value: 'active', label: 'Active'},
        {value: 'cancelled', label: 'Cancelled'},
        {value: 'transferred', label: 'Transferred'},
      ]},
    ], async (data) => {
      if (isEdit) {
        await Auth.apiPut(`/lp/commitments/${existing.id}/`, data);
      } else {
        await Auth.apiPost('/lp/commitments/', data);
      }
      await loadCommitments();
      renderStats();
    });
  }

  // ═══════════════════════════════════════════════════════════
  // CAPITAL CALLS
  // ═══════════════════════════════════════════════════════════
  async function loadCapitalCalls() {
    try {
      const schemeId = document.getElementById('call-scheme-filter').value;
      const url = schemeId ? `/lp/capital-calls/?scheme=${schemeId}` : '/lp/capital-calls/';
      capitalCalls = await Auth.apiGet(url);
      renderCapitalCalls();
    } catch (e) { console.error('Failed to load capital calls:', e); }
  }

  function renderCapitalCalls() {
    const container = document.getElementById('capital-calls-list');
    container.innerHTML = '';

    if (!capitalCalls.length) {
      container.innerHTML = `<div class="lp-empty">No capital calls found. Issue one using the button above.</div>`;
      return;
    }

    capitalCalls.forEach(call => {
      const statusCls = `status-${call.call_status}`;
      const card = document.createElement('div');
      card.className = 'call-card';
      card.innerHTML = `
        <div class="call-card-header">
          <div>
            <div class="call-card-title">Call #${esc(call.call_number)} — ${esc(call.scheme_name)}</div>
            <div class="call-card-meta">Call Date: ${fmtDate(call.call_date)} · Due: ${fmtDate(call.payment_due_date)}</div>
          </div>
          ${badge(call.status_display, statusCls)}
        </div>
        <div class="call-card-metrics">
          <div class="call-metric">
            <span class="label">Total Call Amount</span>
            <span class="value">₹${fmtCurrency(call.total_call_amount)}</span>
          </div>
          <div class="call-metric">
            <span class="label">Call %</span>
            <span class="value">${fmtPct(call.call_percentage)}</span>
          </div>
          <div class="call-metric">
            <span class="label">Status</span>
            <span class="value">${esc(call.status_display)}</span>
          </div>
        </div>
        <div class="call-card-actions">
          <button class="btn-action btn-toggle-items" data-id="${call.id}">View Line Items</button>
          <button class="btn-action" data-id="${call.id}" data-action="edit-call">Edit</button>
          <button class="btn-action btn-send-notices" data-send-btn="${call.id}" onclick="window._sendCallNotices('${call.id}', ${call.call_number})">Send Notices</button>
        </div>
        <div class="line-items-wrap hidden" id="line-items-${call.id}"></div>
      `;
      container.appendChild(card);

      card.querySelector('.btn-toggle-items').onclick = () => toggleLineItems(call.id);
      card.querySelector('[data-action="edit-call"]').onclick = () => {
        openCapitalCallForm(call);
      };
    });
  }

  async function toggleLineItems(callId) {
    const wrap = document.getElementById(`line-items-${callId}`);
    if (!wrap.classList.contains('hidden')) {
      wrap.classList.add('hidden');
      return;
    }
    try {
      const items = await Auth.apiGet(`/lp/capital-calls/${callId}/line-items/`);
      wrap.innerHTML = '';
      if (!items.length) {
        wrap.innerHTML = '<p style="padding:12px;color:var(--text-muted);font-size:12px;">No line items yet.</p>';
      } else {
        const table = document.createElement('table');
        table.className = 'line-items-table';
        table.innerHTML = `
          <thead><tr>
            <th>Investor</th>
            <th>Called Amount</th>
            <th>Cumulative %</th>
            <th>Units Allotted</th>
            <th>Payment Status</th>
            <th>Amount Received</th>
            <th>UTR</th>
            <th>Actions</th>
          </tr></thead>
          <tbody>${items.map(it => `
            <tr>
              <td>${esc(it.investor_name)}</td>
              <td>₹${fmtCurrency(it.called_amount)}</td>
              <td>${fmtPct(it.cumulative_called_pct)}</td>
              <td>${it.units_allotted ? parseFloat(it.units_allotted).toFixed(4) : '—'}</td>
              <td>${badge(it.payment_status_display, `status-${it.payment_status}`)}</td>
              <td>${it.amount_received ? '₹' + fmtCurrency(it.amount_received) : '—'}</td>
              <td>${esc(it.utr_number) || '—'}</td>
              <td>${it.payment_status !== 'paid' ?
                `<button class="btn-action btn-match-utr" data-call="${callId}" data-commitment="${it.commitment}" data-amount="${it.called_amount}">Match UTR</button>` : '<span style="color:var(--accent-green);font-size:11px;">Paid</span>'}</td>
            </tr>`).join('')}
          </tbody>
        `;
        wrap.appendChild(table);

        // Bind UTR match buttons
        wrap.querySelectorAll('.btn-match-utr').forEach(btn => {
          btn.onclick = () => openUTRMatchModal(btn.dataset.call, btn.dataset.commitment, btn.dataset.amount);
        });
      }
      wrap.classList.remove('hidden');
    } catch (e) { console.error('Failed to load line items:', e); }
  }

  function openCapitalCallForm(existing = null) {
    const isEdit = !!existing;
    const schemeOpts = [{value: '', label: '— Select Scheme —'}].concat(
      schemes.map(s => ({value: s.id, label: `${s.fund_name} → ${s.name}`}))
    );

    openModal(isEdit ? 'Edit Capital Call' : 'Issue Capital Call', [
      {name: 'scheme', label: 'Scheme', type: 'select', required: true, options: schemeOpts, default: existing?.scheme || ''},
      {name: 'call_number', label: 'Call Number', type: 'number', required: true, default: existing?.call_number || ''},
      {name: 'call_date', label: 'Call Date', type: 'date', required: true, default: existing?.call_date || ''},
      {name: 'payment_due_date', label: 'Payment Due Date', type: 'date', required: true, default: existing?.payment_due_date || ''},
      {name: 'call_percentage', label: 'Call % (of commitment)', type: 'number', required: true, step: '0.01', default: existing?.call_percentage || ''},
      {name: 'total_call_amount', label: 'Total Call Amount (₹)', type: 'number', step: '0.01', default: existing?.total_call_amount || ''},
      {name: 'purpose', label: 'Purpose / Notes', type: 'textarea', default: existing?.purpose || ''},
      {name: 'call_status', label: 'Status', type: 'select', default: existing?.call_status || 'draft', options: [
        {value: 'draft', label: 'Draft'},
        {value: 'sent', label: 'Sent'},
        {value: 'partially_paid', label: 'Partially Paid'},
        {value: 'fully_paid', label: 'Fully Paid'},
        {value: 'cancelled', label: 'Cancelled'},
      ]},
    ], async (data) => {
      if (isEdit) {
        await Auth.apiPut(`/lp/capital-calls/${existing.id}/`, data);
      } else {
        await Auth.apiPost('/lp/capital-calls/', data);
      }
      await loadCapitalCalls();
    });
  }

  // ── UTR Match Modal ────────────────────────────────────────
  function openUTRMatchModal(callId, commitmentId, calledAmount) {
    openModal('Match UTR Payment', [
      {name: 'utr_number', label: 'UTR / Transaction Reference', required: true, default: '', placeholder: 'e.g. AXIS12345678'},
      {name: 'amount_received', label: `Amount Received (₹) — called: ₹${fmtCurrency(calledAmount)}`, type: 'number', step: '0.01', required: true, default: calledAmount},
      {name: 'payment_date', label: 'Payment Date', type: 'date', required: true, default: new Date().toISOString().split('T')[0]},
    ], async (data) => {
      data.commitment_id = commitmentId;
      const result = await Auth.apiPost(`/lp/capital-calls/${callId}/match-utr/`, data);
      alert(result.detail);
      await loadCapitalCalls();
    });
  }

  // ═══════════════════════════════════════════════════════════
  // DISTRIBUTIONS
  // ═══════════════════════════════════════════════════════════
  async function loadDistributions() {
    try {
      const schemeId = document.getElementById('dist-scheme-filter').value;
      const url = schemeId ? `/lp/distributions/?scheme=${schemeId}` : '/lp/distributions/';
      distributions = await Auth.apiGet(url);
      renderDistributions();
    } catch (e) { console.error('Failed to load distributions:', e); }
  }

  function renderDistributions() {
    const container = document.getElementById('distributions-list');
    container.innerHTML = '';

    if (!distributions.length) {
      container.innerHTML = `<div class="lp-empty">No distributions found.</div>`;
      return;
    }

    distributions.forEach(dist => {
      const statusCls = `status-${dist.distribution_status}`;
      const card = document.createElement('div');
      card.className = 'call-card';
      card.innerHTML = `
        <div class="call-card-header">
          <div>
            <div class="call-card-title">Distribution #${esc(dist.distribution_number)} — ${esc(dist.scheme_name)}</div>
            <div class="call-card-meta">${esc(dist.type_display)} · ${fmtDate(dist.distribution_date)}</div>
          </div>
          ${badge(dist.status_display, statusCls)}
        </div>
        <div class="call-card-metrics">
          <div class="call-metric">
            <span class="label">Gross Amount</span>
            <span class="value">₹${fmtCurrency(dist.total_gross_amount)}</span>
          </div>
          <div class="call-metric">
            <span class="label">TDS Deducted</span>
            <span class="value">₹${fmtCurrency(dist.total_tds_amount)}</span>
          </div>
          <div class="call-metric">
            <span class="label">Net to LPs</span>
            <span class="value" style="color:var(--accent-green);">₹${fmtCurrency(dist.total_net_amount)}</span>
          </div>
        </div>
        <div class="call-card-actions">
          <button class="btn-action btn-toggle-items" data-id="${dist.id}">View Line Items</button>
          <button class="btn-action" data-id="${dist.id}" data-action="edit-dist">Edit</button>
          ${dist.distribution_status !== 'distributed' ?
            `<button class="btn-action btn-process-dist" data-id="${dist.id}" data-num="${dist.distribution_number}">Process & Notify LPs</button>` :
            '<span style="color:var(--accent-green);font-size:11px;padding:6px;">Distributed</span>'}
        </div>
        <div class="line-items-wrap hidden" id="dist-items-${dist.id}"></div>
      `;
      container.appendChild(card);

      card.querySelector('.btn-toggle-items').onclick = () => toggleDistLineItems(dist.id);
      card.querySelector('[data-action="edit-dist"]').onclick = () => openDistributionForm(dist);

      const processBtn = card.querySelector('.btn-process-dist');
      if (processBtn) {
        processBtn.onclick = async () => {
          if (!confirm(`Process Distribution #${dist.distribution_number} and notify all LPs?`)) return;
          processBtn.disabled = true;
          processBtn.textContent = 'Processing...';
          try {
            const result = await Auth.apiPost(`/lp/distributions/${dist.id}/process/`, {});
            alert(result.detail);
            await loadDistributions();
          } catch (e) {
            alert('Failed to process: ' + e.message);
          } finally {
            processBtn.disabled = false;
            processBtn.textContent = 'Process & Notify LPs';
          }
        };
      }
    });
  }

  async function toggleDistLineItems(distId) {
    const wrap = document.getElementById(`dist-items-${distId}`);
    if (!wrap.classList.contains('hidden')) {
      wrap.classList.add('hidden');
      return;
    }
    try {
      const items = await Auth.apiGet(`/lp/distributions/${distId}/line-items/`);
      wrap.innerHTML = '';
      if (!items.length) {
        wrap.innerHTML = '<p style="padding:12px;color:var(--text-muted);font-size:12px;">No line items yet.</p>';
      } else {
        const table = document.createElement('table');
        table.className = 'line-items-table';
        table.innerHTML = `
          <thead><tr>
            <th>Investor</th>
            <th>Gross</th>
            <th>TDS Rate</th>
            <th>TDS Amount</th>
            <th>Net Amount</th>
            <th>Units Redeemed</th>
            <th>UTR</th>
          </tr></thead>
          <tbody>${items.map(it => `
            <tr>
              <td>${esc(it.investor_name)}</td>
              <td>₹${fmtCurrency(it.gross_amount)}</td>
              <td>${fmtPct(it.tds_rate)}</td>
              <td>₹${fmtCurrency(it.tds_amount)}</td>
              <td style="color:var(--accent-green);">₹${fmtCurrency(it.net_amount)}</td>
              <td>${it.units_redeemed ? parseFloat(it.units_redeemed).toFixed(4) : '—'}</td>
              <td>${esc(it.utr_number) || '—'}</td>
            </tr>`).join('')}
          </tbody>
        `;
        wrap.appendChild(table);
      }
      wrap.classList.remove('hidden');
    } catch (e) { console.error('Failed to load distribution line items:', e); }
  }

  function openDistributionForm(existing = null) {
    const isEdit = !!existing;
    const schemeOpts = [{value: '', label: '— Select Scheme —'}].concat(
      schemes.map(s => ({value: s.id, label: `${s.fund_name} → ${s.name}`}))
    );

    openModal(isEdit ? 'Edit Distribution' : 'New Distribution', [
      {name: 'scheme', label: 'Scheme', type: 'select', required: true, options: schemeOpts, default: existing?.scheme || ''},
      {name: 'distribution_number', label: 'Distribution Number', type: 'number', required: true, default: existing?.distribution_number || ''},
      {name: 'distribution_date', label: 'Distribution Date', type: 'date', required: true, default: existing?.distribution_date || ''},
      {name: 'distribution_type', label: 'Distribution Type', type: 'select', required: true, default: existing?.distribution_type || 'return_of_capital', options: [
        {value: 'return_of_capital', label: 'Return of Capital'},
        {value: 'income', label: 'Income'},
        {value: 'capital_gain', label: 'Capital Gain'},
        {value: 'dividend', label: 'Dividend'},
        {value: 'carry', label: 'Carried Interest'},
        {value: 'interest', label: 'Interest'},
        {value: 'other', label: 'Other'},
      ]},
      {name: 'total_gross_amount', label: 'Total Gross Amount (₹)', type: 'number', required: true, step: '0.01', default: existing?.total_gross_amount || ''},
      {name: 'total_tds_amount', label: 'Total TDS Amount (₹)', type: 'number', step: '0.01', default: existing?.total_tds_amount || ''},
      {name: 'total_net_amount', label: 'Total Net Amount (₹)', type: 'number', step: '0.01', default: existing?.total_net_amount || ''},
      {name: 'distribution_status', label: 'Status', type: 'select', default: existing?.distribution_status || 'pending', options: [
        {value: 'pending', label: 'Pending'},
        {value: 'processing', label: 'Processing'},
        {value: 'completed', label: 'Completed'},
        {value: 'cancelled', label: 'Cancelled'},
      ]},
      {name: 'notes', label: 'Notes', type: 'textarea', default: existing?.notes || ''},
    ], async (data) => {
      if (isEdit) {
        await Auth.apiPut(`/lp/distributions/${existing.id}/`, data);
      } else {
        await Auth.apiPost('/lp/distributions/', data);
      }
      await loadDistributions();
    });
  }

  // ═══════════════════════════════════════════════════════════
  // LP CAPITAL ACCOUNTS
  // ═══════════════════════════════════════════════════════════
  async function loadCapitalAccounts() {
    try {
      const schemeId = document.getElementById('acc-scheme-filter').value;
      const url = schemeId ? `/lp/capital-accounts/?scheme=${schemeId}` : '/lp/capital-accounts/';
      capitalAccounts = await Auth.apiGet(url);
      renderCapitalAccounts();
    } catch (e) { console.error('Failed to load capital accounts:', e); }
  }

  function renderCapitalAccounts() {
    const tbody = document.getElementById('capital-accounts-tbody');
    tbody.innerHTML = '';

    if (!capitalAccounts.length) {
      tbody.innerHTML = `<tr><td colspan="14" class="lp-empty">No capital account records found.</td></tr>`;
      return;
    }

    capitalAccounts.forEach(acc => {
      const irr = acc.irr ? parseFloat(acc.irr) : null;
      const irrCls = irr === null ? 'metric-neutral' : irr >= 0 ? 'metric-positive' : 'metric-negative';
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>
          <div class="primary">${esc(acc.investor_name)}</div>
        </td>
        <td style="font-size:12px;">${esc(acc.scheme_name)}</td>
        <td style="font-family:var(--font-mono);font-size:12px;">${fmtDate(acc.as_of_date)}</td>
        <td class="acc-metrics">₹${fmtCurrency(acc.committed_capital)}</td>
        <td class="acc-metrics">₹${fmtCurrency(acc.called_capital)}</td>
        <td class="acc-metrics">₹${fmtCurrency(acc.uncalled_capital)}</td>
        <td class="acc-metrics" style="color:var(--accent-green);">₹${fmtCurrency(acc.distributed_capital)}</td>
        <td class="acc-metrics">₹${fmtCurrency(acc.unrealized_value)}</td>
        <td class="acc-metrics" style="font-weight:600;">₹${fmtCurrency(acc.total_value)}</td>
        <td><span class="irr-val ${irrCls}">${irr !== null ? irr.toFixed(1) + '%' : '—'}</span></td>
        <td class="acc-metrics">${acc.tvpi ? parseFloat(acc.tvpi).toFixed(2) + 'x' : '—'}</td>
        <td class="acc-metrics">${acc.dpi ? parseFloat(acc.dpi).toFixed(2) + 'x' : '—'}</td>
        <td class="acc-metrics">${acc.moic ? parseFloat(acc.moic).toFixed(2) + 'x' : '—'}</td>
        <td>
          <button class="btn-action" data-id="${acc.id}" data-action="edit-acc">Edit</button>
        </td>
      `;
      tbody.appendChild(tr);
    });

    tbody.querySelectorAll('[data-action="edit-acc"]').forEach(btn => {
      btn.onclick = () => {
        const acc = capitalAccounts.find(a => a.id === btn.dataset.id);
        if (acc) openCapitalAccountForm(acc);
      };
    });
  }

  function openCapitalAccountForm(existing = null) {
    const isEdit = !!existing;
    const commitmentOpts = [{value: '', label: '— Select Commitment —'}].concat(
      commitments.map(c => ({value: c.id, label: `${c.investor_name} — ${c.scheme_name}`}))
    );

    openModal(isEdit ? 'Edit Capital Account' : 'Record Capital Account', [
      {name: 'commitment', label: 'Commitment (Investor + Scheme)', type: 'select', required: true, options: commitmentOpts, default: existing?.commitment || ''},
      {name: 'as_of_date', label: 'As Of Date', type: 'date', required: true, default: existing?.as_of_date || ''},
      {name: 'committed_capital', label: 'Committed Capital (₹)', type: 'number', step: '0.01', default: existing?.committed_capital || ''},
      {name: 'called_capital', label: 'Called Capital (₹)', type: 'number', step: '0.01', default: existing?.called_capital || ''},
      {name: 'uncalled_capital', label: 'Uncalled Capital (₹)', type: 'number', step: '0.01', default: existing?.uncalled_capital || ''},
      {name: 'distributed_capital', label: 'Distributed Capital (₹)', type: 'number', step: '0.01', default: existing?.distributed_capital || ''},
      {name: 'unrealized_value', label: 'Unrealized Value (₹)', type: 'number', step: '0.01', default: existing?.unrealized_value || ''},
      {name: 'total_value', label: 'Total Value (₹)', type: 'number', step: '0.01', default: existing?.total_value || ''},
      {name: 'irr', label: 'IRR (%)', type: 'number', step: '0.0001', default: existing?.irr || ''},
      {name: 'tvpi', label: 'TVPI (x)', type: 'number', step: '0.0001', default: existing?.tvpi || ''},
      {name: 'dpi', label: 'DPI (x)', type: 'number', step: '0.0001', default: existing?.dpi || ''},
      {name: 'rvpi', label: 'RVPI (x)', type: 'number', step: '0.0001', default: existing?.rvpi || ''},
      {name: 'moic', label: 'MOIC (x)', type: 'number', step: '0.0001', default: existing?.moic || ''},
      {name: 'units_held', label: 'Units Held', type: 'number', step: '0.0001', default: existing?.units_held || ''},
      {name: 'management_fee_charged', label: 'Management Fee Charged (₹)', type: 'number', step: '0.01', default: existing?.management_fee_charged || ''},
      {name: 'carried_interest_charged', label: 'Carried Interest Charged (₹)', type: 'number', step: '0.01', default: existing?.carried_interest_charged || ''},
    ], async (data) => {
      if (isEdit) {
        await Auth.apiPut(`/lp/capital-accounts/${existing.id}/`, data);
      } else {
        await Auth.apiPost('/lp/capital-accounts/', data);
      }
      await loadCapitalAccounts();
    });
  }

  // ── Unit Allotment ─────────────────────────────────────────
  function openAllotUnitsForm() {
    const schemeOpts = [{value: '', label: '— Select Scheme —'}].concat(
      schemes.map(s => ({value: s.id, label: `${s.fund_name} → ${s.name}`}))
    );

    openModal('Allot Units at NAV', [
      {name: 'scheme', label: 'Scheme', type: 'select', required: true, options: schemeOpts, default: ''},
      {name: 'nav_per_unit', label: 'NAV per Unit (₹)', type: 'number', step: '0.01', required: true, default: '100'},
    ], async (data) => {
      const schemeId = data.scheme;
      if (!schemeId) { alert('Please select a scheme.'); return; }
      const result = await Auth.apiPost(`/lp/schemes/${schemeId}/allot-units/`, {
        nav_per_unit: data.nav_per_unit,
      });
      alert(result.detail);
      await loadCommitments();
    });
  }

  // ═══════════════════════════════════════════════════════════
  // MODAL ENGINE (reused from fund-admin pattern)
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
    const textFields = ['investor_name', 'contact_person', 'email', 'phone', 'pan',
      'aadhaar_last_4', 'ckyc_number', 'address', 'city', 'state', 'country',
      'purpose', 'notes', 'utr_number'];
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

  // ── Waterfall Simulator ────────────────────────────────────

  function initWaterfallSimulator() {
    const schemeSelect = document.getElementById('wf-scheme-select');
    // Populate scheme dropdown
    schemes.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.id;
      opt.textContent = `${s.fund_name} → ${s.name}`;
      // Pre-populate hurdle/carry from scheme if available
      opt.dataset.hurdle = s.hurdle_rate_pct || 8;
      opt.dataset.carry = s.carry_pct || 20;
      schemeSelect.appendChild(opt);
    });

    const sliders = [
      { id: 'wf-distributions', valId: 'wf-distributions-val', fmt: v => `₹${v} Cr` },
      { id: 'wf-called',        valId: 'wf-called-val',        fmt: v => `₹${v} Cr` },
      { id: 'wf-hurdle',        valId: 'wf-hurdle-val',        fmt: v => `${v}%` },
      { id: 'wf-carry',         valId: 'wf-carry-val',         fmt: v => `${v}%` },
      { id: 'wf-tenure',        valId: 'wf-tenure-val',        fmt: v => `${v} years` },
    ];

    sliders.forEach(({ id, valId, fmt }) => {
      const input = document.getElementById(id);
      const display = document.getElementById(valId);
      if (!input || !display) return;
      display.textContent = fmt(input.value);
      input.oninput = () => { display.textContent = fmt(input.value); runWaterfall(); };
    });

    // When scheme changes, pre-fill hurdle and carry from scheme data
    schemeSelect.onchange = () => {
      const opt = schemeSelect.selectedOptions[0];
      if (opt && opt.dataset.hurdle) {
        const hurdleEl = document.getElementById('wf-hurdle');
        const carryEl = document.getElementById('wf-carry');
        if (hurdleEl) { hurdleEl.value = opt.dataset.hurdle; document.getElementById('wf-hurdle-val').textContent = `${opt.dataset.hurdle}%`; }
        if (carryEl) { carryEl.value = opt.dataset.carry; document.getElementById('wf-carry-val').textContent = `${opt.dataset.carry}%`; }
      }
      runWaterfall();
    };

    runWaterfall();
  }

  function runWaterfall() {
    const totalDist  = parseFloat(document.getElementById('wf-distributions')?.value || 200) * 1e7; // Cr to ₹
    const calledCap  = parseFloat(document.getElementById('wf-called')?.value || 100) * 1e7;
    const hurdlePct  = parseFloat(document.getElementById('wf-hurdle')?.value || 8) / 100;
    const carryPct   = parseFloat(document.getElementById('wf-carry')?.value || 20) / 100;
    const tenure     = parseFloat(document.getElementById('wf-tenure')?.value || 7);

    // European waterfall calculation
    const prefReturn = calledCap * Math.pow(1 + hurdlePct, tenure) - calledCap; // compound preferred return
    const carryBase  = Math.max(0, totalDist - calledCap - prefReturn);
    const gpCarry    = carryBase * carryPct;
    const lpTotal    = totalDist - gpCarry;
    const moic       = calledCap > 0 ? totalDist / calledCap : 0;
    const lpMoic     = calledCap > 0 ? lpTotal / calledCap : 0;

    const fmtCr = (v) => {
      if (v >= 1e9) return `₹${(v/1e9).toFixed(2)}B`;
      if (v >= 1e7) return `₹${(v/1e7).toFixed(1)} Cr`;
      if (v >= 1e5) return `₹${(v/1e5).toFixed(1)} L`;
      return `₹${v.toLocaleString('en-IN')}`;
    };

    const pct = (v) => totalDist > 0 ? Math.round((v / totalDist) * 100) : 0;

    const steps = [
      { name: 'Total Distributions', label: 'Gross pool to be distributed', amount: totalDist, pctVal: 100, color: '#4a9eff', cls: '' },
      { name: 'Return of Capital (LP)', label: 'Called capital returned to LPs first', amount: calledCap, pctVal: pct(calledCap), color: '#4a9eff', cls: 'step-lp' },
      { name: 'Preferred Return (LP)', label: `Hurdle: ${(hurdlePct*100).toFixed(1)}% compounded over ${tenure} yrs`, amount: prefReturn, pctVal: pct(prefReturn), color: '#f6a623', cls: 'step-lp' },
      { name: 'Carry Base (GP)', label: 'Profit above hurdle subject to carry', amount: carryBase, pctVal: pct(carryBase), color: '#a78bfa', cls: 'step-highlight' },
      { name: `GP Carried Interest (${(carryPct*100).toFixed(0)}%)`, label: 'GP share of carry base', amount: gpCarry, pctVal: pct(gpCarry), color: '#3ecf8e', cls: 'step-gp' },
      { name: 'LP Net Proceeds', label: 'Total LP receives (capital + hurdle + LP carry share)', amount: lpTotal, pctVal: pct(lpTotal), color: '#4a9eff', cls: 'step-lp' },
    ];

    const stepsEl = document.getElementById('wf-steps');
    if (stepsEl) {
      stepsEl.innerHTML = steps.map(s => `
        <div class="wf-step ${s.cls}">
          <div style="flex:1;">
            <div class="wf-step-name">${s.name}</div>
            <div class="wf-step-label">${s.label}</div>
          </div>
          <div class="wf-bar-wrap"><div class="wf-bar" style="width:${s.pctVal}%;background:${s.color}"></div></div>
          <div class="wf-step-amount" style="color:${s.color}">${fmtCr(Math.max(0, s.amount))}</div>
        </div>`).join('');
    }

    const summaryEl = document.getElementById('wf-summary');
    if (summaryEl) {
      summaryEl.innerHTML = `
        <div class="wf-metric">
          <div class="wf-metric-label">Fund MoIC</div>
          <div class="wf-metric-value" style="color:var(--accent)">${moic.toFixed(2)}x</div>
        </div>
        <div class="wf-metric">
          <div class="wf-metric-label">GP Carry</div>
          <div class="wf-metric-value" style="color:#3ecf8e">${fmtCr(Math.max(0, gpCarry))}</div>
        </div>
        <div class="wf-metric">
          <div class="wf-metric-label">LP Net MoIC</div>
          <div class="wf-metric-value" style="color:#4a9eff">${lpMoic.toFixed(2)}x</div>
        </div>`;
    }
  }

  // Initialise simulator when waterfall tab is first opened
  let wfInitialised = false;
  const origSwitchTab = window._lpSwitchTab;
  // Patch switchTab to init simulator on first visit
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-tab="waterfall"]').forEach(btn => {
      btn.addEventListener('click', () => {
        if (!wfInitialised) {
          wfInitialised = true;
          initWaterfallSimulator();
        }
      });
    });
  });

  // ── Bulk Capital Call Notice Dispatch ──────────────────────

  // Adds a "Send Notices" button to each capital call card
  window._sendCallNotices = async (callId, callNumber) => {
    if (!confirm(`Send email + WhatsApp notices to all LPs for Capital Call #${callNumber}?`)) return;
    const btn = document.querySelector(`[data-send-btn="${callId}"]`);
    if (btn) { btn.disabled = true; btn.textContent = 'Sending...'; }
    try {
      await Auth.apiPost(`/lp/capital-calls/${callId}/send-notices/`, {});
      alert(`Notices dispatched for Capital Call #${callNumber}.`);
    } catch (e) {
      alert('Failed to send notices: ' + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = 'Send Notices'; }
    }
  };

  // ── Boot ──────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
