/* ============================================================
   investments.js
   TrackFundAI Phase 2 — Investments, Valuations, KPIs, Exits, Board Meetings
============================================================ */

(() => {
  let schemes = [];
  let investments = [];
  let currentInvestment = null;
  let modalCallback = null;
  let activeTab = 'investments';

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

  const fmtDate = (d) => d ? new Date(d).toLocaleDateString('en-IN') : '—';

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
    document.querySelectorAll('.inv-tab').forEach(tab => {
      tab.onclick = () => switchTab(tab.dataset.tab);
    });

    // Buttons
    document.getElementById('btn-new-investment').onclick = () => openInvestmentForm();
    document.getElementById('btn-back-investments').onclick = () => showInvestmentList();
    document.getElementById('btn-new-tranche').onclick = () => openTrancheForm();
    document.getElementById('btn-new-valuation').onclick = () => openValuationForm();
    document.getElementById('btn-new-exit').onclick = () => openExitForm();
    document.getElementById('btn-new-board').onclick = () => openBoardForm();
    document.getElementById('btn-generate-board-pack').onclick = () => generateBoardPack();

    // Modal
    document.getElementById('modal-close').onclick = closeModal;
    document.getElementById('modal-cancel').onclick = closeModal;
    document.getElementById('modal-form').onsubmit = handleModalSubmit;

    await loadSchemes();
    await loadInvestments();
    loadNotifCount();
  }

  // ── Notification badge ────────────────────────────────────
  async function loadNotifCount() {
    try {
      const data = await Auth.apiGet('/notifications/unread-count/');
      const badge = document.getElementById('notif-badge');
      if (badge) {
        badge.textContent = data.unread_count || 0;
        badge.classList.toggle('zero', !data.unread_count);
      }
    } catch (e) { console.error('Notif count failed:', e); }
  }

  // ── Tab switching ─────────────────────────────────────────
  function switchTab(tab) {
    activeTab = tab;
    document.querySelectorAll('.inv-tab').forEach(t => {
      t.classList.toggle('active', t.dataset.tab === tab);
    });

    // Hide all tab sections
    ['investments', 'valuations', 'kpis', 'exits', 'board'].forEach(t => {
      const el = document.getElementById(`tab-${t}`);
      if (el) el.classList.toggle('hidden', t !== tab);
    });

    // Also hide detail section when switching tabs
    document.getElementById('investment-detail-section').classList.add('hidden');

    // Load tab data
    if (tab === 'valuations') loadAllValuations();
    else if (tab === 'kpis') loadAllKPIs();
    else if (tab === 'exits') loadAllExits();
    else if (tab === 'board') loadAllBoardMeetings();
  }

  // ── Load schemes (for dropdown) ───────────────────────────
  async function loadSchemes() {
    try {
      const funds = await Auth.apiGet('/funds/');
      const select = document.getElementById('scheme-select');
      const boardSelect = document.getElementById('board-pack-scheme');
      schemes = [];
      for (const fund of funds) {
        const fundSchemes = await Auth.apiGet(`/funds/${fund.id}/schemes/`);
        for (const s of fundSchemes) {
          schemes.push({ ...s, fund_name: fund.name });
          const opt = document.createElement('option');
          opt.value = s.id;
          opt.textContent = `${fund.name} → ${s.name}`;
          select.appendChild(opt);

          const opt2 = opt.cloneNode(true);
          boardSelect.appendChild(opt2);
        }
      }

      select.onchange = () => loadInvestments();
    } catch (e) {
      console.error('Failed to load schemes:', e);
    }
  }

  // ── Load & render investments ─────────────────────────────
  async function loadInvestments() {
    try {
      const schemeId = document.getElementById('scheme-select').value;
      if (schemeId) {
        investments = await Auth.apiGet(`/schemes/${schemeId}/investments/`);
      } else {
        // Load from all schemes
        investments = [];
        for (const s of schemes) {
          try {
            const invs = await Auth.apiGet(`/schemes/${s.id}/investments/`);
            investments.push(...invs);
          } catch (e) { /* scheme may have no investments */ }
        }
      }
      renderInvestmentGrid();
      renderStats();
    } catch (e) {
      console.error('Failed to load investments:', e);
    }
  }

  function renderStats() {
    const bar = document.getElementById('stats-bar');
    bar.innerHTML = '';
    const totalInvested = investments.reduce((a, i) => a + parseFloat(i.total_invested || 0), 0);
    const active = investments.filter(i => i.status === 'active').length;
    const stats = [
      ['Total Investments', investments.length],
      ['Active', active],
      ['Total Deployed', fmtCurrency(totalInvested)],
      ['Schemes', schemes.length],
    ];
    stats.forEach(([label, value]) => {
      const div = document.createElement('div');
      div.className = 'hero-stat';
      div.innerHTML = `<span class="hero-stat-label">${label}</span><span class="hero-stat-value mono">${value}</span>`;
      bar.appendChild(div);
    });
  }

  function renderInvestmentGrid() {
    const grid = document.getElementById('investment-grid');
    grid.innerHTML = '';
    if (!investments.length) {
      grid.innerHTML = '<p style="color: var(--text-muted); padding: 20px;">No investments found. Create one using the button above.</p>';
      return;
    }

    investments.forEach(inv => {
      const card = document.createElement('div');
      card.className = 'fund-card';
      const statusClass = inv.status === 'active' ? 'active' : inv.status === 'written_off' ? 'closed' : '';
      card.innerHTML = `
        <div class="fund-card-head">
          <div>
            <div class="fund-card-name">${esc(inv.company_name)}</div>
            <div class="fund-card-meta">${esc(inv.instrument_type_display)} · ${esc(inv.currency)}</div>
          </div>
          <span class="fund-status ${statusClass}">${esc(inv.status_display)}</span>
        </div>
        <div class="fund-card-stats">
          <div class="fund-stat">
            <span class="fund-stat-label">Invested</span>
            <span class="fund-stat-value">${fmtCurrency(inv.total_invested)}</span>
          </div>
          <div class="fund-stat">
            <span class="fund-stat-label">Ownership</span>
            <span class="fund-stat-value">${inv.ownership_pct ? parseFloat(inv.ownership_pct).toFixed(2) + '%' : '—'}</span>
          </div>
          <div class="fund-stat">
            <span class="fund-stat-label">Tranches</span>
            <span class="fund-stat-value">${inv.tranche_count || 0}</span>
          </div>
          <div class="fund-stat">
            <span class="fund-stat-label">Latest Valuation</span>
            <span class="fund-stat-value">${fmtCurrency(inv.latest_valuation)}</span>
          </div>
        </div>
      `;
      card.onclick = () => showInvestmentDetail(inv.id);
      grid.appendChild(card);
    });
  }

  // ── Investment Detail ─────────────────────────────────────
  async function showInvestmentDetail(investmentId) {
    try {
      currentInvestment = await Auth.apiGet(`/investments/${investmentId}/`);
    } catch (e) {
      console.error('Failed to load investment detail:', e);
      return;
    }

    document.getElementById('tab-investments').classList.add('hidden');
    document.getElementById('investment-detail-section').classList.remove('hidden');

    const inv = currentInvestment;
    document.getElementById('inv-detail-tag').textContent = 'Investment Detail';
    document.getElementById('inv-detail-title').textContent = inv.company_name;
    document.getElementById('inv-detail-subtitle').textContent =
      `${inv.instrument_type_display} · ${inv.currency} · ${inv.status_display}`;

    // Info grid
    const grid = document.getElementById('inv-info-grid');
    grid.innerHTML = '';
    const fields = [
      ['Scheme', inv.scheme],
      ['Instrument', inv.instrument_type_display],
      ['Total Invested', fmtCurrency(inv.total_invested)],
      ['Ownership %', inv.ownership_pct ? parseFloat(inv.ownership_pct).toFixed(2) + '%' : '—'],
      ['Investment Date', fmtDate(inv.investment_date)],
      ['Sector', inv.sector || '—'],
      ['Board Seat', inv.board_seat ? 'Yes' : 'No'],
      ['Status', inv.status_display],
    ];
    fields.forEach(([label, val]) => {
      const el = document.createElement('div');
      el.className = 'detail-item';
      el.innerHTML = `<span class="detail-label">${label}</span><span class="detail-value">${esc(String(val))}</span>`;
      grid.appendChild(el);
    });

    // Load sub-resources
    renderTranches(inv.tranches || []);
    loadValuations(inv.id);
    loadExitScenarios(inv.id);
    loadBoardMeetings(inv.id);
  }

  function showInvestmentList() {
    document.getElementById('investment-detail-section').classList.add('hidden');
    document.getElementById('tab-investments').classList.remove('hidden');
    currentInvestment = null;
  }

  // ── Tranches ──────────────────────────────────────────────
  function renderTranches(tranches) {
    const list = document.getElementById('tranche-list');
    list.innerHTML = '';
    if (!tranches.length) {
      list.innerHTML = '<p style="color: var(--text-muted); padding: 12px;">No tranches recorded.</p>';
      return;
    }
    tranches.forEach(t => {
      const row = document.createElement('div');
      row.className = 'scheme-row';
      row.innerHTML = `
        <div class="scheme-row-main">
          <strong>Tranche ${t.tranche_number}</strong> — ${esc(t.round_name || 'N/A')}
          <span class="scheme-row-meta">${fmtDate(t.date)} · ${fmtCurrency(t.amount)}</span>
        </div>
        <div class="scheme-row-detail">
          ${t.shares_acquired ? `Shares: ${parseFloat(t.shares_acquired).toLocaleString()}` : ''}
          ${t.price_per_share ? ` @ ${fmtCurrency(t.price_per_share)}/share` : ''}
          ${t.post_money_valuation ? ` · Post-money: ${fmtCurrency(t.post_money_valuation)}` : ''}
        </div>
      `;
      list.appendChild(row);
    });
  }

  // ── Valuations (per investment) ───────────────────────────
  async function loadValuations(investmentId) {
    try {
      const vals = await Auth.apiGet(`/investments/${investmentId}/valuations/`);
      const list = document.getElementById('valuation-list');
      list.innerHTML = '';
      if (!vals.length) {
        list.innerHTML = '<p style="color: var(--text-muted); padding: 12px;">No valuations submitted.</p>';
        return;
      }
      vals.forEach(v => {
        const row = document.createElement('div');
        row.className = 'scheme-row';
        const statusBadge = v.status === 'approved' ? 'active' : v.status === 'rejected' ? 'closed' : '';
        row.innerHTML = `
          <div class="scheme-row-main">
            <strong>${esc(v.methodology_display)}</strong> — ${fmtDate(v.valuation_date)}
            <span class="fund-status ${statusBadge}" style="margin-left: 8px;">${esc(v.status_display)}</span>
          </div>
          <div class="scheme-row-detail">
            Fair Value: ${fmtCurrency(v.fair_value)}
            ${v.multiple ? ` · MOIC: ${parseFloat(v.multiple).toFixed(2)}x` : ''}
            ${v.unrealized_gain_loss ? ` · Unrealized G/L: ${fmtCurrency(v.unrealized_gain_loss)}` : ''}
          </div>
          ${v.status === 'submitted' ? `
            <div class="scheme-row-actions">
              <button class="btn-ghost small" onclick="InvestmentsPage.approveValuation('${v.id}', 'approve')">Approve</button>
              <button class="btn-ghost small" onclick="InvestmentsPage.approveValuation('${v.id}', 'reject')">Reject</button>
            </div>
          ` : ''}
        `;
        list.appendChild(row);
      });
    } catch (e) {
      console.error('Failed to load valuations:', e);
    }
  }

  async function approveValuation(valId, action) {
    try {
      const body = { action };
      if (action === 'reject') {
        const reason = prompt('Reason for rejection:');
        if (reason === null) return;
        body.reason = reason;
      }
      await Auth.apiPost(`/valuations/${valId}/approve/`, body);
      if (currentInvestment) loadValuations(currentInvestment.id);
      alert(`Valuation ${action}d successfully.`);
    } catch (e) {
      alert('Failed: ' + e.message);
    }
  }

  // ── Exit Scenarios (per investment) ───────────────────────
  async function loadExitScenarios(investmentId) {
    try {
      const exits = await Auth.apiGet(`/investments/${investmentId}/exit-scenarios/`);
      const list = document.getElementById('exit-list');
      list.innerHTML = '';
      if (!exits.length) {
        list.innerHTML = '<p style="color: var(--text-muted); padding: 12px;">No exit scenarios modelled.</p>';
        return;
      }
      exits.forEach(e => {
        const row = document.createElement('div');
        row.className = 'scheme-row';
        row.innerHTML = `
          <div class="scheme-row-main">
            <strong>${esc(e.exit_type_display)}</strong>
            <span class="fund-status ${e.is_actual ? 'active' : ''}">${e.is_actual ? 'ACTUAL' : 'SCENARIO'}</span>
          </div>
          <div class="scheme-row-detail">
            ${e.exit_date ? `Date: ${fmtDate(e.exit_date)}` : ''}
            ${e.proceeds ? ` · Proceeds: ${fmtCurrency(e.proceeds)}` : ''}
            ${e.moic ? ` · MOIC: ${parseFloat(e.moic).toFixed(2)}x` : ''}
            ${e.irr_pct ? ` · IRR: ${parseFloat(e.irr_pct).toFixed(1)}%` : ''}
          </div>
        `;
        list.appendChild(row);
      });
    } catch (e) {
      console.error('Failed to load exit scenarios:', e);
    }
  }

  // ── Board Meetings (per investment) ───────────────────────
  async function loadBoardMeetings(investmentId) {
    try {
      const meetings = await Auth.apiGet(`/investments/${investmentId}/board-meetings/`);
      const list = document.getElementById('board-list');
      list.innerHTML = '';
      if (!meetings.length) {
        list.innerHTML = '<p style="color: var(--text-muted); padding: 12px;">No board meetings recorded.</p>';
        return;
      }
      meetings.forEach(m => {
        const row = document.createElement('div');
        row.className = 'scheme-row';
        row.innerHTML = `
          <div class="scheme-row-main">
            <strong>Board Meeting ${m.meeting_number || ''}</strong> — ${fmtDate(m.meeting_date)}
          </div>
          <div class="scheme-row-detail">
            ${m.attendees && m.attendees.length ? `Attendees: ${m.attendees.join(', ')}` : ''}
            ${m.next_meeting_date ? ` · Next: ${fmtDate(m.next_meeting_date)}` : ''}
          </div>
        `;
        list.appendChild(row);
      });
    } catch (e) {
      console.error('Failed to load board meetings:', e);
    }
  }

  // ── All Valuations Tab ────────────────────────────────────
  async function loadAllValuations() {
    const list = document.getElementById('all-valuations-list');
    list.innerHTML = '<p style="color: var(--text-muted); padding: 12px;">Loading...</p>';
    try {
      let allVals = [];
      for (const inv of investments) {
        try {
          const vals = await Auth.apiGet(`/investments/${inv.id}/valuations/`);
          vals.forEach(v => { v._company = inv.company_name; });
          allVals.push(...vals);
        } catch (e) { /* skip */ }
      }
      list.innerHTML = '';
      if (!allVals.length) {
        list.innerHTML = '<p style="color: var(--text-muted); padding: 12px;">No valuations found.</p>';
        return;
      }
      allVals.sort((a, b) => new Date(b.valuation_date) - new Date(a.valuation_date));
      allVals.forEach(v => {
        const row = document.createElement('div');
        row.className = 'scheme-row';
        const statusBadge = v.status === 'approved' ? 'active' : v.status === 'rejected' ? 'closed' : '';
        row.innerHTML = `
          <div class="scheme-row-main">
            <strong>${esc(v._company)}</strong> — ${esc(v.methodology_display)} (${fmtDate(v.valuation_date)})
            <span class="fund-status ${statusBadge}" style="margin-left: 8px;">${esc(v.status_display)}</span>
          </div>
          <div class="scheme-row-detail">
            Fair Value: ${fmtCurrency(v.fair_value)}
            ${v.multiple ? ` · MOIC: ${parseFloat(v.multiple).toFixed(2)}x` : ''}
          </div>
          ${v.status === 'submitted' ? `
            <div class="scheme-row-actions">
              <button class="btn-ghost small" onclick="InvestmentsPage.approveValuation('${v.id}', 'approve')">Approve</button>
              <button class="btn-ghost small" onclick="InvestmentsPage.approveValuation('${v.id}', 'reject')">Reject</button>
            </div>
          ` : ''}
        `;
        list.appendChild(row);
      });
    } catch (e) {
      list.innerHTML = '<p style="color: var(--text-muted);">Failed to load valuations.</p>';
    }
  }

  // ── All KPIs Tab ──────────────────────────────────────────
  async function loadAllKPIs() {
    const list = document.getElementById('kpi-list');
    list.innerHTML = '<p style="color: var(--text-muted); padding: 12px;">Loading...</p>';
    try {
      let allKPIs = [];
      const statusFilter = document.getElementById('kpi-status-filter').value;
      for (const inv of investments) {
        try {
          let url = `/investments/${inv.id}/kpis/`;
          if (statusFilter) url += `?status=${statusFilter}`;
          const kpis = await Auth.apiGet(url);
          kpis.forEach(k => { k._company = inv.company_name; });
          allKPIs.push(...kpis);
        } catch (e) { /* skip */ }
      }
      list.innerHTML = '';
      if (!allKPIs.length) {
        list.innerHTML = '<p style="color: var(--text-muted); padding: 12px;">No KPI submissions found.</p>';
        return;
      }
      allKPIs.sort((a, b) => new Date(b.period) - new Date(a.period));
      allKPIs.forEach(k => {
        const row = document.createElement('div');
        row.className = 'scheme-row';
        const statusBadge = k.status === 'approved' ? 'active' : k.status === 'rejected' ? 'closed' : '';
        row.innerHTML = `
          <div class="scheme-row-main">
            <strong>${esc(k._company)}</strong> — ${esc(k.kpi_name)} (${fmtDate(k.period)})
            <span class="fund-status ${statusBadge}" style="margin-left: 8px;">${esc(k.status_display)}</span>
          </div>
          <div class="scheme-row-detail">
            Value: ${parseFloat(k.value).toLocaleString()} ${k.notes ? ` · ${esc(k.notes)}` : ''}
          </div>
          ${k.status === 'submitted' ? `
            <div class="scheme-row-actions">
              <button class="btn-ghost small" onclick="InvestmentsPage.reviewKPI('${k.id}', 'approve')">Approve</button>
              <button class="btn-ghost small" onclick="InvestmentsPage.reviewKPI('${k.id}', 'reject')">Reject</button>
            </div>
          ` : ''}
        `;
        list.appendChild(row);
      });

      // Wire up filter change
      document.getElementById('kpi-status-filter').onchange = () => loadAllKPIs();
    } catch (e) {
      list.innerHTML = '<p style="color: var(--text-muted);">Failed to load KPIs.</p>';
    }
  }

  async function reviewKPI(kpiId, action) {
    try {
      const body = { action };
      if (action === 'reject') {
        const reason = prompt('Reason for rejection:');
        if (reason === null) return;
        body.reason = reason;
      }
      await Auth.apiPut(`/kpis/${kpiId}/review/`, body);
      loadAllKPIs();
      alert(`KPI ${action}d successfully.`);
    } catch (e) {
      alert('Failed: ' + e.message);
    }
  }

  // ── All Exits Tab ─────────────────────────────────────────
  async function loadAllExits() {
    const list = document.getElementById('all-exits-list');
    list.innerHTML = '<p style="color: var(--text-muted); padding: 12px;">Loading...</p>';
    try {
      let allExits = [];
      for (const inv of investments) {
        try {
          const exits = await Auth.apiGet(`/investments/${inv.id}/exit-scenarios/`);
          exits.forEach(e => { e._company = inv.company_name; });
          allExits.push(...exits);
        } catch (e) { /* skip */ }
      }
      list.innerHTML = '';
      if (!allExits.length) {
        list.innerHTML = '<p style="color: var(--text-muted); padding: 12px;">No exit scenarios found.</p>';
        return;
      }
      allExits.forEach(e => {
        const row = document.createElement('div');
        row.className = 'scheme-row';
        row.innerHTML = `
          <div class="scheme-row-main">
            <strong>${esc(e._company)}</strong> — ${esc(e.exit_type_display)}
            <span class="fund-status ${e.is_actual ? 'active' : ''}">${e.is_actual ? 'ACTUAL' : 'SCENARIO'}</span>
          </div>
          <div class="scheme-row-detail">
            ${e.exit_date ? `Date: ${fmtDate(e.exit_date)}` : ''}
            ${e.proceeds ? ` · Proceeds: ${fmtCurrency(e.proceeds)}` : ''}
            ${e.moic ? ` · MOIC: ${parseFloat(e.moic).toFixed(2)}x` : ''}
            ${e.irr_pct ? ` · IRR: ${parseFloat(e.irr_pct).toFixed(1)}%` : ''}
          </div>
        `;
        list.appendChild(row);
      });
    } catch (e) {
      list.innerHTML = '<p style="color: var(--text-muted);">Failed to load exit scenarios.</p>';
    }
  }

  // ── All Board Meetings Tab ────────────────────────────────
  async function loadAllBoardMeetings() {
    const list = document.getElementById('all-board-list');
    list.innerHTML = '<p style="color: var(--text-muted); padding: 12px;">Loading...</p>';
    try {
      // Board meetings are nested in investment detail, so we need to load each
      let allMeetings = [];
      for (const inv of investments) {
        try {
          const meetings = await Auth.apiGet(`/investments/${inv.id}/board-meetings/`);
          meetings.forEach(m => { m._company = inv.company_name; });
          allMeetings.push(...meetings);
        } catch (e) { /* skip */ }
      }
      list.innerHTML = '';
      if (!allMeetings.length) {
        list.innerHTML = '<p style="color: var(--text-muted); padding: 12px;">No board meetings found.</p>';
        return;
      }
      allMeetings.sort((a, b) => new Date(b.meeting_date) - new Date(a.meeting_date));
      allMeetings.forEach(m => {
        const row = document.createElement('div');
        row.className = 'scheme-row';
        row.innerHTML = `
          <div class="scheme-row-main">
            <strong>${esc(m._company)}</strong> — Board Meeting ${m.meeting_number || ''}
            <span class="scheme-row-meta">${fmtDate(m.meeting_date)}</span>
          </div>
          <div class="scheme-row-detail">
            ${m.attendees && m.attendees.length ? `Attendees: ${m.attendees.join(', ')}` : ''}
            ${m.resolutions && m.resolutions.length ? ` · ${m.resolutions.length} resolution(s)` : ''}
          </div>
        `;
        list.appendChild(row);
      });
    } catch (e) {
      list.innerHTML = '<p style="color: var(--text-muted);">Failed to load board meetings.</p>';
    }
  }

  // ── Board Pack Generation ─────────────────────────────────
  async function generateBoardPack() {
    const schemeId = document.getElementById('board-pack-scheme').value;
    if (!schemeId) {
      alert('Please select a scheme first.');
      return;
    }
    try {
      const pack = await Auth.apiPost(`/schemes/${schemeId}/board-pack/generate/`, {});
      // Display as JSON in a new window for now
      const blob = new Blob([JSON.stringify(pack, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      window.open(url, '_blank');
      alert('Board pack generated! A new tab has opened with the data.');
    } catch (e) {
      alert('Failed to generate board pack: ' + e.message);
    }
  }

  // ═══════════════════════════════════════════════════════════
  // MODAL FORMS
  // ═══════════════════════════════════════════════════════════

  function openModal(title, fields, callback) {
    document.getElementById('modal-title').textContent = title;
    const container = document.getElementById('modal-fields');
    container.innerHTML = '';

    fields.forEach(f => {
      const group = document.createElement('div');
      group.className = 'form-group';
      if (f.type === 'select') {
        group.innerHTML = `
          <label>${f.label}</label>
          <select name="${f.name}" class="select-dark" ${f.required ? 'required' : ''}>
            ${f.options.map(o => `<option value="${o.value}">${o.label}</option>`).join('')}
          </select>
        `;
      } else if (f.type === 'textarea') {
        group.innerHTML = `
          <label>${f.label}</label>
          <textarea name="${f.name}" class="input-dark" rows="3" ${f.required ? 'required' : ''}>${f.value || ''}</textarea>
        `;
      } else if (f.type === 'checkbox') {
        group.innerHTML = `
          <label class="switch-label">
            <input type="checkbox" name="${f.name}" ${f.value ? 'checked' : ''} />
            <span>${f.label}</span>
          </label>
        `;
      } else {
        group.innerHTML = `
          <label>${f.label}</label>
          <input type="${f.type || 'text'}" name="${f.name}" class="input-dark"
                 value="${f.value || ''}" ${f.required ? 'required' : ''}
                 ${f.step ? `step="${f.step}"` : ''} />
        `;
      }
      container.appendChild(group);
    });

    modalCallback = callback;
    document.getElementById('modal-overlay').classList.remove('hidden');
  }

  function closeModal() {
    document.getElementById('modal-overlay').classList.add('hidden');
    modalCallback = null;
  }

  function handleModalSubmit(e) {
    e.preventDefault();
    const form = e.target;
    const data = {};
    new FormData(form).forEach((val, key) => { data[key] = val; });
    // Handle checkboxes (unchecked ones don't appear in FormData)
    form.querySelectorAll('input[type="checkbox"]').forEach(cb => {
      data[cb.name] = cb.checked;
    });
    if (modalCallback) modalCallback(data);
    closeModal();
  }

  // ── Investment Form ───────────────────────────────────────
  function openInvestmentForm() {
    const schemeOpts = schemes.map(s => ({
      value: s.id,
      label: `${s.fund_name} → ${s.name}`,
    }));
    if (!schemeOpts.length) {
      alert('No schemes found. Create a fund and scheme in Fund Admin first.');
      return;
    }
    openModal('New Investment', [
      { name: 'scheme_id', label: 'Scheme', type: 'select', options: schemeOpts, required: true },
      { name: 'company_name', label: 'Company Name', required: true },
      { name: 'instrument_type', label: 'Instrument Type', type: 'select', options: [
        { value: 'equity', label: 'Equity' },
        { value: 'ccps', label: 'CCPS' },
        { value: 'ccd', label: 'CCD' },
        { value: 'ncd', label: 'NCD' },
        { value: 'safe', label: 'SAFE' },
        { value: 'convertible_note', label: 'Convertible Note' },
      ]},
      { name: 'ownership_pct', label: 'Ownership %', type: 'number', step: '0.01' },
      { name: 'total_invested', label: 'Total Invested', type: 'number', step: '0.01' },
      { name: 'investment_date', label: 'Investment Date', type: 'date' },
      { name: 'currency', label: 'Currency', value: 'INR' },
      { name: 'sector', label: 'Sector' },
      { name: 'board_seat', label: 'Board Seat', type: 'checkbox' },
      { name: 'description', label: 'Description', type: 'textarea' },
    ], async (data) => {
      try {
        const schemeId = data.scheme_id;
        delete data.scheme_id;
        data.ownership_pct = data.ownership_pct || null;
        data.total_invested = data.total_invested || 0;
        await Auth.apiPost(`/schemes/${schemeId}/investments/`, data);
        await loadInvestments();
      } catch (e) {
        alert('Failed to create investment: ' + e.message);
      }
    });
  }

  // ── Tranche Form ──────────────────────────────────────────
  function openTrancheForm() {
    if (!currentInvestment) return;
    const nextNum = (currentInvestment.tranches || []).length + 1;
    openModal('Add Tranche', [
      { name: 'tranche_number', label: 'Tranche Number', type: 'number', value: nextNum, required: true },
      { name: 'amount', label: 'Amount', type: 'number', step: '0.01', required: true },
      { name: 'date', label: 'Date', type: 'date', required: true },
      { name: 'round_name', label: 'Round Name (e.g., Series A)' },
      { name: 'shares_acquired', label: 'Shares Acquired', type: 'number', step: '0.0001' },
      { name: 'price_per_share', label: 'Price per Share', type: 'number', step: '0.0001' },
      { name: 'pre_money_valuation', label: 'Pre-money Valuation', type: 'number', step: '0.01' },
      { name: 'post_money_valuation', label: 'Post-money Valuation', type: 'number', step: '0.01' },
      { name: 'notes', label: 'Notes', type: 'textarea' },
    ], async (data) => {
      try {
        // Clean up empty numeric fields
        ['shares_acquired', 'price_per_share', 'pre_money_valuation', 'post_money_valuation'].forEach(f => {
          if (!data[f]) data[f] = null;
        });
        await Auth.apiPost(`/investments/${currentInvestment.id}/tranches/`, data);
        await showInvestmentDetail(currentInvestment.id);
      } catch (e) {
        alert('Failed to add tranche: ' + e.message);
      }
    });
  }

  // ── Valuation Form ────────────────────────────────────────
  function openValuationForm() {
    if (!currentInvestment) return;
    openModal('Submit Valuation', [
      { name: 'valuation_date', label: 'Valuation Date', type: 'date', required: true },
      { name: 'methodology', label: 'Methodology', type: 'select', options: [
        { value: 'dcf', label: 'Discounted Cash Flow' },
        { value: 'comparables', label: 'Market Comparables' },
        { value: 'recent_transaction', label: 'Recent Transaction' },
        { value: 'net_assets', label: 'Net Assets' },
        { value: 'cost', label: 'Cost (at cost)' },
      ], required: true },
      { name: 'fair_value', label: 'Fair Value', type: 'number', step: '0.01', required: true },
      { name: 'cost_basis', label: 'Cost Basis', type: 'number', step: '0.01' },
      { name: 'multiple', label: 'MOIC', type: 'number', step: '0.01' },
      { name: 'discount_rate', label: 'Discount Rate (%)', type: 'number', step: '0.01' },
      { name: 'assumptions', label: 'Assumptions & Notes', type: 'textarea' },
    ], async (data) => {
      try {
        ['cost_basis', 'multiple', 'discount_rate'].forEach(f => {
          if (!data[f]) data[f] = null;
        });
        // Compute unrealized gain/loss
        if (data.cost_basis && data.fair_value) {
          data.unrealized_gain_loss = parseFloat(data.fair_value) - parseFloat(data.cost_basis);
        }
        await Auth.apiPost(`/investments/${currentInvestment.id}/valuations/`, data);
        await loadValuations(currentInvestment.id);
      } catch (e) {
        alert('Failed to submit valuation: ' + e.message);
      }
    });
  }

  // ── Exit Scenario Form ────────────────────────────────────
  function openExitForm() {
    if (!currentInvestment) return;
    openModal('Model Exit Scenario', [
      { name: 'exit_type', label: 'Exit Type', type: 'select', options: [
        { value: 'ipo', label: 'IPO' },
        { value: 'merger_acquisition', label: 'M&A' },
        { value: 'secondary_sale', label: 'Secondary Sale' },
        { value: 'buyback', label: 'Buyback' },
        { value: 'write_off', label: 'Write-Off' },
      ], required: true },
      { name: 'is_actual', label: 'This exit has occurred (actual)', type: 'checkbox' },
      { name: 'exit_date', label: 'Exit Date', type: 'date' },
      { name: 'exit_valuation', label: 'Exit Valuation', type: 'number', step: '0.01' },
      { name: 'proceeds', label: 'Proceeds to Fund', type: 'number', step: '0.01' },
      { name: 'moic', label: 'MOIC', type: 'number', step: '0.01' },
      { name: 'irr_pct', label: 'IRR %', type: 'number', step: '0.01' },
      { name: 'buyer_name', label: 'Buyer / Acquirer' },
      { name: 'assumptions', label: 'Assumptions', type: 'textarea' },
    ], async (data) => {
      try {
        ['exit_valuation', 'proceeds', 'moic', 'irr_pct'].forEach(f => {
          if (!data[f]) data[f] = null;
        });
        // Compute realized gain/loss
        if (data.proceeds && currentInvestment.total_invested) {
          data.realized_gain_loss = parseFloat(data.proceeds) - parseFloat(currentInvestment.total_invested);
        }
        await Auth.apiPost(`/investments/${currentInvestment.id}/exit-scenarios/`, data);
        await loadExitScenarios(currentInvestment.id);
      } catch (e) {
        alert('Failed to create exit scenario: ' + e.message);
      }
    });
  }

  // ── Board Meeting Form ────────────────────────────────────
  function openBoardForm() {
    if (!currentInvestment) return;
    openModal('Add Board Meeting', [
      { name: 'meeting_date', label: 'Meeting Date', type: 'date', required: true },
      { name: 'meeting_number', label: 'Meeting Number', type: 'number' },
      { name: 'agenda', label: 'Agenda', type: 'textarea' },
      { name: 'minutes', label: 'Minutes', type: 'textarea' },
      { name: 'next_meeting_date', label: 'Next Meeting Date', type: 'date' },
    ], async (data) => {
      try {
        if (!data.meeting_number) data.meeting_number = null;
        if (!data.next_meeting_date) data.next_meeting_date = null;
        data.attendees = [];
        data.resolutions = [];
        await Auth.apiPost(`/investments/${currentInvestment.id}/board-meetings/`, data);
        await loadBoardMeetings(currentInvestment.id);
      } catch (e) {
        alert('Failed to create board meeting: ' + e.message);
      }
    });
  }

  // ── Expose globally for onclick handlers ──────────────────
  window.InvestmentsPage = {
    approveValuation,
    reviewKPI,
  };

  // ── Boot ──────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', init);
})();
