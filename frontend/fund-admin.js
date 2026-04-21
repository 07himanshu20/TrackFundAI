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

    // Modal controls
    document.getElementById('modal-close').onclick = closeModal;
    document.getElementById('modal-cancel').onclick = closeModal;
    document.getElementById('modal-form').onsubmit = handleModalSubmit;

    await loadFunds();
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
      ['Active', funds.filter(f => f.status === 'active').length],
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
      grid.innerHTML = '<p style="color: var(--text-muted); padding: 40px; text-align: center;">No funds yet. Click "+ New Fund" to create one.</p>';
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
          <span class="fund-card-badge ${f.status}">${f.status_display}</span>
        </div>
        <div class="fund-card-metrics">
          <div class="fund-card-metric">
            <span class="label">Category</span>
            <span class="value">${f.category_display}</span>
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
    document.getElementById('detail-tag').textContent = f.category_display;
    document.getElementById('detail-title').textContent = f.name;
    document.getElementById('detail-subtitle').textContent =
      f.sebi_registration_number || 'No SEBI registration number';

    // Info grid
    const grid = document.getElementById('fund-info-grid');
    grid.innerHTML = '';
    const items = [
      ['Status', f.status_display],
      ['Category', f.category_display],
      ['Structure', f.structure_display],
      ['Base Currency', f.base_currency],
      ['Target Corpus', `${f.base_currency} ${fmtCurrency(f.corpus_target)}`],
      ['Inception Date', f.inception_date || '—'],
      ['SEBI Registration', f.sebi_registration_number || '—'],
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

    // Entities
    const entityList = document.getElementById('entity-list');
    entityList.innerHTML = '';
    (f.entities || []).forEach(e => {
      const card = document.createElement('div');
      card.className = 'entity-card';
      card.innerHTML = `
        <div class="entity-card-header">
          <span class="entity-card-name">${esc(e.name)}</span>
          <span class="scheme-card-role">${e.role_display}</span>
        </div>
        <div class="entity-card-details">
          <div class="entity-detail"><span class="label">Contact</span><span class="value">${esc(e.contact_person)}</span></div>
          <div class="entity-detail"><span class="label">Email</span><span class="value">${esc(e.email)}</span></div>
          <div class="entity-detail"><span class="label">Phone</span><span class="value">${esc(e.phone)}</span></div>
          <div class="entity-detail"><span class="label">SEBI Reg.</span><span class="value">${esc(e.sebi_registration)}</span></div>
        </div>
      `;
      entityList.appendChild(card);
    });
    if (f.entities.length === 0) {
      entityList.innerHTML = '<p style="color: var(--text-muted); padding: 20px;">No entities yet.</p>';
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
      if (!isNaN(num) && v.trim() !== '' && !['name', 'description', 'contact_person', 'email', 'phone', 'sebi_registration', 'sebi_registration_number', 'address'].includes(k)) {
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
    openModal('Create New Fund', [
      {name: 'name', label: 'Fund Name', required: true, placeholder: 'e.g., Trivesta Growth Fund II'},
      {name: 'sebi_registration_number', label: 'SEBI Registration Number', placeholder: 'IN/AIF2/XX-XX/XXXX'},
      {name: 'category', label: 'Category', type: 'select', default: 'cat_2', options: [
        {value: 'cat_1', label: 'Category I'},
        {value: 'cat_2', label: 'Category II'},
        {value: 'cat_3', label: 'Category III'},
      ]},
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
      {name: 'inception_date', label: 'Inception Date', type: 'date'},
      {name: 'description', label: 'Description', type: 'textarea', placeholder: 'Fund description...'},
    ], async (data) => {
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

  // ── Entity form ───────────────────────────────────────────
  function openEntityForm() {
    if (!currentFund) return;
    openModal('Add Entity', [
      {name: 'role', label: 'Role', type: 'select', required: true, options: [
        {value: 'manager', label: 'Investment Manager'},
        {value: 'trustee', label: 'Trustee'},
        {value: 'sponsor', label: 'Sponsor'},
        {value: 'custodian', label: 'Custodian'},
        {value: 'statutory_auditor', label: 'Statutory Auditor'},
        {value: 'legal_counsel', label: 'Legal Counsel'},
        {value: 'registrar', label: 'Registrar & Transfer Agent'},
      ]},
      {name: 'name', label: 'Entity Name', required: true, placeholder: 'e.g., Axis Trustee Services Ltd.'},
      {name: 'contact_person', label: 'Contact Person', placeholder: 'Name'},
      {name: 'email', label: 'Email', type: 'email', placeholder: 'email@example.com'},
      {name: 'phone', label: 'Phone', placeholder: '+91-XXXXX-XXXXX'},
      {name: 'sebi_registration', label: 'SEBI Registration', placeholder: 'If applicable'},
    ], async (data) => {
      await Auth.apiPost(`/funds/${currentFund.id}/entities/`, data);
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
