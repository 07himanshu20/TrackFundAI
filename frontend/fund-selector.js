/**
 * fund-selector.js — Dynamic fund selector + period selector for TrackFundAI navbar.
 *
 * Injects a fund dropdown + period dropdown + refresh button into any element
 * with id="fund-selector-mount" in the navbar.
 *
 * On fund/period change, fires a custom event "tfai:context-change" with detail:
 *   { fundId, fundName, period, periodLabel }
 *
 * All module pages listen for this event and re-fetch their data accordingly.
 *
 * KPI cards update in <1.5s on fund switch (single re-fetch, no full page reload).
 */

const FundSelector = (() => {
  const API_BASE = (window.APP_CONFIG && window.APP_CONFIG.API_BASE) || '/api';

  // Persisted context across navigation
  const STORAGE_KEY_FUND   = 'tfai_selected_fund_id';
  const STORAGE_KEY_FUND_NAME = 'tfai_selected_fund_name';
  const STORAGE_KEY_PERIOD = 'tfai_selected_period';

  // Quarter periods covering FY24 and FY25
  const PERIODS = [
    { value: 'fy25_q4', label: 'FY25 Q4 (Jan–Mar 2025)' },
    { value: 'fy25_q3', label: 'FY25 Q3 (Oct–Dec 2024)' },
    { value: 'fy25_q2', label: 'FY25 Q2 (Jul–Sep 2024)' },
    { value: 'fy25_q1', label: 'FY25 Q1 (Apr–Jun 2024)' },
    { value: 'fy24_q4', label: 'FY24 Q4 (Jan–Mar 2024)' },
    { value: 'fy24_q3', label: 'FY24 Q3 (Oct–Dec 2023)' },
    { value: 'fy24_q2', label: 'FY24 Q2 (Jul–Sep 2023)' },
    { value: 'fy24_q1', label: 'FY24 Q1 (Apr–Jun 2023)' },
    { value: 'all',     label: 'All Time' },
  ];

  // Period → date range mapping for API calls
  const PERIOD_DATES = {
    fy25_q4: { start: '2025-01-01', end: '2025-03-31' },
    fy25_q3: { start: '2024-10-01', end: '2024-12-31' },
    fy25_q2: { start: '2024-07-01', end: '2024-09-30' },
    fy25_q1: { start: '2024-04-01', end: '2024-06-30' },
    fy24_q4: { start: '2024-01-01', end: '2024-03-31' },
    fy24_q3: { start: '2023-10-01', end: '2023-12-31' },
    fy24_q2: { start: '2023-07-01', end: '2023-09-30' },
    fy24_q1: { start: '2023-04-01', end: '2023-06-30' },
    all:     { start: null, end: null },
  };

  let _funds = [];
  let _currentFundId = localStorage.getItem(STORAGE_KEY_FUND) || '';
  let _currentPeriod = localStorage.getItem(STORAGE_KEY_PERIOD) || 'all';

  async function _loadFunds() {
    const token = localStorage.getItem('tfai_access') || localStorage.getItem('access_token');
    if (!token) return [];
    try {
      const resp = await fetch(`${API_BASE}/funds/`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!resp.ok) return [];
      const data = await resp.json();
      // data may be array or { results: [...] }
      return Array.isArray(data) ? data : (data.results || []);
    } catch {
      return [];
    }
  }

  function _dispatch() {
    const fund = _funds.find(f => String(f.id) === String(_currentFundId));
    const periodObj = PERIODS.find(p => p.value === _currentPeriod) || PERIODS[PERIODS.length - 1];
    const dateRange = PERIOD_DATES[_currentPeriod] || PERIOD_DATES.all;

    const event = new CustomEvent('tfai:context-change', {
      detail: {
        fundId:      _currentFundId || null,
        fundName:    fund ? fund.name : 'All Funds',
        period:      _currentPeriod,
        periodLabel: periodObj.label,
        dateStart:   dateRange.start,
        dateEnd:     dateRange.end,
      },
      bubbles: true,
    });
    document.dispatchEvent(event);
  }

  async function mount(mountId) {
    const mountEl = document.getElementById(mountId || 'fund-selector-mount');
    if (!mountEl) return;

    _funds = await _loadFunds();

    // Build HTML
    mountEl.innerHTML = `
      <div class="fund-selector-wrap">
        <select id="tfai-fund-select" class="fund-selector" title="Select Fund">
          <option value="">All Funds</option>
          ${_funds.map(f => `<option value="${f.id}">${f.name}</option>`).join('')}
        </select>
        <select id="tfai-period-select" class="period-selector" title="Select Period">
          ${PERIODS.map(p => `<option value="${p.value}">${p.label}</option>`).join('')}
        </select>
        <button id="tfai-refresh-btn" class="refresh-btn" title="Refresh data">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="23 4 23 10 17 10"/>
            <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
          </svg>
        </button>
      </div>
    `;

    const fundSel   = document.getElementById('tfai-fund-select');
    const periodSel = document.getElementById('tfai-period-select');
    const refreshBtn = document.getElementById('tfai-refresh-btn');

    // Restore saved values
    if (_currentFundId) {
      fundSel.value = _currentFundId;
      // Ensure fund name is also persisted for chatbot widget
      const selectedOpt = fundSel.options[fundSel.selectedIndex];
      if (selectedOpt && selectedOpt.value) {
        localStorage.setItem(STORAGE_KEY_FUND_NAME, selectedOpt.text);
      }
    }
    if (_currentPeriod) periodSel.value = _currentPeriod;

    fundSel.addEventListener('change', () => {
      _currentFundId = fundSel.value;
      localStorage.setItem(STORAGE_KEY_FUND, _currentFundId);
      // Also persist fund name so chatbot widget can read it
      const selectedOpt = fundSel.options[fundSel.selectedIndex];
      localStorage.setItem(STORAGE_KEY_FUND_NAME, selectedOpt ? selectedOpt.text : '');
      _dispatch();
    });

    periodSel.addEventListener('change', () => {
      _currentPeriod = periodSel.value;
      localStorage.setItem(STORAGE_KEY_PERIOD, _currentPeriod);
      _dispatch();
    });

    refreshBtn.addEventListener('click', () => {
      refreshBtn.classList.add('spinning');
      _dispatch();
      setTimeout(() => refreshBtn.classList.remove('spinning'), 700);
    });

    // Dispatch initial context so the page can load with the right fund/period
    _dispatch();
  }

  function getContext() {
    const fund = _funds.find(f => String(f.id) === String(_currentFundId));
    const dateRange = PERIOD_DATES[_currentPeriod] || PERIOD_DATES.all;
    return {
      fundId:    _currentFundId || null,
      fundName:  fund ? fund.name : 'All Funds',
      period:    _currentPeriod,
      dateStart: dateRange.start,
      dateEnd:   dateRange.end,
    };
  }

  return { mount, getContext, PERIOD_DATES };
})();

window.FundSelector = FundSelector;
