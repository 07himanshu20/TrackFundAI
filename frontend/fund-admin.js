/* ============================================================
   fund-admin.js
   TrackFundAI Module 1 — Fund Administration UI
   Lists funds, shows fund detail with schemes + entities,
   supports create/edit via modal forms.
============================================================ */

(() => {
  let funds = [];
  let currentFund = null;    // full fund detail (with schemes + entities)
  let modalCallback = null;  // function called with form data on submit
  let fundCategories = [];   // cached SEBI fund categories for dropdowns
  let orgEntities = [];      // cached org-level entities for dropdowns

  // ── Formatting ────────────────────────────────────────────
  const fmtCurrency = (v) => {
    if (!v) return '—';
    const n = parseFloat(v);
    if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
    if (n >= 1e7) return `${(n / 1e7).toFixed(1)}Cr`;
    if (n >= 1e5) return `${(n / 1e5).toFixed(1)}L`;
    return n.toLocaleString('en-IN');
  };

  const esc = (s) => {
    if (s === null || s === undefined) return '—';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  };

  // ── Init ──────────────────────────────────────────────────
  async function init() {
    if (!Auth.requireAuth()) return;

    const user = Auth.getUser();
    document.getElementById('user-badge').textContent =
      `${user.first_name || user.username} · ${user.role.replace('_', ' ').toUpperCase()}`;
    document.getElementById('org-label').textContent =
      user.organization_name || 'Unknown Organization';

    document.getElementById('btn-logout').onclick = () => Auth.logout();
    document.getElementById('btn-new-fund').onclick = () => openFundForm();
    document.getElementById('btn-back-list').onclick = () => showList();
    document.getElementById('btn-new-scheme').onclick = () => openSchemeForm();
    document.getElementById('btn-new-entity').onclick = () => openEntityForm();
    document.getElementById('btn-link-entity').onclick = () => openLinkEntityForm();

    // Modal controls
    document.getElementById('modal-close').onclick = closeModal;
    document.getElementById('modal-cancel').onclick = closeModal;
    document.getElementById('modal-form').onsubmit = handleModalSubmit;

    await Promise.all([loadFunds(), loadFundCategories(), loadOrgEntities()]);
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
    } catch (e) {
      console.error('Failed to load notification count:', e);
    }
  }

  // ── Load fund categories & entities ────────────────────────
  async function loadFundCategories() {
    try {
      fundCategories = await Auth.apiGet('/funds/categories/');
    } catch (e) {
      console.error('Failed to load fund categories:', e);
    }
  }

  async function loadOrgEntities() {
    try {
      orgEntities = await Auth.apiGet('/funds/entities/');
    } catch (e) {
      console.error('Failed to load entities:', e);
    }
  }

  // ── Load funds ────────────────────────────────────────────
  async function loadFunds() {
    try {
      funds = await Auth.apiGet('/funds/');
      renderFundGrid();
      renderStats();
    } catch (e) {
      console.error('Failed to load funds:', e);
    }
  }

  function renderStats() {
    const bar = document.getElementById('stats-bar');
    bar.innerHTML = '';
    const chips = [
      ['Total Funds', funds.length],
      ['Active', funds.filter(f => f.fund_status === 'active').length],
      ['Total Schemes', funds.reduce((a, f) => a + (f.scheme_count || 0), 0)],
    ];
    chips.forEach(([label, value]) => {
      const div = document.createElement('div');
      div.className = 'hero-stat';
      div.innerHTML = `<span class="hero-stat-label">${label}</span><span class="hero-stat-value mono">${value}</span>`;
      bar.appendChild(div);
    });
  }

  function renderFundGrid() {
    const grid = document.getElementById('fund-grid');
    grid.innerHTML = '';

    if (funds.length === 0) {
      grid.innerHTML = `
        <div style="text-align:center; padding:60px 20px;">
          <div style="font-size:48px; margin-bottom:16px;">📂</div>
          <p style="color:var(--text-secondary); font-size:16px; margin-bottom:8px;">No fund data found</p>
          <p style="color:var(--text-muted); font-size:13px; margin-bottom:24px;">Upload your fund Excel files to get started. Gemini AI will map columns and import everything automatically.</p>
          <a href="data-upload.html" style="display:inline-block; padding:12px 32px; background:var(--accent-blue); color:var(--bg-void); border-radius:8px; text-decoration:none; font-weight:600; font-size:14px;">Upload Fund Data</a>
          <p style="color:var(--text-muted); font-size:12px; margin-top:16px;">or click "+ New Fund" above to create manually</p>
        </div>`;
      return;
    }

    funds.forEach(f => {
      const card = document.createElement('div');
      card.className = 'fund-card';
      card.innerHTML = `
        <div class="fund-card-header">
          <div>
            <div class="fund-card-name">${esc(f.name)}</div>
            <div class="fund-card-sebi">${f.sebi_registration_number || 'No SEBI registration'}</div>
          </div>
          <span class="fund-card-badge ${f.fund_status}">${f.status_display}</span>
        </div>
        <div class="fund-card-metrics">
          <div class="fund-card-metric">
            <span class="label">Category</span>
            <span class="value">${f.category_name || '—'}</span>
          </div>
          <div class="fund-card-metric">
            <span class="label">Structure</span>
            <span class="value">${f.structure_display}</span>
          </div>
          <div class="fund-card-metric">
            <span class="label">Corpus</span>
            <span class="value">${f.base_currency} ${fmtCurrency(f.corpus_target)}</span>
          </div>
        </div>
        <div class="fund-card-chevron">&rsaquo;</div>
      `;
      card.onclick = () => loadFundDetail(f.id);
      grid.appendChild(card);
    });
  }

  // ── Fund detail ───────────────────────────────────────────
  async function loadFundDetail(fundId) {
    try {
      currentFund = await Auth.apiGet(`/funds/${fundId}/`);
      renderFundDetail();
      showDetail();
    } catch (e) {
      console.error('Failed to load fund:', e);
    }
  }

  function renderFundDetail() {
    const f = currentFund;
    document.getElementById('detail-tag').textContent = f.category_name || 'Fund Detail';
    document.getElementById('detail-title').textContent = f.name;
    document.getElementById('detail-subtitle').textContent =
      f.sebi_registration_number || 'No SEBI registration number';

    // Info grid
    const grid = document.getElementById('fund-info-grid');
    grid.innerHTML = '';
    const catDetail = f.fund_category_detail;
    const items = [
      ['Status', f.status_display],
      ['Category', f.category_name || '—'],
      ['Sub-Category', catDetail ? catDetail.sub_category || '—' : '—'],
      ['Structure', f.structure_display],
      ['Base Currency', f.base_currency],
      ['Target Corpus', `${f.base_currency} ${fmtCurrency(f.corpus_target)}`],
      ['Inception Date', f.inception_date || '—'],
      ['SEBI Registration', f.sebi_registration_number || '—'],
      ['PAN', f.pan || '—'],
      ['GSTIN', f.gstin || '—'],
      ['GIFT City', f.is_gift_city ? 'Yes' : 'No'],
    ];
    items.forEach(([label, value]) => {
      const div = document.createElement('div');
      div.className = 'detail-item';
      div.innerHTML = `<div class="label">${label}</div><div class="value">${esc(value)}</div>`;
      grid.appendChild(div);
    });

    // Schemes
    const schemeList = document.getElementById('scheme-list');
    schemeList.innerHTML = '';
    (f.schemes || []).forEach(s => {
      const card = document.createElement('div');
      card.className = 'scheme-card';
      card.innerHTML = `
        <div class="scheme-card-header">
          <span class="scheme-card-name">${esc(s.name)}</span>
          <span class="scheme-card-role">${s.carry_type_display}</span>
        </div>
        <div class="scheme-card-metrics">
          <div class="scheme-metric"><span class="label">Vintage</span><span class="value">${s.vintage_year || '—'}</span></div>
          <div class="scheme-metric"><span class="label">Size</span><span class="value">${fmtCurrency(s.scheme_size)}</span></div>
          <div class="scheme-metric"><span class="label">Hurdle</span><span class="value">${s.hurdle_rate_pct ? s.hurdle_rate_pct + '%' : '—'}</span></div>
          <div class="scheme-metric"><span class="label">Carry</span><span class="value">${s.carry_pct ? s.carry_pct + '%' : '—'}</span></div>
          <div class="scheme-metric"><span class="label">Mgmt Fee</span><span class="value">${s.management_fee_pct ? s.management_fee_pct + '% (' + s.fee_basis_display + ')' : '—'}</span></div>
          <div class="scheme-metric"><span class="label">First Close</span><span class="value">${s.first_close_date || '—'}</span></div>
        </div>
      `;
      schemeList.appendChild(card);
    });
    if (f.schemes.length === 0) {
      schemeList.innerHTML = '<p style="color: var(--text-muted); padding: 20px;">No schemes yet.</p>';
    }

    // Entities (linked via FK on fund)
    const entityList = document.getElementById('entity-list');
    entityList.innerHTML = '';
    const entitySlots = [
      ['Investment Manager', f.manager_entity_detail],
      ['Trustee', f.trustee_entity_detail],
      ['Sponsor', f.sponsor_entity_detail],
      ['Custodian', f.custodian_entity_detail],
      ['Statutory Auditor', f.auditor_entity_detail],
    ];
    let entityCount = 0;
    entitySlots.forEach(([role, e]) => {
      if (!e) return;
      entityCount++;
      const card = document.createElement('div');
      card.className = 'entity-card';
      card.innerHTML = `
        <div class="entity-card-header">
          <span class="entity-card-name">${esc(e.entity_name)}</span>
          <span class="scheme-card-role">${role}</span>
        </div>
        <div class="entity-card-details">
          <div class="entity-detail"><span class="label">Type</span><span class="value">${esc(e.entity_type_display)}</span></div>
          <div class="entity-detail"><span class="label">SEBI Reg.</span><span class="value">${esc(e.sebi_registration)}</span></div>
        </div>
      `;
      entityList.appendChild(card);
    });
    if (entityCount === 0) {
      entityList.innerHTML = '<p style="color: var(--text-muted); padding: 20px;">No entities linked. Use "+ Link Entity" to assign entities to this fund.</p>';
    }
  }

  function showList() {
    document.getElementById('fund-detail-section').classList.add('hidden');
    document.querySelector('.section:nth-of-type(2)').classList.remove('hidden');
    currentFund = null;
    window.scrollTo({top: 0, behavior: 'smooth'});
  }

  function showDetail() {
    document.querySelector('.section:nth-of-type(2)').classList.add('hidden');
    document.getElementById('fund-detail-section').classList.remove('hidden');
    window.scrollTo({top: 0, behavior: 'smooth'});
  }

  // ── Modal / Forms ─────────────────────────────────────────
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
          ${f.options.map(o => `<option value="${o.value}" ${o.value === f.default ? 'selected' : ''}>${o.label}</option>`).join('')}
        </select>`;
      } else if (f.type === 'textarea') {
        input = `<textarea name="${f.name}" placeholder="${f.placeholder || ''}" ${f.required ? 'required' : ''}>${f.default || ''}</textarea>`;
      } else {
        input = `<input type="${f.type || 'text'}" name="${f.name}" value="${f.default || ''}" placeholder="${f.placeholder || ''}" ${f.required ? 'required' : ''} ${f.step ? 'step="' + f.step + '"' : ''} />`;
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
    new FormData(form).forEach((v, k) => {
      // Convert numeric fields
      if (v === '') return;
      const num = Number(v);
      if (!isNaN(num) && v.trim() !== '' && !['name', 'entity_name', 'description', 'contact_person', 'email', 'phone', 'sebi_registration', 'sebi_registration_number', 'address', 'city', 'state', 'country', 'pan', 'gstin', 'fund_category', 'manager_entity', 'trustee_entity', 'sponsor_entity', 'custodian_entity', 'auditor_entity'].includes(k)) {
        data[k] = num;
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

  // ── Fund form ─────────────────────────────────────────────
  function openFundForm() {
    const categoryOptions = [{value: '', label: '— Select Category —'}].concat(
      fundCategories.map(c => ({value: c.id, label: `${c.name}${c.sub_category ? ' — ' + c.sub_category : ''}`}))
    );
    const entityOptions = (type) => {
      const filtered = orgEntities.filter(e => !type || e.entity_type === type);
      return [{value: '', label: '— None —'}].concat(
        filtered.map(e => ({value: e.id, label: e.entity_name}))
      );
    };

    openModal('Create New Fund', [
      {name: 'name', label: 'Fund Name', required: true, placeholder: 'e.g., Trivesta Growth Fund II'},
      {name: 'sebi_registration_number', label: 'SEBI Registration Number', placeholder: 'IN/AIF2/XX-XX/XXXX'},
      {name: 'fund_category', label: 'SEBI Category', type: 'select', options: categoryOptions},
      {name: 'structure_type', label: 'Structure', type: 'select', default: 'trust', options: [
        {value: 'trust', label: 'Trust'},
        {value: 'company', label: 'Company'},
        {value: 'llp', label: 'LLP'},
      ]},
      {name: 'corpus_target', label: 'Target Corpus', type: 'number', placeholder: '500000000', step: '0.01'},
      {name: 'base_currency', label: 'Base Currency', type: 'select', default: 'INR', options: [
        {value: 'INR', label: 'INR'},
        {value: 'USD', label: 'USD'},
      ]},
      {name: 'pan', label: 'PAN', placeholder: 'AAACX1234X'},
      {name: 'gstin', label: 'GSTIN', placeholder: 'Optional'},
      {name: 'inception_date', label: 'Inception Date', type: 'date'},
      {name: 'manager_entity', label: 'Investment Manager', type: 'select', options: entityOptions('manager')},
      {name: 'trustee_entity', label: 'Trustee', type: 'select', options: entityOptions('trustee')},
      {name: 'sponsor_entity', label: 'Sponsor', type: 'select', options: entityOptions('sponsor')},
      {name: 'custodian_entity', label: 'Custodian', type: 'select', options: entityOptions('custodian')},
      {name: 'auditor_entity', label: 'Statutory Auditor', type: 'select', options: entityOptions('statutory_auditor')},
      {name: 'description', label: 'Description', type: 'textarea', placeholder: 'Fund description...'},
    ], async (data) => {
      // Remove empty string values for optional FK fields
      ['fund_category', 'manager_entity', 'trustee_entity', 'sponsor_entity',
       'custodian_entity', 'auditor_entity'].forEach(k => {
        if (!data[k]) delete data[k];
      });
      await Auth.apiPost('/funds/', data);
      await loadFunds();
    });
  }

  // ── Scheme form ───────────────────────────────────────────
  function openSchemeForm() {
    if (!currentFund) return;
    openModal('Add Scheme', [
      {name: 'name', label: 'Scheme Name', required: true, placeholder: 'e.g., Scheme I'},
      {name: 'vintage_year', label: 'Vintage Year', type: 'number', placeholder: '2025'},
      {name: 'scheme_size', label: 'Scheme Size', type: 'number', step: '0.01', placeholder: '250000000'},
      {name: 'hurdle_rate_pct', label: 'Hurdle Rate (%)', type: 'number', step: '0.01', placeholder: '8.00'},
      {name: 'carry_pct', label: 'Carry (%)', type: 'number', step: '0.01', placeholder: '20.00'},
      {name: 'carry_type', label: 'Waterfall Type', type: 'select', default: 'european', options: [
        {value: 'european', label: 'European (Whole Fund)'},
        {value: 'american', label: 'American (Deal-by-Deal)'},
      ]},
      {name: 'management_fee_basis', label: 'Fee Basis', type: 'select', default: 'committed', options: [
        {value: 'committed', label: 'Committed Capital'},
        {value: 'called', label: 'Called Capital'},
        {value: 'nav', label: 'NAV'},
      ]},
      {name: 'management_fee_pct', label: 'Mgmt Fee (%)', type: 'number', step: '0.01', placeholder: '2.00'},
      {name: 'first_close_date', label: 'First Close Date', type: 'date'},
      {name: 'final_close_date', label: 'Final Close Date', type: 'date'},
    ], async (data) => {
      await Auth.apiPost(`/funds/${currentFund.id}/schemes/`, data);
      await loadFundDetail(currentFund.id);
    });
  }

  // ── Entity form (org-level) ────────────────────────────────
  function openEntityForm() {
    openModal('Create Entity', [
      {name: 'entity_type', label: 'Entity Type', type: 'select', required: true, options: [
        {value: 'manager', label: 'Investment Manager'},
        {value: 'trustee', label: 'Trustee'},
        {value: 'sponsor', label: 'Sponsor'},
        {value: 'custodian', label: 'Custodian'},
        {value: 'statutory_auditor', label: 'Statutory Auditor'},
        {value: 'legal_counsel', label: 'Legal Counsel'},
        {value: 'registrar', label: 'Registrar & Transfer Agent'},
      ]},
      {name: 'entity_name', label: 'Entity Name', required: true, placeholder: 'e.g., Axis Trustee Services Ltd.'},
      {name: 'pan', label: 'PAN', placeholder: 'AAACX1234X'},
      {name: 'gstin', label: 'GSTIN', placeholder: 'Optional'},
      {name: 'sebi_registration', label: 'SEBI Registration', placeholder: 'If applicable'},
      {name: 'contact_person', label: 'Contact Person', placeholder: 'Name'},
      {name: 'email', label: 'Email', type: 'email', placeholder: 'email@example.com'},
      {name: 'phone', label: 'Phone', placeholder: '+91-XXXXX-XXXXX'},
      {name: 'address', label: 'Address', type: 'textarea', placeholder: 'Full address'},
      {name: 'city', label: 'City', placeholder: 'Mumbai'},
      {name: 'state', label: 'State', placeholder: 'Maharashtra'},
      {name: 'country', label: 'Country', default: 'India'},
    ], async (data) => {
      await Auth.apiPost('/funds/entities/', data);
      await loadOrgEntities();
      if (currentFund) await loadFundDetail(currentFund.id);
    });
  }

  // ── Link entity to fund form ──────────────────────────────
  function openLinkEntityForm() {
    if (!currentFund) return;
    const makeOpts = (role) => {
      const filtered = orgEntities.filter(e => !role || e.entity_type === role);
      return [{value: '', label: '— None —'}].concat(
        filtered.map(e => ({value: e.id, label: e.entity_name}))
      );
    };

    openModal('Link Entities to Fund', [
      {name: 'manager_entity', label: 'Investment Manager', type: 'select',
       default: currentFund.manager_entity || '', options: makeOpts('manager')},
      {name: 'trustee_entity', label: 'Trustee', type: 'select',
       default: currentFund.trustee_entity || '', options: makeOpts('trustee')},
      {name: 'sponsor_entity', label: 'Sponsor', type: 'select',
       default: currentFund.sponsor_entity || '', options: makeOpts('sponsor')},
      {name: 'custodian_entity', label: 'Custodian', type: 'select',
       default: currentFund.custodian_entity || '', options: makeOpts('custodian')},
      {name: 'auditor_entity', label: 'Statutory Auditor', type: 'select',
       default: currentFund.auditor_entity || '', options: makeOpts('statutory_auditor')},
    ], async (data) => {
      // Convert empty strings to null for FK clearing
      const payload = {};
      Object.entries(data).forEach(([k, v]) => { payload[k] = v || null; });
      await Auth.apiPut(`/funds/${currentFund.id}/`, payload);
      await loadFundDetail(currentFund.id);
    });
  }

  // ── Boot ──────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
