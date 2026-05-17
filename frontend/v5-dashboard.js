/* ============================================================
   v5-dashboard.js  —  TrackFundAI SPA Controller
   Handles page switching, data loading, sector bars, mini charts,
   and AI chatbot for the v5 single-page dashboard.
============================================================ */
'use strict';

/* ── Global helpers ─────────────────────────────────────────── */
function showToast(msg, type) { if (typeof Toast !== 'undefined') Toast.show(msg, type); }

/* ── Fund Context State ─────────────────────────────────────── */
// _ctx is the single source of truth for current fund/period selection.
// All loaders read _ctx.fundId and _ctx.schemeIds to filter API calls.
let _ctx = {
  fundId:       null,   // selected fund UUID or null (= all funds)
  fundName:     'All Funds',
  corpusTarget: null,   // fund.corpus_target in Cr (null = All Funds or unknown)
  period:       'all',
  dateStart:    null,
  dateEnd:      null,
  schemeIds:    [],     // resolved: scheme UUIDs belonging to fundId
};

// Cache: fund id → array of scheme UUIDs
const _schemeCache = {};

// Cache: scheme id → array of investment objects (with cost + FV)
const _invCache = {};

/* ── Page render flags (reset on fund change) ────────────────── */
const _pageRendered  = {};
const _subRendered   = {};
/* ── Tracks the last-active sub-tab per page so fund-switch can reload it ── */
const _activeSubTab  = {};

/* ── Portfolio data cache for sector/geo bars ────────────────── */
let _portfolioData = null;

/* ── Helpers ───────────────────────────────────────────────────  */
const $ = id => document.getElementById(id);
const esc = s => { const d = document.createElement('div'); d.textContent = String(s ?? ''); return d.innerHTML; };
const fmt    = (n, dec=1) => n == null ? '—' : Number(n).toLocaleString('en-IN', {minimumFractionDigits:dec,maximumFractionDigits:dec});
const fmtCr  = n => n == null ? '—' : '₹' + fmt(n) + ' Cr';
const fmtPct = n => n == null ? '—' : Number(n).toFixed(1) + '%';
const fmtX   = n => n == null ? '—' : Number(n).toFixed(2) + 'x';

/* ── Scheme resolver ───────────────────────────────────────────  */
// Returns array of scheme IDs for the given fund (cached).
// If fundId is null/empty → returns [] meaning "all schemes" (no filter).
async function resolveSchemeIds(fundId) {
  if (!fundId) return [];
  if (_schemeCache[fundId]) return _schemeCache[fundId];
  try {
    const data = await Auth.apiGet(`/funds/${fundId}/schemes/`);
    const schemes = Array.isArray(data) ? data : (data.results || []);
    const ids = schemes.map(s => s.id);
    _schemeCache[fundId] = ids;
    return ids;
  } catch(e) {
    console.warn('Could not resolve schemes for fund', fundId, e);
    return [];
  }
}

// Build query string with scheme filter(s). Returns '' if no filter.
function schemeQS(schemeIds) {
  if (!schemeIds || !schemeIds.length) return '';
  return schemeIds.map(id => `scheme=${id}`).join('&');
}

// Get investments for all schemes in current context (cached).
// Returns flat array of investment objects.
async function getInvestmentsForContext(schemeIds) {
  if (!schemeIds || !schemeIds.length) {
    // All funds: fetch all schemes first
    try {
      const fundsData = await Auth.apiGet('/funds/');
      const funds = Array.isArray(fundsData) ? fundsData : (fundsData.results || []);
      // Limit to first fund if too many (performance)
      const allIds = [];
      for (const f of funds) {
        const ids = await resolveSchemeIds(f.id);
        allIds.push(...ids);
      }
      return await _fetchInvestmentsForSchemes(allIds);
    } catch(e) { return []; }
  }
  return await _fetchInvestmentsForSchemes(schemeIds);
}

async function _fetchInvestmentsForSchemes(schemeIds) {
  const all = [];
  for (const sid of schemeIds) {
    if (_invCache[sid]) { all.push(..._invCache[sid]); continue; }
    try {
      const data = await Auth.apiGet(`/schemes/${sid}/investments/`);
      const items = Array.isArray(data) ? data : (data.results || []);
      _invCache[sid] = items;
      all.push(...items);
    } catch(e) {}
  }
  return all;
}

/* ── Sidebar dynamic sub-tabs ─────────────────────────────── */
const SIDEBAR_SUBTABS = {
  overview: [],
  portfolio: [
    { icon: '\u{1F4CA}', label: 'Overview', sub: 'overview' },
    { icon: '\u{1F3E2}', label: 'Companies', sub: 'companies' },
    { icon: '\u{1F525}', label: 'Burn & Runway', sub: 'burn' },
    { icon: '\u{1F6AA}', label: 'Exits', sub: 'exits' },
    { icon: '\u{1F4C9}', label: 'KPIs', sub: 'kpis' },
    { icon: '\u26A1', label: 'SaaS Metrics', sub: 'saas' },
    { icon: '\u{1F3E0}', label: 'Quoted & Unquoted', sub: 'quoted' },
    { icon: '\u{1F4BC}', label: 'Investments', sub: 'inv-detail' },
    { icon: '\u{1F4CA}', label: 'Valuations', sub: 'val-tab' },
    { icon: '\u{1F4CB}', label: 'KPI Tracking', sub: 'kpi-tracking' },
    { icon: '\u{1F680}', label: 'Exit Scenarios', sub: 'exit-scenarios' },
    { icon: '\u{1F3DB}', label: 'Board Meetings', sub: 'board-meetings' },
  ],
  accounting: [
    { icon: '\u{1F4C8}', label: 'NAV & Unit Value', sub: 'nav' },
    { icon: '\u{1F4B5}', label: 'Waterfall', sub: 'waterfall' },
    { icon: '\u{1F4CB}', label: 'Capital Calls', sub: 'calls' },
    { icon: '\u{1F4B8}', label: 'Distributions', sub: 'dist' },
    { icon: '\u{1F4CC}', label: 'Fund P&L', sub: 'fpl' },
    { icon: '\u{1F4F1}', label: 'NAV Records', sub: 'navrecords' },
    { icon: '\u{1F4B0}', label: 'Carried Interest', sub: 'carried' },
    { icon: '\u{1F4DC}', label: 'Fund Ledger', sub: 'ledger' },
    { icon: '\u{1F4BC}', label: 'Management Fees', sub: 'fees' },
    { icon: '\u{1F4C1}', label: 'Chart of Accounts', sub: 'coa' },
    { icon: '\u2696', label: 'Trial Balance', sub: 'tb' },
    { icon: '\u{1F4CA}', label: 'Financial Statements', sub: 'finstat' },
  ],
  financials: [
    { icon: '\u{1F4C8}', label: 'P&L', sub: 'pl' },
    { icon: '\u{1F4CC}', label: 'Budget vs Actual', sub: 'bva' },
    { icon: '\u{1F4F0}', label: 'Consolidated', sub: 'consolidated' },
  ],
  valuations: [
    { icon: '\u{1F4CA}', label: 'Summary', sub: 'summary' },
    { icon: '\u2699', label: 'Methodology', sub: 'method' },
    { icon: '\u{1F4C8}', label: 'Value Bridge', sub: 'bridge' },
  ],
  investors: [
    { icon: '\u{1F91D}', label: 'LP Register', sub: 'register' },
    { icon: '\u{1F5A5}', label: 'Capital Accounts', sub: 'capital' },
    { icon: '\u2705', label: 'KYC/FATCA', sub: 'kyc' },
  ],
  compliance: [
    { icon: '\u{1F4CA}', label: 'Dashboard', sub: 'dashboard' },
    { icon: '\u{1F58C}', label: 'SEBI', sub: 'sebi' },
    { icon: '\u26A0', label: 'Alerts', sub: 'alerts' },
    { icon: '\u{1F4C5}', label: 'Calendar', sub: 'calendar' },
  ],
  benchmarks: [],
  market: [],
  analytics: [
    { icon: '\u{1F916}', label: 'AI Chatbot', sub: 'chatbot' },
    { icon: '\u{1F4A1}', label: 'AI Insights', sub: 'insights' },
    { icon: '\u{1F4C9}', label: 'Risk Monitor', sub: 'risk' },
    { icon: '\u{1F4F0}', label: 'MIS Reports', sub: 'mis' },
    { icon: '\u2713', label: 'Audit Log', sub: 'audit' },
    { icon: '\u{1F52E}', label: 'Predictions', sub: 'predict' },
  ],
  icworkflow: [],
};

// Page display names for the sidebar label
const SIDEBAR_PAGE_LABELS = {
  overview: 'Dashboard',
  portfolio: 'Portfolio',
  accounting: 'Fund Accounting',
  financials: 'Financials',
  valuations: 'Valuations',
  investors: 'Investors',
  compliance: 'Compliance',
  benchmarks: 'Benchmarks',
  market: 'Market Research',
  analytics: 'AI Analytics',
  icworkflow: 'IC Workflow',
};

let _currentPage = 'overview';

function updateSidebarSubtabs(pageId) {
  _currentPage = pageId;
  const container = $('sidebar-dynamic');
  if (!container) return;

  const tabs = SIDEBAR_SUBTABS[pageId] || [];
  if (!tabs.length) {
    container.innerHTML = '';
    return;
  }

  const label = SIDEBAR_PAGE_LABELS[pageId] || pageId;
  const activeSub = _activeSubTab[pageId] || tabs[0].sub;

  container.innerHTML =
    `<div class="v5-sidebar-label">${esc(label)}</div>` +
    tabs.map(t => {
      const isActive = t.sub === activeSub ? ' active' : '';
      return `<div class="v5-sidebar-item sidebar-subtab-item${isActive}" data-page="${pageId}" data-sub="${t.sub}" onclick="onSidebarSubClick(this,'${pageId}','${t.sub}')">` +
        `<span class="s-icon">${t.icon}</span>${esc(t.label)}` +
        `</div>`;
    }).join('');
}

function onSidebarSubClick(el, page, sub) {
  // Highlight clicked item in dynamic section
  const container = $('sidebar-dynamic');
  if (container) {
    container.querySelectorAll('.sidebar-subtab-item').forEach(i => i.classList.remove('active'));
  }
  if (el) el.classList.add('active');

  // Trigger the sub-tab switch
  showSub(page, sub);
}

/* ── Page routing ──────────────────────────────────────────── */
function showPage(id, btn) {
  document.querySelectorAll('.v5-page').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.v5-nav-tab').forEach(el => el.classList.remove('active'));

  const pg = $('pg-' + id);
  if (pg) pg.classList.add('active');

  if (btn) {
    btn.classList.add('active');
  } else {
    const tabs = [...document.querySelectorAll('.v5-nav-tab')];
    const found = tabs.find(t => (t.getAttribute('onclick') || '').includes("'" + id + "'"));
    if (found) found.classList.add('active');
  }

  // Update sidebar sub-tabs for the active page
  updateSidebarSubtabs(id);

  if (!_pageRendered[id]) {
    _pageRendered[id] = true;
    lazyRender(id);
  }
}

function showSub(page, sub) {
  const pageEl = $('pg-' + page);
  if (!pageEl) return;
  pageEl.querySelectorAll('.v5-subpane').forEach(p => p.classList.remove('active'));

  const prefixes = [page + '-', page.slice(0,3) + '-', 'pt-', 'acc-', 'fin-', 'val-', 'inv-', 'comp-', 'ai-'];
  let found = null;
  for (const pfx of prefixes) {
    found = pageEl.querySelector('#' + pfx + sub);
    if (found) break;
  }
  if (!found) found = pageEl.querySelector('#' + sub);
  if (found) found.classList.add('active');

  const bar = pageEl.querySelector('.v5-subtab-bar');
  if (bar) {
    bar.querySelectorAll('.v5-subtab').forEach(t => t.classList.remove('active'));
    const at = [...bar.querySelectorAll('.v5-subtab')].find(t =>
      (t.getAttribute('onclick') || '').includes("'" + sub + "'")
    );
    if (at) at.classList.add('active');
  }

  // Always track the last-clicked sub-tab so fund-switch can reload it
  _activeSubTab[page] = sub;

  // Sync sidebar dynamic sub-tab highlight
  const sidebarDyn = $('sidebar-dynamic');
  if (sidebarDyn) {
    sidebarDyn.querySelectorAll('.sidebar-subtab-item').forEach(i => {
      i.classList.toggle('active', i.dataset.sub === sub && i.dataset.page === page);
    });
  }

  const key = page + '-' + sub;
  if (!_subRendered[key]) {
    _subRendered[key] = true;
    lazyRenderSub(key);
  }
}

function sideActive(el) {
  // Clear active on static sidebar items only (not dynamic sub-tab items)
  document.querySelectorAll('.v5-sidebar-item:not(.sidebar-subtab-item)').forEach(i => i.classList.remove('active'));
  if (el) el.classList.add('active');
}

function switchInnerTab(btn, targetId) {
  const panel = btn.closest('.v5-panel-body, .v5-panel') || btn.parentElement.parentElement;
  panel.querySelectorAll('.v5-tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  panel.querySelectorAll('.v5-tab-content').forEach(tc => tc.classList.remove('active'));
  const tc = $(targetId);
  if (tc) tc.classList.add('active');
}

/* ── Lazy render map ───────────────────────────────────────── */
function lazyRender(id) {
  const map = {
    overview:    loadOverview,
    portfolio:   loadPortfolioOverview,
    accounting:  loadAccountingNAV,
    financials:  loadFinancials,
    valuations:  loadValuations,
    investors:   loadInvestors,
    compliance:  loadCompliance,
    benchmarks:  renderBenchmarks,
    market:      renderMarket,
    analytics:   renderAnalytics,
    icworkflow:  loadICWorkflow,
    mis:         loadMIS,
  };
  if (map[id]) map[id]();
}

function lazyRenderSub(key) {
  const map = {
    'portfolio-companies':      loadFullPortfolio,
    'portfolio-burn':           loadBurnRunway,
    'portfolio-exits':          loadExits,
    'portfolio-kpis':           loadKPIs,
    'portfolio-saas':           loadSaasMetrics,
    'portfolio-quoted':         loadQuotedUnquoted,
    'portfolio-inv-detail':     loadPortfolioInvestments,
    'portfolio-val-tab':        loadPortfolioValuations,
    'portfolio-kpi-tracking':   loadPortfolioKPITracking,
    'portfolio-exit-scenarios': loadPortfolioExitScenarios,
    'portfolio-board-meetings': loadPortfolioBoardMeetings,
    'accounting-waterfall':  renderWaterfall,
    'accounting-calls':      loadCapitalCalls,
    'accounting-dist':       loadDistributions,
    'accounting-fpl':        loadFundPL,
    'accounting-navrecords': loadAccNAVRecords,
    'accounting-carried':    loadAccCarried,
    'accounting-ledger':     loadAccLedger,
    'accounting-fees':       loadAccFees,
    'accounting-coa':        loadAccCOA,
    'accounting-tb':         loadAccTrialBalanceUI,
    'accounting-finstat':    loadAccFinStatementsUI,
    'financials-bva':      loadBvA,
    'financials-consolidated': loadConsolidated,
    'valuations-method':   renderValMethod,
    'valuations-bridge':   renderValBridge,
    'investors-capital':   loadLPCapital,
    'investors-kyc':       loadLPKYC,
    'compliance-sebi':     loadSEBI,
    'compliance-alerts':   loadCompAlerts,
    'compliance-calendar': loadCompCalendar,
    'analytics-insights':  loadAIInsights,
    'analytics-risk':      loadRiskMonitor,
    'analytics-mis':       loadMISReports,
    'analytics-audit':     loadAuditLog,
    'analytics-predict':   loadPredictions,
  };
  if (map[key]) map[key]();
}

/* ── Fund Context Change Handler ───────────────────────────── */
// Called when FundSelector fires tfai:context-change event.
// Resolves scheme IDs for the selected fund, then refreshes all pages.
async function onContextChange(detail) {
  const newFundId = detail.fundId || null;
  const sameCtx   = (newFundId === _ctx.fundId && detail.period === _ctx.period);

  // Update context
  _ctx = {
    fundId:       newFundId,
    fundName:     detail.fundName     || 'All Funds',
    corpusTarget: detail.corpusTarget || null,
    period:       detail.period       || 'all',
    dateStart:    detail.dateStart    || null,
    dateEnd:      detail.dateEnd      || null,
    schemeIds:    [],
  };

  // Persist fund context to localStorage + dispatch event so chatbot widget
  // (and any other listener) always knows the currently selected fund.
  localStorage.setItem('tfai_selected_fund_id', newFundId || '');
  localStorage.setItem('tfai_selected_fund_name', _ctx.fundName);
  document.dispatchEvent(new CustomEvent('tfai:context-change', {
    detail: {
      fundId:   newFundId,
      fundName: _ctx.fundName,
      period:   _ctx.period,
    },
    bubbles: true,
  }));

  // Resolve scheme IDs for new fund
  if (newFundId) {
    _ctx.schemeIds = await resolveSchemeIds(newFundId);
  }

  // Clear all render flags and investment cache for fresh load
  Object.keys(_pageRendered).forEach(k => delete _pageRendered[k]);
  Object.keys(_subRendered).forEach(k => delete _subRendered[k]);
  Object.keys(_invCache).forEach(k => delete _invCache[k]);
  _portfolioData = null;
  _invDetailRows = [];
  // Reset accounting extended tab state so schemes repopulate for new fund
  _accSchemesLoaded = false;
  _accNav = []; _accCarry = []; _accLedger = []; _accFees = []; _accCOA = [];

  // Re-render the currently active page + its active sub-tab
  const active = document.querySelector('.v5-page.active');
  if (active) {
    const id = active.id.replace('pg-','');
    _pageRendered[id] = true;
    lazyRender(id);
    // Also reload whichever sub-tab was last active in this page.
    // lazyRender only fires the page-level loader (e.g. loadPortfolioOverview).
    // Without this, sub-tab bodies keep showing the previous fund's data until
    // the user manually clicks the sub-tab again.
    if (_activeSubTab[id]) {
      const subKey = id + '-' + _activeSubTab[id];
      _subRendered[subKey] = true;   // mark as rendered so showSub won't double-fire
      lazyRenderSub(subKey);
    }
  } else {
    _pageRendered['overview'] = true;
    loadOverview();
  }
}

/* ── Fund selector (drives the <select> in the navbar) ─────── */
async function loadFundList() {
  try {
    const funds = await Auth.apiGet('/funds/');
    const arr = Array.isArray(funds) ? funds : (funds.results || []);

    // Populate the navbar <select> if it exists (index.html pattern)
    const sel = $('fund-selector-nav');
    if (sel) {
      const prev = sel.value;
      sel.innerHTML = '<option value="">All Funds</option>' +
        arr.map(f => `<option value="${f.id}">${f.name}</option>`).join('');
      // Restore previously selected fund or pick first
      if (prev && arr.find(f => f.id === prev)) {
        sel.value = prev;
      } else if (arr.length) {
        sel.value = arr[0].id;
      }
      // Trigger context change for the selected fund
      await _triggerFundContextFromSelect(sel.value, arr);
    }

    // If FundSelector component is mounted (other pages), it handles itself.
    // Mount it if the mount point exists.
    const mountEl = $('fund-selector-mount');
    if (mountEl && typeof FundSelector !== 'undefined') {
      await FundSelector.mount('fund-selector-mount');
      // FundSelector fires tfai:context-change automatically on mount
    }
  } catch(e) {
    console.warn('Fund list error:', e);
  }
}

// Triggered when user picks a fund from the navbar <select>
async function onFundChange(val) {
  try {
    const funds = await Auth.apiGet('/funds/');
    const arr = Array.isArray(funds) ? funds : (funds.results || []);
    await _triggerFundContextFromSelect(val, arr);
  } catch(e) {
    // Fallback: trigger with empty fund list
    await _triggerFundContextFromSelect(val, []);
  }
}

async function _triggerFundContextFromSelect(fundId, funds) {
  const fund = funds.find(f => String(f.id) === String(fundId));
  await onContextChange({
    fundId:       fundId || null,
    fundName:     fund ? fund.name : 'All Funds',
    corpusTarget: fund ? parseFloat(fund.corpus_target || 0) || null : null,
    period:       'all',
    dateStart:    null,
    dateEnd:      null,
  });
}

/* ── Overview ──────────────────────────────────────────────── */
async function loadOverview() {
  // Show a fast company count immediately while the heavy calls run
  if ($('kt-cos')) $('kt-cos').textContent = '…';
  try {
    const schemeIds = _ctx.schemeIds;

    // Build NAV and capital-call query strings
    const qs     = schemeQS(schemeIds);
    const navUrl  = '/accounting/nav/'    + (qs ? '?' + qs : '');
    const callUrl = '/lp/capital-calls/'  + (qs ? '?' + qs : '');
    const distUrl = '/lp/distributions/'  + (qs ? '?' + qs : '');

    const cosUrl = '/portfolio-companies/' + (_ctx.fundId ? `?fund=${_ctx.fundId}` : '');
    const [companies, navData, callsData, distsData, invData] = await Promise.allSettled([
      Auth.apiGet(cosUrl),
      Auth.apiGet(navUrl),
      Auth.apiGet(callUrl),
      Auth.apiGet(distUrl),
      getInvestmentsForContext(schemeIds),
    ]);

    const cos   = (companies.value?.results  || companies.value  || []);
    const navs  = (navData.value?.results    || navData.value    || []);
    const calls = (callsData.value?.results  || callsData.value  || []);
    const dists = (distsData.value?.results  || distsData.value  || []);
    const invs  = Array.isArray(invData.value) ? invData.value : [];

    // ── KPI computation from actual investments ──
    const active   = cos.filter(c => c.is_active).length;
    const inactive = cos.length - active;
    const totalCos = cos.length;

    // Cost = sum of total_invested from investments (in Cr already)
    let totalCost = 0;
    invs.forEach(inv => { totalCost += parseFloat(inv.total_invested || 0); });

    // FV = sum of latest_valuation from investments
    let totalFV = 0;
    invs.forEach(inv => { totalFV += parseFloat(inv.latest_valuation || 0); });

    // Fall back to NAV if no investment data
    if (!totalFV && navs.length) {
      // NAV total_nav is in Cr
      const latestByScheme = {};
      navs.forEach(n => {
        const sid = n.scheme;
        if (!latestByScheme[sid] || n.nav_date > latestByScheme[sid].nav_date) {
          latestByScheme[sid] = n;
        }
      });
      Object.values(latestByScheme).forEach(n => { totalFV += parseFloat(n.total_nav || 0); });
    }

    // Fall back to capital calls for cost if no investment data
    if (!totalCost && calls.length) {
      calls.forEach(c => { totalCost += parseFloat(c.total_call_amount || 0); });
    }

    const moic = totalCost > 0 ? totalFV / totalCost : 0;

    // Total distributions
    let totalDist = 0;
    dists.forEach(d => { totalDist += parseFloat(d.total_net_amount || 0); });
    const dpi  = totalCost > 0 ? totalDist / totalCost : 0;
    const tvpi = totalCost > 0 ? (totalFV + totalDist) / totalCost : 0;

    // totalCost and totalFV from investments are already in Cr
    const costCr = totalCost;
    const fvCr   = totalFV;

    // Cost-weighted average IRR from per-investment irr_pct values
    let irrNum = 0, irrDen = 0;
    invs.forEach(inv => {
      if (inv.irr_pct != null) {
        const w = parseFloat(inv.total_invested || 0);
        irrNum += parseFloat(inv.irr_pct) * w;
        irrDen += w;
      }
    });
    let netIrr = irrDen > 0 ? irrNum / irrDen : null;

    // If no per-investment IRR data, fall back to fund-level Net IRR from BvA ConsolidatedMIS.
    // This value is extracted from the "Net IRR" row in the Budget vs Actual sheet during import.
    if (netIrr === null && _ctx.fundId) {
      try {
        const misIrr = await Auth.apiGet(`/mis/consolidated/?fund=${_ctx.fundId}&line_item=net_irr`);
        const irrRec = Array.isArray(misIrr)
          ? misIrr.find(r => r.line_item === 'net_irr')
          : (misIrr.results || []).find(r => r.line_item === 'net_irr');
        if (irrRec && irrRec.total_actual_inr != null) {
          netIrr = parseFloat(irrRec.total_actual_inr);
        }
      } catch (e) { /* silent — no IRR data available */ }
    }

    // Update KPI cards — show ACTIVE portfolio count as primary (matches Excel Portfolio Investments sheet)
    if ($('kv-cos'))  $('kv-cos').textContent  = active || '—';
    if ($('ks-cos'))  $('ks-cos').textContent  = inactive > 0 ? `+ ${inactive} Exited` : 'Active portfolio';
    if ($('kt-cos'))  $('kt-cos').textContent  = totalCos > 0 ? `${active} active in this fund` : '—';
    if ($('kv-fv'))   $('kv-fv').textContent   = fmtCr(fvCr);
    if ($('ks-fv'))   $('ks-fv').textContent   = `vs Cost ${fmtCr(costCr)} Cr`;
    if ($('kv-moic')) $('kv-moic').textContent = fmtX(moic);
    if ($('kv-tvpi')) $('kv-tvpi').textContent = fmtX(tvpi);
    if ($('kv-irr'))  $('kv-irr').textContent  = netIrr != null ? netIrr.toFixed(1) + '%' : '—';
    if ($('kv-dep'))  $('kv-dep').textContent  = fmtCr(costCr);
    const corpus = _ctx.corpusTarget;
    const depPct = corpus > 0 ? ((costCr / corpus) * 100).toFixed(1) + '% of ₹' + fmtCr(corpus) + ' Cr corpus' : '% of corpus';
    if ($('ks-dep'))  $('ks-dep').textContent  = depPct;

    // Subtitle
    const sub = $('ov-subtitle');
    if (sub) sub.textContent = `${active} Active Portfolio Companies` + (inactive > 0 ? ` · ${inactive} Exited` : '') + ` · ${_ctx.fundName} · All figures in ₹ Crore`;

    // Sidebar company count
    const sbCos = $('sb-cos');
    if (sbCos) sbCos.textContent = active;

    // Alert strip — fetch real anomaly alerts, hide if none
    const alertEl  = $('alert-strip');
    const alertMsg = $('alert-strip-msg');
    if (alertEl) {
      alertEl.style.display = 'none';
      const alertQS = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
      Auth.apiGet(`/mis/anomalies/${alertQS}`).then(data => {
        const alerts = Array.isArray(data) ? data : (data.results || []);
        if (!alerts.length) return;
        alertEl.style.display = 'flex';
        const high = alerts.filter(a => a.severity === 'high').length;
        if (alertMsg) alertMsg.textContent =
          `${alerts.length} active anomaly alert${alerts.length > 1 ? 's' : ''}` +
          (high ? ` · ${high} high severity` : '');
      }).catch(() => {});
    }

    // Sector bars — use investments for FV/cost, cos for count
    renderSectorBars(cos, invs, 'sector-bars', 'cos');

    // Performance metrics
    renderPerfMetrics({ moic, tvpi, dpi, totalFV: fvCr, totalCost: costCr, totalCos, active, netIrr });

    // NAV mini chart — navs are per-scheme, date = nav_date, value = total_nav (Cr)
    const sortedNavs = [...navs].sort((a,b) => (a.nav_date||'') < (b.nav_date||'') ? -1 : 1);
    // Aggregate by date if multiple schemes
    const navByDate = {};
    sortedNavs.forEach(n => {
      navByDate[n.nav_date] = (navByDate[n.nav_date] || 0) + parseFloat(n.total_nav || 0);
    });
    const navChartVals = Object.values(navByDate).slice(-6);
    renderMiniChart('nav-chart', navChartVals, '#2563eb');

    // Revenue sparkline: fetch real 6-month MIS rollup
    let revChartVals = [];
    if (_ctx.fundId) {
      try {
        const revRollup = await Auth.apiGet(`/mis/consolidated/6month/?fund=${_ctx.fundId}&line_items=revenue`);
        const revRows = revRollup?.data || [];
        revChartVals = revRows.map(r => parseFloat(r.revenue || 0)).filter(v => v > 0);
      } catch(e) {}
    }
    renderMiniChart('rev-chart', revChartVals, '#10b981');

    // Top portfolio — use investments for cost/FV, mapped by company
    renderTopPortfolio(cos, invs);

    // Stage / geo bars
    renderStageBars(cos, 'stage-bars');
    renderGeoBars(cos, 'geo-bars');

    // Capital calls timeline
    loadCapitalCallsTimeline();

    // Exits
    renderExitsList([]);

    // Scorecard
    renderScorecard({ moic, tvpi, dpi, totalCos, active, netIrr });

    _portfolioData = { cos, navs, invs };

  } catch(e) {
    console.error('Overview error:', e);
  }
}

function renderSectorBars(cos, invs, targetId, view='fv') {
  const el = $(targetId);
  if (!el) return;

  const byName = {};
  cos.forEach(c => {
    const s = c.sector || 'Other';
    if (!byName[s]) byName[s] = { cos: 0, fv: 0, cost: 0 };
    byName[s].cos++;
  });

  // Map investment data by portfolio_company or company_name
  invs.forEach(inv => {
    // investments have portfolio_company (UUID) and sector directly
    const s = inv.sector || 'Other';
    if (!byName[s]) byName[s] = { cos: 0, fv: 0, cost: 0 };
    byName[s].fv   += parseFloat(inv.latest_valuation  || 0);
    byName[s].cost += parseFloat(inv.total_invested     || 0);
  });

  const sorted = Object.entries(byName).sort((a,b) => b[1][view] - a[1][view]).slice(0,10);
  const max    = Math.max(...sorted.map(s => s[1][view]), 1);
  const colors = ['#2563eb','#10b981','#f59e0b','#8b5cf6','#06b6d4','#ef4444','#f97316','#14b8a6','#e879f9','#84cc16'];

  el.innerHTML = sorted.map(([name, d], i) => {
    const val = d[view];
    const pct = ((val / max) * 100).toFixed(0);
    let display;
    if (view === 'cos') display = d.cos + ' cos';
    else if (view === 'fv')   display = fmtCr(d.fv)   + ' Cr';
    else                      display = fmtCr(d.cost) + ' Cr';
    const color = colors[i % colors.length];
    const safeN = esc(name);
    const safeName = name.replace(/'/g, "\\'");
    return `
    <div class="v5-sector-bar v5-sector-bar-clickable"
         title="View ${safeN} companies"
         onclick="drillSector('${safeName}')">
      <div class="v5-sector-name">${safeN}</div>
      <div class="v5-sector-track">
        <div class="v5-sector-fill" style="width:${pct}%;background:${color}"></div>
      </div>
      <div class="v5-sector-pct">${pct}%</div>
      <div class="v5-sector-val" style="color:${color}">${display}</div>
      <div class="v5-sector-arrow">&#8594;</div>
    </div>`;
  }).join('');
}

/* ── Sector drill-down: navigate to Portfolio > Companies filtered by sector ── */
function drillSector(sectorName) {
  // Switch to Portfolio page
  showPage('portfolio', null);
  // Activate the sidebar item for portfolio
  const sideItems = [...document.querySelectorAll('.v5-sidebar-item')];
  const ptSide = sideItems.find(el => (el.getAttribute('onclick') || el.textContent).includes('portfolio'));
  if (ptSide) sideActive(ptSide);

  // Switch to the Companies sub-tab
  setTimeout(() => {
    showSub('portfolio', 'companies');
    // After the table renders, apply the sector filter
    setTimeout(() => {
      const sel = $('pt-sector-filter');
      if (sel) {
        // Find the matching option (case-insensitive)
        const opt = [...sel.options].find(o => o.value.toLowerCase() === sectorName.toLowerCase()
                                            || o.text.toLowerCase()  === sectorName.toLowerCase());
        if (opt) { sel.value = opt.value; }
        else      { sel.value = sectorName; }
        if (typeof filterPtTable === 'function') filterPtTable();
      }
      // Show a sector drill-down header
      _showSectorDrillHeader(sectorName);
    }, 300);
  }, 100);
}

/* Render a compact sector-drill banner above the portfolio table */
function _showSectorDrillHeader(sectorName) {
  let banner = $('sector-drill-banner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'sector-drill-banner';
    banner.style.cssText = [
      'display:flex;align-items:center;gap:12px;padding:10px 16px',
      'background:rgba(37,99,235,.1);border:1px solid rgba(37,99,235,.25)',
      'border-radius:8px;margin-bottom:12px;font-size:12px;color:var(--text)',
    ].join(';');
    const tableWrap = $('full-portfolio-tbody');
    if (tableWrap) tableWrap.closest('.v5-panel')?.querySelector('.v5-panel-body,table')
      ?.before(banner);
    else {
      const pg = $('pg-portfolio');
      if (pg) pg.prepend(banner);
    }
  }
  banner.innerHTML = `
    <span style="font-size:16px">&#128269;</span>
    <span>Showing companies in <strong style="color:var(--accent3)">${esc(sectorName)}</strong></span>
    <button onclick="_clearSectorDrill()" style="margin-left:auto;background:none;border:1px solid var(--border);
      color:var(--text2);padding:3px 10px;border-radius:6px;cursor:pointer;font-size:11px">
      &#10005; Clear filter
    </button>`;
  banner.style.display = 'flex';
}

function _clearSectorDrill() {
  const sel = $('pt-sector-filter');
  if (sel) { sel.value = ''; if (typeof filterPtTable === 'function') filterPtTable(); }
  const banner = $('sector-drill-banner');
  if (banner) banner.style.display = 'none';
}

function ovSecView(view, btn) {
  document.querySelectorAll('#sec-fv-btn,#sec-cost-btn,#sec-cos-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  if (_portfolioData) renderSectorBars(_portfolioData.cos, _portfolioData.invs, 'sector-bars', view);
}

function renderPerfMetrics(d) {
  const el = $('perf-metrics');
  if (!el) return;
  const items = [
    { label: 'Portfolio MOIC', value: fmtX(d.moic) },
    { label: 'Total FV (Cr)',  value: fmtCr(d.totalFV) },
    { label: 'Total Cost (Cr)',value: fmtCr(d.totalCost) },
    { label: 'Active Cos',     value: String(d.active) },
    { label: 'Total Cos',      value: String(d.totalCos) },
    { label: 'Net IRR',        value: d.netIrr != null ? d.netIrr.toFixed(1) + '%' : '—' },
  ];
  el.innerHTML = items.map(it => `
    <div class="v5-metric-item">
      <div class="v5-metric-label">${it.label}</div>
      <div class="v5-metric-value">${it.value}</div>
    </div>`).join('');
}

function renderMiniChart(targetId, values, color='#2563eb') {
  const el = $(targetId);
  if (!el || !values.length) return;
  const max = Math.max(...values, 1);
  el.innerHTML = values.map(v => {
    const h = Math.max(8, Math.round((v / max) * 100));
    return `<div class="v5-mini-bar" style="height:${h}%;background:${color};opacity:0.7"></div>`;
  }).join('');
}

function renderTopPortfolio(cos, invs) {
  const tbody = $('top-portfolio-tbody');
  if (!tbody) return;

  // Build cost/FV map by company_name from investments
  const costMap = {}, fvMap = {};
  invs.forEach(inv => {
    const n = inv.company_name || inv.portfolio_company_name || '';
    costMap[n] = (costMap[n] || 0) + parseFloat(inv.total_invested    || 0);
    fvMap[n]   = (fvMap[n]   || 0) + parseFloat(inv.latest_valuation  || 0);
  });

  // Sort by FV descending if available, else by name
  const sorted = [...cos].sort((a,b) => {
    const fa = fvMap[a.name] || 0, fb = fvMap[b.name] || 0;
    return fb - fa;
  }).slice(0, 10);

  tbody.innerHTML = sorted.map(c => {
    const cost = costMap[c.name] || 0;
    const fv   = fvMap[c.name]   || 0;
    const moic = cost > 0 ? (fv / cost).toFixed(2) + 'x' : '—';
    const statusLabel = c.is_active ? 'Active' : 'Inactive';
    const statusClass = c.is_active ? 'active' : 'exited';
    return `<tr>
      <td class="td-bold">${esc(c.name || '—')}</td>
      <td>${esc(c.sector || '—')}</td>
      <td class="td-right">${cost > 0 ? fmtCr(cost) : '—'}</td>
      <td class="td-right">${fv   > 0 ? fmtCr(fv)   : '—'}</td>
      <td class="td-right v5-text-green">${moic}</td>
      <td class="td-center"><span class="v5-status ${statusClass}">${statusLabel}</span></td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" class="table-empty">No data</td></tr>';
}

function renderStageBars(cos, targetId) {
  const el = $(targetId);
  if (!el) return;
  const stages = {};
  cos.forEach(c => { const s = c.sector || 'Unknown'; stages[s] = (stages[s]||0)+1; });
  renderBarList(el, stages, '#8b5cf6');
}

function renderGeoBars(cos, targetId) {
  const el = $(targetId);
  if (!el) return;
  const geos = {};
  cos.forEach(c => { const g = c.headquarters_city || c.headquarters_country || 'Unknown'; geos[g] = (geos[g]||0)+1; });
  renderBarList(el, geos, '#06b6d4');
}

function renderBarList(el, obj, color) {
  const sorted = Object.entries(obj).sort((a,b) => b[1]-a[1]).slice(0,8);
  const max = Math.max(...sorted.map(s=>s[1]), 1);
  el.innerHTML = sorted.map(([name, count]) => `
    <div class="v5-sector-bar">
      <div class="v5-sector-name">${esc(name)}</div>
      <div class="v5-sector-track"><div class="v5-sector-fill" style="width:${Math.round(count/max*100)}%;background:${color}"></div></div>
      <div class="v5-sector-val">${count}</div>
    </div>`).join('');
}

async function loadCapitalCallsTimeline() {
  const el = $('capital-calls-timeline');
  if (!el) return;
  try {
    const fqs  = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const data = await Auth.apiGet('/lp/capital-calls/' + fqs);
    const calls = (data.results || data || []).slice(0,6);
    if (!calls.length) { el.innerHTML = '<div style="color:var(--text3);font-size:11px">No capital calls for this fund.</div>'; return; }
    const dotColors = ['green','gold','','green','red','gold'];
    el.innerHTML = calls.map((c, i) => `
      <div class="v5-timeline-item">
        <div class="v5-timeline-dot ${dotColors[i%6]}"></div>
        <div class="v5-timeline-date">${c.call_date || '—'}</div>
        <div class="v5-timeline-text">${esc(c.scheme_name || '—')}</div>
        <div class="v5-timeline-amount">${fmtCr(parseFloat(c.total_call_amount||0))} Cr</div>
      </div>`).join('');
  } catch(e) {
    el.innerHTML = '<div style="color:var(--text3);font-size:11px">No capital calls data.</div>';
  }
}

function renderExitsList(invs) {
  const el = $('exits-list');
  if (!el) return;
  el.innerHTML = '<div style="color:var(--text3);font-size:11px">Exit data available in Fund Admin per-investment detail.</div>';
}

function renderScorecard(d) {
  const el = $('scorecard');
  if (!el) return;
  const items = [
    { label: 'MOIC',  value: fmtX(d.moic),  color: '#f59e0b' },
    { label: 'IRR',   value: d.netIrr != null ? d.netIrr.toFixed(1) + '%' : '—', color: '#8b5cf6' },
    { label: 'Active',value: String(d.active),color: '#10b981' },
    { label: 'Total', value: String(d.totalCos), color: '#2563eb' },
    { label: 'DPI',   value: fmtX(d.dpi),   color: '#06b6d4' },
    { label: 'TVPI',  value: fmtX(d.tvpi),  color: '#f97316' },
  ];
  el.innerHTML = items.map(it => `
    <div class="v5-gauge-item">
      <div class="v5-gauge-value" style="color:${it.color}">${it.value}</div>
      <div class="v5-gauge-label">${it.label}</div>
    </div>`).join('');
}

/* ── Portfolio ─────────────────────────────────────────────── */
async function loadPortfolioOverview() {
  try {
    const schemeIds = _ctx.schemeIds;

    // Load portfolio companies and investments in parallel
    // Pass fund filter so backend returns only companies for this fund
    const cosUrl = '/portfolio-companies/' + (_ctx.fundId ? `?fund=${_ctx.fundId}` : '');
    const [cosRes, invData] = await Promise.allSettled([
      Auth.apiGet(cosUrl),
      getInvestmentsForContext(schemeIds),
    ]);

    const cosArr = cosRes.value?.results || cosRes.value || [];
    const invs   = Array.isArray(invData.value) ? invData.value : [];

    // Backend already filters by fund — no client-side name-matching needed
    const filteredCos = cosArr;

    const elSub = $('pt-subtitle');
    if (elSub) elSub.textContent = `${filteredCos.length} companies across multiple sectors · ${_ctx.fundName}`;
    const sbCos = $('sb-cos');
    if (sbCos) sbCos.textContent = cosArr.length;

    // Cost and FV from investments (already in Cr)
    let totalCost = 0, totalFV = 0;
    invs.forEach(inv => {
      totalCost += parseFloat(inv.total_invested   || 0);
      totalFV   += parseFloat(inv.latest_valuation || 0);
    });

    const elCost = $('pt-cost');
    if (elCost) elCost.textContent = totalCost > 0 ? fmtCr(totalCost) : '—';
    const elCos = $('pt-cos');
    if (elCos) elCos.textContent = `${filteredCos.length} companies`;
    const elAvg = $('pt-avg-ticket');
    if (elAvg) elAvg.textContent = filteredCos.length && totalCost ? fmtCr(totalCost / filteredCos.length) : '—';
    const elGain = $('pt-gain');
    if (elGain) elGain.textContent = totalCost > 0 && totalFV > 0 ? fmtX(totalFV / totalCost) : '—';
    const elHold = $('pt-holding');
    Auth.apiGet('/portfolio/avg-holding/' + (_ctx.fundId ? `?fund=${_ctx.fundId}` : ''))
      .then(r => {
        if (elHold) elHold.textContent = r && r.avg_holding_years != null
          ? r.avg_holding_years.toFixed(1) + ' yrs'
          : '—';
      })
      .catch(() => { if (elHold) elHold.textContent = '—'; });

    renderSectorBars(filteredCos, invs, 'pt-sec-bars', 'fv');
    renderStageBars(filteredCos, 'pt-stage-bars');

    // Store for sub renders
    window._pt_cos     = filteredCos;
    window._pt_invs    = invs;
    window._pt_costMap  = {};
    window._pt_fvMap    = {};
    window._pt_stageMap = {};
    window._pt_irrMap   = {};
    invs.forEach(inv => {
      const n = inv.company_name || inv.portfolio_company_name || '';
      window._pt_costMap[n] = (window._pt_costMap[n] || 0) + parseFloat(inv.total_invested   || 0);
      window._pt_fvMap[n]   = (window._pt_fvMap[n]   || 0) + parseFloat(inv.latest_valuation || 0);
      if (inv.stage && !window._pt_stageMap[n]) window._pt_stageMap[n] = inv.stage;
      if (inv.irr_pct != null && window._pt_irrMap[n] == null) window._pt_irrMap[n] = parseFloat(inv.irr_pct);
    });
  } catch(e) {
    console.error('Portfolio overview error:', e);
  }
}

async function loadFullPortfolio() {
  const tbody = $('full-portfolio-tbody');
  if (!tbody) return;
  try {
    if (!window._pt_cos) await loadPortfolioOverview();
    const cos      = window._pt_cos     || [];
    const fvMap    = window._pt_fvMap    || {};
    const costMap  = window._pt_costMap  || {};
    const stageMap = window._pt_stageMap || {};
    const irrMap   = window._pt_irrMap   || {};

    // Populate Sector filter
    const sectorSel = $('pt-sector-filter');
    if (sectorSel) {
      const prevSector = sectorSel.value;
      const sectors = [...new Set(cos.map(c => c.sector).filter(Boolean))].sort();
      sectorSel.innerHTML = '<option value="">All Sectors</option>' +
        sectors.map(s => `<option value="${esc(s)}"${s === prevSector ? ' selected' : ''}>${esc(s)}</option>`).join('');
    }

    // Populate Stage filter from stageMap values
    const stageSel = $('pt-stage-filter');
    if (stageSel) {
      const prevStage = stageSel.value;
      const stages = [...new Set(Object.values(stageMap).filter(Boolean))].sort();
      stageSel.innerHTML = '<option value="">All Stages</option>' +
        stages.map(s => `<option value="${esc(s)}"${s === prevStage ? ' selected' : ''}>${esc(s)}</option>`).join('');
    }

    // Render rows — store exact sector/stage in data-attrs for precise filtering
    tbody.innerHTML = cos.map((c, idx) => {
      const cost  = costMap[c.name] || 0;
      const fv    = fvMap[c.name]   || 0;
      const stage = stageMap[c.name] || '';
      const moic  = cost > 0 ? (fv / cost).toFixed(2) + 'x' : '—';
      const statusLabel = c.is_active ? 'Active' : 'Inactive';
      const statusClass = c.is_active ? 'active' : 'exited';
      return `<tr data-sector="${esc(c.sector || '')}" data-stage="${esc(stage)}">
        <td class="td-center td-sno" style="color:var(--text3);font-size:11px;width:40px">${idx + 1}</td>
        <td class="td-bold">${esc(c.name || '—')}</td>
        <td>${esc(c.sector || '—')}</td>
        <td>${esc(stage || '—')}</td>
        <td>${esc(c.headquarters_city || '—')}</td>
        <td class="td-right">${cost > 0 ? fmtCr(cost) : '—'}</td>
        <td class="td-right">${fv   > 0 ? fmtCr(fv)   : '—'}</td>
        <td class="td-right">${irrMap[c.name] != null ? irrMap[c.name].toFixed(1) + '%' : '—'}</td>
        <td class="td-right v5-text-green">${moic}</td>
        <td class="td-center"><span class="v5-status ${statusClass}">${statusLabel}</span></td>
      </tr>`;
    }).join('') || '<tr><td colspan="10" class="table-empty">No companies found.</td></tr>';

    window._pt_all_rows = tbody.querySelectorAll('tr');
    _updatePtCount();
  } catch(e) {
    tbody.innerHTML = '<tr><td colspan="10" class="table-empty">Error loading data.</td></tr>';
  }
}

function filterPtTable() {
  const q      = (($('pt-search')        || {}).value || '').toLowerCase();
  const sector = (($('pt-sector-filter') || {}).value || '');
  const stage  = (($('pt-stage-filter')  || {}).value || '');
  const rows   = $('full-portfolio-tbody') ? $('full-portfolio-tbody').querySelectorAll('tr') : [];
  let visibleIdx = 0;
  rows.forEach(tr => {
    // Use data-attrs for exact match; fall back to text search for the query string
    const trSector = tr.dataset.sector || '';
    const trStage  = tr.dataset.stage  || '';
    const text     = tr.textContent.toLowerCase();
    const match    = (!q      || text.includes(q)) &&
                     (!sector || trSector === sector) &&
                     (!stage  || trStage  === stage);
    tr.style.display = match ? '' : 'none';
    if (match) {
      const snoCell = tr.querySelector('.td-sno');
      if (snoCell) snoCell.textContent = ++visibleIdx;
    }
  });
  _updatePtCount();
}

function _updatePtCount() {
  const label = $('pt-count-label');
  if (!label) return;
  const sector = (($('pt-sector-filter') || {}).value || '');
  const stage  = (($('pt-stage-filter')  || {}).value || '');
  const rows   = $('full-portfolio-tbody') ? $('full-portfolio-tbody').querySelectorAll('tr') : [];
  const visible = [...rows].filter(tr => tr.style.display !== 'none' && !tr.querySelector('.table-empty'));
  const parts = [];
  if (sector) parts.push(sector);
  if (stage)  parts.push(stage);
  const suffix = parts.length ? ' · ' + parts.join(' · ') : ' · All';
  label.textContent = `${visible.length} ${visible.length === 1 ? 'company' : 'companies'}${suffix}`;
}

async function loadBurnRunway() {
  const tbody = $('burn-tbody');
  try {
    const qs = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const data = await Auth.apiGet(`/portfolio/burn-runway/${qs}`);
    const companies = data.companies || [];

    if ($('burn-gross'))  $('burn-gross').textContent  = data.avg_gross_burn != null ? fmtCr(data.avg_gross_burn) : '—';
    if ($('burn-net'))    $('burn-net').textContent    = data.avg_net_burn  != null ? fmtCr(data.avg_net_burn)  : '—';
    if ($('burn-runway')) $('burn-runway').textContent = data.avg_runway    != null ? `${data.avg_runway.toFixed(1)} mo` : '—';
    if ($('burn-cash'))   $('burn-cash').textContent   = data.total_cash    != null ? fmtCr(data.total_cash)   : '—';

    if (!tbody) return;
    if ($('burn-count')) $('burn-count').textContent = `(${companies.length} companies)`;
    tbody.innerHTML = companies.map((c, i) => {
      const rmo = c.runway_months;
      const riskLabel = rmo == null ? '—' : rmo < 6 ? '<span class="v5-status critical">High</span>' : rmo < 12 ? '<span class="v5-status attention">Watch</span>' : '<span class="v5-status active">Safe</span>';
      return `<tr data-search="${esc((c.company_name||'').toLowerCase())}">
        <td class="row-num td-center" style="color:var(--text3);font-size:12px">${i + 1}</td>
        <td class="td-bold">${esc(c.company_name)}</td>
        <td class="td-right">${c.gross_burn != null ? fmtCr(c.gross_burn) : '—'}</td>
        <td class="td-right">${c.net_burn != null ? fmtCr(c.net_burn) : '—'}</td>
        <td class="td-right">${c.cash_balance != null ? fmtCr(c.cash_balance) : '—'}</td>
        <td class="td-right">${rmo != null ? rmo.toFixed(1) + ' mo' : '—'}</td>
        <td class="td-center">${riskLabel}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="7" class="table-empty">No burn/runway data imported yet. Upload a file with a Burn &amp; Runway sheet.</td></tr>';
  } catch(e) {
    if ($('burn-gross'))  $('burn-gross').textContent  = '—';
    if ($('burn-net'))    $('burn-net').textContent    = '—';
    if ($('burn-runway')) $('burn-runway').textContent = '—';
    if ($('burn-cash'))   $('burn-cash').textContent   = '—';
    if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="table-empty">No burn/runway data.</td></tr>';
  }
}
function filterBurnRunway() { _filterTableRows('burn-tbody','burn-search',null,null,'burn-count','companies'); }

async function loadExits() {
  const tbody = $('exits-tbody');
  try {
    const qs = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const data = await Auth.apiGet(`/portfolio/exits/${qs}`);
    const exits = data.exits || [];
    const summary = data.summary || {};

    if ($('exit-realized')) $('exit-realized').textContent = summary.total_proceeds ? fmtCr(summary.total_proceeds) : '—';
    if ($('exit-moic'))     $('exit-moic').textContent     = summary.avg_moic  != null ? fmtX(summary.avg_moic)  : '—';
    // Prefer Net IRR (avg_net_irr) over Gross IRR for the summary KPI card
    const displayIrr = summary.avg_net_irr ?? summary.avg_irr;
    if ($('exit-irr'))      $('exit-irr').textContent      = displayIrr != null ? displayIrr.toFixed(1) + '%' : '—';
    if ($('exit-dpi'))      $('exit-dpi').textContent      = summary.dpi       != null ? summary.dpi.toFixed(2) + 'x' : '—';
    if ($('exit-count'))    $('exit-count').textContent    = summary.total_exits ?? '0';

    if (!tbody) return;
    if ($('exits-count')) $('exits-count').textContent = `(${exits.length} exits)`;
    // Populate sector filter
    const exitSectorSel = $('exits-sector-filter');
    if (exitSectorSel) {
      const sectors = [...new Set(exits.map(e => e.sector).filter(Boolean))].sort();
      const cur = exitSectorSel.value;
      exitSectorSel.innerHTML = '<option value="">All Sectors</option>' +
        sectors.map(s => `<option value="${esc(s)}"${s===cur?' selected':''}>${esc(s)}</option>`).join('');
    }
    tbody.innerHTML = exits.map((e, i) => {
      // Show Net IRR when available, Gross IRR otherwise
      const irrDisplay = e.net_irr_pct != null ? e.net_irr_pct.toFixed(1) + '%'
                       : e.irr_pct     != null ? e.irr_pct.toFixed(1) + '%'
                       : '—';
      return `<tr data-search="${esc((e.company_name+' '+(e.sector||'')+' '+(e.exit_type_display||'')).toLowerCase())}" data-sector="${esc(e.sector||'')}">
      <td class="row-num td-center" style="color:var(--text3);font-size:12px">${i + 1}</td>
      <td class="td-bold">${esc(e.company_name)}</td>
      <td>${esc(e.sector || '—')}</td>
      <td>${esc(e.exit_type_display || e.exit_type)}</td>
      <td class="td-right">${e.cost ? fmtCr(e.cost) : '—'}</td>
      <td class="td-right">${e.proceeds != null ? fmtCr(e.proceeds) : '—'}</td>
      <td class="td-right">${e.moic != null ? e.moic.toFixed(2) + 'x' : '—'}</td>
      <td class="td-right">${irrDisplay}</td>
      <td>${e.exit_date || '—'}</td>
    </tr>`;
    }).join('') || '<tr><td colspan="9" class="table-empty">No exits recorded for this fund yet.</td></tr>';
  } catch(e) {
    if ($('exit-count'))    $('exit-count').textContent    = '—';
    if ($('exit-realized')) $('exit-realized').textContent = '—';
    if ($('exit-moic'))     $('exit-moic').textContent     = '—';
    if ($('exit-irr'))      $('exit-irr').textContent      = '—';
    if ($('exit-dpi'))      $('exit-dpi').textContent      = '—';
    if (tbody) tbody.innerHTML = '<tr><td colspan="9" class="table-empty">No exits data.</td></tr>';
  }
}
function filterExits() { _filterTableRows('exits-tbody','exits-search','exits-sector-filter',null,'exits-count','exits'); }

async function loadKPIs() {
  const tbody = $('kpi-trend-tbody');
  try {
    const qs = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const data = await Auth.apiGet(`/portfolio/kpi-matrix/${qs}`);
    const companies = data.companies || [];

    if (!tbody) return;
    if ($('kpis-count')) $('kpis-count').textContent = `(${companies.length} companies)`;
    const _p = v => v != null ? Number(v).toFixed(1) + '%' : '—';
    const _n = v => v != null ? Number(v).toLocaleString('en-IN', {maximumFractionDigits: 2}) : '—';
    tbody.innerHTML = companies.map((c, i) => `<tr data-search="${esc((c.company_name||'').toLowerCase())}">
      <td class="row-num td-center" style="color:var(--text3);font-size:12px">${i + 1}</td>
      <td class="td-bold">${esc(c.company_name || '—')}</td>
      <td class="td-right">${c.gmv != null ? fmtCr(c.gmv) : '—'}</td>
      <td class="td-right">${c.revenue != null ? fmtCr(c.revenue) : '—'}</td>
      <td class="td-right">${_p(c.gross_m)}</td>
      <td class="td-right">${_p(c.ebitda)}</td>
      <td class="td-right">${_n(c.orders)}</td>
      <td class="td-right">${_n(c.aov)}</td>
      <td class="td-right">${_p(c.returns)}</td>
      <td class="td-right">${_n(c.cac)}</td>
      <td class="td-right">${_p(c.repeat)}</td>
      <td class="td-right">${c.cost != null ? fmtCr(c.cost) : '—'}</td>
      <td class="td-right">${c.fv != null ? fmtCr(c.fv) : '—'}</td>
    </tr>`).join('') || '<tr><td colspan="13" class="table-empty">No KPI data imported yet. Upload a file with a Portfolio KPIs sheet.</td></tr>';
  } catch(e) {
    if (tbody) tbody.innerHTML = '<tr><td colspan="13" class="table-empty">No KPI data.</td></tr>';
  }
}
function filterKPIs() { _filterTableRows('kpi-trend-tbody','kpis-search',null,null,'kpis-count','companies'); }

async function loadSaasMetrics() {
  const tbody = $('saas-tbody');
  try {
    const qs = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const data = await Auth.apiGet(`/portfolio/saas-metrics/${qs}`);
    const companies = data.companies || [];

    // Summary cards — aggregate across portfolio
    let totalMRR = 0, totalARR = 0, churnSum = 0, nrrSum = 0, churnN = 0, nrrN = 0;
    companies.forEach(c => {
      if (c.mrr != null) totalMRR += c.mrr;
      if (c.arr != null) totalARR += c.arr;
      if (c.churn_rate != null) { churnSum += c.churn_rate; churnN++; }
      if (c.nrr != null) { nrrSum += c.nrr; nrrN++; }
    });

    if ($('saas-mrr'))   $('saas-mrr').textContent   = totalMRR  ? fmtCr(totalMRR)  : '—';
    if ($('saas-arr'))   $('saas-arr').textContent   = totalARR  ? fmtCr(totalARR)  : '—';
    if ($('saas-churn')) $('saas-churn').textContent = churnN    ? (churnSum / churnN).toFixed(1) + '%' : '—';
    if ($('saas-nrr'))   $('saas-nrr').textContent   = nrrN      ? (nrrSum / nrrN).toFixed(1) + '%' : '—';

    if (!tbody) return;
    if ($('saas-count')) $('saas-count').textContent = `(${companies.length} companies)`;
    // Populate sector filter for SaaS table
    const saasSectorSel = $('saas-sector-filter');
    if (saasSectorSel) {
      const sectors = [...new Set(companies.map(c => c.sector).filter(Boolean))].sort();
      const cur = saasSectorSel.value;
      saasSectorSel.innerHTML = '<option value="">All Sectors</option>' +
        sectors.map(s => `<option value="${esc(s)}"${s===cur?' selected':''}>${esc(s)}</option>`).join('');
    }
    tbody.innerHTML = companies.map((c, i) => `<tr data-search="${esc(((c.company_name||'')+' '+(c.sector||'')).toLowerCase())}" data-sector="${esc(c.sector||'')}">
      <td class="row-num td-center" style="color:var(--text3);font-size:12px">${i + 1}</td>
      <td class="td-bold">${esc(c.company_name || '—')}</td>
      <td>${esc(c.sector || '—')}</td>
      <td class="td-right">${c.mrr != null ? fmtCr(c.mrr) : '—'}</td>
      <td class="td-right">${c.arr != null ? fmtCr(c.arr) : '—'}</td>
      <td class="td-right">${c.nrr != null ? c.nrr.toFixed(1) + '%' : '—'}</td>
      <td class="td-right">${c.churn_rate != null ? c.churn_rate.toFixed(2) + '%' : '—'}</td>
      <td class="td-right">${c.cac != null ? '₹' + Number(c.cac).toLocaleString('en-IN', {maximumFractionDigits:0}) : '—'}</td>
      <td class="td-right">${c.ltv != null ? '₹' + Number(c.ltv).toLocaleString('en-IN', {maximumFractionDigits:0}) : '—'}</td>
      <td class="td-right">${c.ltv_cac_ratio != null ? c.ltv_cac_ratio.toFixed(1) + 'x' : (c.ltv != null && c.cac ? (c.ltv / c.cac).toFixed(1) + 'x' : '—')}</td>
    </tr>`).join('') || '<tr><td colspan="10" class="table-empty">No SaaS metrics imported yet. Upload a file with MRR, ARR, Churn, NRR columns.</td></tr>';
  } catch(e) {
    if ($('saas-mrr'))   $('saas-mrr').textContent   = '—';
    if ($('saas-arr'))   $('saas-arr').textContent   = '—';
    if ($('saas-churn')) $('saas-churn').textContent = '—';
    if ($('saas-nrr'))   $('saas-nrr').textContent   = '—';
    if (tbody) tbody.innerHTML = '<tr><td colspan="10" class="table-empty">No SaaS data.</td></tr>';
  }
}
function filterSaasMetrics() { _filterTableRows('saas-tbody','saas-search','saas-sector-filter',null,'saas-count','companies'); }

async function loadQuotedUnquoted() {
  try {
    const qs = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const data = await Auth.apiGet(`/portfolio/quoted-unquoted/${qs}`);
    const quoted   = data.quoted   || [];
    const unquoted = data.unquoted || [];
    const summary  = data.summary  || {};

    if ($('q-quoted-count'))   $('q-quoted-count').textContent   = summary.quoted_count   ?? '—';
    if ($('q-unquoted-count')) $('q-unquoted-count').textContent = summary.unquoted_count ?? '—';
    if ($('q-quoted-cost'))    $('q-quoted-cost').textContent    = summary.quoted_cost   ? fmtCr(summary.quoted_cost)   : '—';
    if ($('q-unquoted-cost'))  $('q-unquoted-cost').textContent  = summary.unquoted_cost ? fmtCr(summary.unquoted_cost) : '—';

    const qbody = $('quoted-tbody');
    if (qbody) {
      if ($('quoted-count')) $('quoted-count').textContent = `(${quoted.length} companies)`;
      const qSectors = [...new Set(quoted.map(c => c.sector).filter(Boolean))].sort();
      const qSel = $('quoted-sector-filter');
      if (qSel) {
        const cur = qSel.value;
        qSel.innerHTML = '<option value="">All Sectors</option>' +
          qSectors.map(s => `<option value="${esc(s)}"${s===cur?' selected':''}>${esc(s)}</option>`).join('');
      }
      qbody.innerHTML = quoted.map((c, i) => `<tr data-search="${esc(((c.name||'')+' '+(c.sector||'')+' '+(c.exchange||'')).toLowerCase())}" data-sector="${esc(c.sector||'')}">
        <td class="row-num td-center" style="color:var(--text3);font-size:12px">${i + 1}</td>
        <td class="td-bold">${esc(c.name)}</td>
        <td>${esc(c.sector || '—')}</td>
        <td><span style="color:var(--accent);font-weight:600">${esc(c.exchange || '—')}</span></td>
        <td class="td-right">${c.cost ? fmtCr(c.cost) : '—'}</td>
        <td class="td-right">${c.fair_value ? fmtCr(c.fair_value) : '—'}</td>
        <td class="td-center"><span style="font-size:11px;color:var(--text3)">L${c.ipev_level || '—'}</span></td>
      </tr>`).join('') || '<tr><td colspan="7" class="table-empty">No quoted companies.</td></tr>';
    }

    const ubody = $('unquoted-tbody');
    if (ubody) {
      if ($('unquoted-count')) $('unquoted-count').textContent = `(${unquoted.length} companies)`;
      const uSectors = [...new Set(unquoted.map(c => c.sector).filter(Boolean))].sort();
      const uSel = $('unquoted-sector-filter');
      if (uSel) {
        const cur = uSel.value;
        uSel.innerHTML = '<option value="">All Sectors</option>' +
          uSectors.map(s => `<option value="${esc(s)}"${s===cur?' selected':''}>${esc(s)}</option>`).join('');
      }
      ubody.innerHTML = unquoted.map((c, i) => `<tr data-search="${esc(((c.name||'')+' '+(c.sector||'')).toLowerCase())}" data-sector="${esc(c.sector||'')}">
        <td class="row-num td-center" style="color:var(--text3);font-size:12px">${i + 1}</td>
        <td class="td-bold">${esc(c.name)}</td>
        <td>${esc(c.sector || '—')}</td>
        <td class="td-right">${c.cost ? fmtCr(c.cost) : '—'}</td>
        <td class="td-right">${c.fair_value ? fmtCr(c.fair_value) : '—'}</td>
        <td class="td-center"><span style="font-size:11px;color:var(--text3)">L${c.ipev_level || '—'}</span></td>
      </tr>`).join('') || '<tr><td colspan="6" class="table-empty">No unquoted companies.</td></tr>';
    }
  } catch(e) {
    const qbody = $('quoted-tbody');
    const ubody = $('unquoted-tbody');
    if (qbody) qbody.innerHTML = '<tr><td colspan="7" class="table-empty">No data.</td></tr>';
    if (ubody) ubody.innerHTML = '<tr><td colspan="6" class="table-empty">No data.</td></tr>';
  }
}
function filterQuoted()   { _filterTableRows('quoted-tbody',   'quoted-search',   'quoted-sector-filter',   null, 'quoted-count',   'companies'); }
function filterUnquoted() { _filterTableRows('unquoted-tbody', 'unquoted-search', 'unquoted-sector-filter', null, 'unquoted-count', 'companies'); }

/* ── Generic table filter utility ─────────────────────────── */
/**
 * Filters visible rows of a tbody by search text + data-sector + data-stage.
 * Rows must have: data-search (lowercased search blob), data-sector, data-stage attrs.
 * First <td class="row-num"> in each visible row gets re-sequenced (1, 2, 3…).
 * @param {string} tbodyId   - id of the <tbody> element
 * @param {string} searchId  - id of search <input> (or null)
 * @param {string} sectorId  - id of sector <select> (or null)
 * @param {string} stageId   - id of stage <select> (or null)
 * @param {string} countId   - id of count <span/element> (or null)
 * @param {string} countLabel- label word to use in count e.g. "companies" or "records"
 */
function _filterTableRows(tbodyId, searchId, sectorId, stageId, countId, countLabel) {
  const tbody = $(tbodyId);
  if (!tbody) return;
  const q      = searchId ? (($(searchId) || {}).value || '').toLowerCase()  : '';
  const sector = sectorId ? (($(sectorId) || {}).value || '') : '';
  const stage  = stageId  ? (($(stageId)  || {}).value || '') : '';
  let n = 0;
  Array.from(tbody.rows).forEach(tr => {
    const blob = (tr.dataset.search || '').toLowerCase();
    const show = (!q      || blob.includes(q))   &&
                 (!sector || tr.dataset.sector === sector) &&
                 (!stage  || tr.dataset.stage  === stage);
    tr.style.display = show ? '' : 'none';
    if (show) {
      n++;
      const numCell = tr.querySelector('td.row-num');
      if (numCell) numCell.textContent = n;
    }
  });
  if (countId) {
    const el = $(countId);
    if (el) el.textContent = `(${n} ${countLabel || 'records'})`;
  }
}

/* ── Portfolio: Investments Detail ────────────────────────── */
let _invDetailRows = [];

async function loadPortfolioInvestments() {
  const tbody = $('inv-detail-tbody');
  if (!tbody) return;
  try {
    const fqs = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const data = await Auth.apiGet('/portfolio/investments/' + fqs);
    _invDetailRows = data.investments || [];

    // Populate sector filter
    const sectorSel = $('inv-sector-filter');
    if (sectorSel) {
      const sectors = [...new Set(_invDetailRows.map(r => r.sector).filter(Boolean))].sort();
      const cur = sectorSel.value;
      sectorSel.innerHTML = '<option value="">All Sectors</option>' +
        sectors.map(s => `<option value="${esc(s)}"${s === cur ? ' selected' : ''}>${esc(s)}</option>`).join('');
    }

    // Populate stage filter
    const stageSel = $('inv-stage-filter');
    if (stageSel) {
      const stages = [...new Set(_invDetailRows.map(r => r.stage).filter(Boolean))].sort();
      const cur = stageSel.value;
      stageSel.innerHTML = '<option value="">All Stages</option>' +
        stages.map(s => `<option value="${esc(s)}"${s === cur ? ' selected' : ''}>${esc(s)}</option>`).join('');
    }

    renderInvDetail();
  } catch(e) {
    if (tbody) tbody.innerHTML = '<tr><td colspan="12" class="table-empty">No data.</td></tr>';
  }
}

function filterInvDetail() {
  renderInvDetail();
}

function renderInvDetail() {
  const tbody = $('inv-detail-tbody');
  if (!tbody) return;

  const q      = (($('inv-search')        || {}).value || '').toLowerCase();
  const sector = (($('inv-sector-filter') || {}).value || '');
  const stage  = (($('inv-stage-filter')  || {}).value || '');

  const filtered = _invDetailRows.filter(inv => {
    const text = (inv.company_name + ' ' + inv.scheme_name + ' ' + inv.sector + ' ' + inv.stage).toLowerCase();
    return (!q      || text.includes(q)) &&
           (!sector || inv.sector === sector) &&
           (!stage  || inv.stage  === stage);
  });

  const countEl = $('inv-detail-count');
  if (countEl) countEl.textContent = `(${filtered.length} investments)`;

  const statusColor = { active: '#22c55e', partially_exited: '#f59e0b', fully_exited: '#06b6d4', written_off: '#ef4444' };
  tbody.innerHTML = filtered.map((inv, i) => {
    const color = statusColor[inv.status] || 'var(--text3)';
    return `<tr>
      <td style="text-align:center;color:var(--text3);font-size:12px">${i + 1}</td>
      <td class="td-bold">${esc(inv.company_name)}</td>
      <td style="color:var(--text2);font-size:12px">${esc(inv.scheme_name)}</td>
      <td>${esc(inv.sector || '—')}</td>
      <td>${esc(inv.stage || '—')}</td>
      <td style="font-size:11px">${esc(inv.instrument_type_display || inv.instrument_type || '—')}</td>
      <td class="td-right">${inv.total_invested ? fmtCr(inv.total_invested) : '—'}</td>
      <td class="td-right">${inv.ownership_pct != null ? inv.ownership_pct.toFixed(2) + '%' : '—'}</td>
      <td class="td-right">${inv.irr_pct != null ? inv.irr_pct.toFixed(1) + '%' : '—'}</td>
      <td class="td-right">${inv.latest_valuation != null ? fmtCr(inv.latest_valuation) : '—'}</td>
      <td class="td-center"><span style="font-size:11px;font-weight:600;color:${color}">${esc(inv.status_display || inv.status)}</span></td>
      <td style="font-size:12px;color:var(--text3)">${inv.investment_date ? inv.investment_date.slice(0,10) : '—'}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="12" class="table-empty">No investments found.</td></tr>';
}

/* ── Portfolio: Valuations ─────────────────────────────────── */
async function loadPortfolioValuations() {
  const tbody = $('val-tab-tbody');
  if (!tbody) return;
  try {
    const fqs = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const data = await Auth.apiGet('/portfolio/valuations/' + fqs);
    const rows = data.valuations || [];

    if ($('val-tab-count')) $('val-tab-count').textContent = `(${rows.length} records)`;
    const statusColor = { draft: 'var(--text3)', submitted: '#f59e0b', approved: '#22c55e', rejected: '#ef4444' };
    const ipevLabel = { 1: 'L1 — Quoted', 2: 'L2 — Observable', 3: 'L3 — Unobservable' };
    tbody.innerHTML = rows.map((v, i) => {
      const color = statusColor[v.status] || 'var(--text3)';
      return `<tr data-search="${esc(((v.company_name||'')+' '+(v.scheme_name||'')+' '+(v.methodology||'')).toLowerCase())}">
        <td class="row-num td-center" style="color:var(--text3);font-size:12px">${i + 1}</td>
        <td class="td-bold">${esc(v.company_name)}</td>
        <td style="color:var(--text2);font-size:12px">${esc(v.scheme_name)}</td>
        <td style="font-size:12px">${v.valuation_date ? v.valuation_date.slice(0,10) : '—'}</td>
        <td class="td-right">${fmtCr(v.fair_value)}</td>
        <td style="font-size:12px">${esc(v.methodology_display || v.methodology)}</td>
        <td class="td-center"><span style="font-size:11px;color:var(--accent)">${v.ipev_level ? esc(ipevLabel[v.ipev_level] || 'L'+v.ipev_level) : '—'}</span></td>
        <td class="td-right">${v.multiple != null ? v.multiple.toFixed(2) + 'x' : '—'}</td>
        <td class="td-center"><span style="font-size:11px;font-weight:600;color:${color}">${esc(v.status)}</span></td>
        <td style="font-size:12px;color:var(--text3)">${esc(v.submitted_by || '—')}</td>
        <td style="font-size:12px;color:var(--text3)">${esc(v.approved_by || '—')}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="11" class="table-empty">No valuations found.</td></tr>';
  } catch(e) {
    if (tbody) tbody.innerHTML = '<tr><td colspan="11" class="table-empty">No data.</td></tr>';
  }
}
function filterValTab() { _filterTableRows('val-tab-tbody','val-tab-search',null,null,'val-tab-count','records'); }

/* ── Portfolio: KPI Tracking ───────────────────────────────── */
function reloadKPITracking() {
  loadPortfolioKPITracking();
}

async function loadPortfolioKPITracking() {
  const tbody = $('kpi-track-tbody');
  if (!tbody) return;
  try {
    const qs = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const data = await Auth.apiGet(`/portfolio/kpi-matrix/${qs}`);
    const companies = data.companies || [];

    if ($('kpi-track-count')) $('kpi-track-count').textContent = `(${companies.length} companies)`;
    const _p = v => v != null ? Number(v).toFixed(1) + '%' : '—';
    const _n = v => v != null ? Number(v).toLocaleString('en-IN', {maximumFractionDigits: 2}) : '—';
    tbody.innerHTML = companies.map((c, i) => `<tr data-search="${esc((c.company_name||'').toLowerCase())}">
      <td class="row-num td-center" style="color:var(--text3);font-size:12px">${i + 1}</td>
      <td class="td-bold">${esc(c.company_name || '—')}</td>
      <td class="td-right">${c.gmv != null ? fmtCr(c.gmv) : '—'}</td>
      <td class="td-right">${c.revenue != null ? fmtCr(c.revenue) : '—'}</td>
      <td class="td-right">${_p(c.gross_m)}</td>
      <td class="td-right">${_p(c.ebitda)}</td>
      <td class="td-right">${_n(c.orders)}</td>
      <td class="td-right">${_n(c.aov)}</td>
      <td class="td-right">${_p(c.returns)}</td>
      <td class="td-right">${_n(c.cac)}</td>
      <td class="td-right">${_p(c.repeat)}</td>
      <td class="td-right">${c.cost != null ? fmtCr(c.cost) : '—'}</td>
      <td class="td-right">${c.fv != null ? fmtCr(c.fv) : '—'}</td>
    </tr>`).join('') || '<tr><td colspan="13" class="table-empty">No KPI data found. Upload a fund Excel file to extract KPI metrics.</td></tr>';
  } catch(e) {
    if (tbody) tbody.innerHTML = '<tr><td colspan="13" class="table-empty">No data.</td></tr>';
  }
}
function filterKPITracking() { _filterTableRows('kpi-track-tbody','kpi-track-search',null,null,'kpi-track-count','companies'); }

/* ── Portfolio: Exit Scenarios ─────────────────────────────── */
async function loadPortfolioExitScenarios() {
  const tbody = $('exit-scen-tbody');
  if (!tbody) return;
  try {
    const fqs = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const data = await Auth.apiGet('/portfolio/exit-scenarios/' + fqs);
    const rows = data.scenarios || [];

    if ($('es-total'))    $('es-total').textContent    = rows.length;
    if ($('es-actual'))   $('es-actual').textContent   = rows.filter(r => r.is_actual).length;
    if ($('es-modelled')) $('es-modelled').textContent = rows.filter(r => !r.is_actual).length;

    if ($('exit-scen-count')) $('exit-scen-count').textContent = `(${rows.length} records)`;
    const exitTypeColor = { ipo: '#06b6d4', merger_acquisition: '#8b5cf6', secondary_sale: '#f59e0b', buyback: '#22c55e', write_off: '#ef4444' };
    tbody.innerHTML = rows.map((e, i) => {
      const tColor = exitTypeColor[e.exit_type] || 'var(--text2)';
      return `<tr data-search="${esc(((e.company_name||'')+' '+(e.scheme_name||'')+' '+(e.exit_type_display||'')).toLowerCase())}">
        <td class="row-num td-center" style="color:var(--text3);font-size:12px">${i + 1}</td>
        <td class="td-bold">${esc(e.company_name)}</td>
        <td style="color:var(--text2);font-size:12px">${esc(e.scheme_name)}</td>
        <td><span style="font-size:12px;font-weight:600;color:${tColor}">${esc(e.exit_type_display || e.exit_type)}</span></td>
        <td class="td-center"><span style="font-size:11px;color:${e.is_actual ? '#22c55e' : '#f59e0b'}">${e.is_actual ? 'Actual' : 'Scenario'}</span></td>
        <td style="font-size:12px">${e.exit_date ? e.exit_date.slice(0,10) : '—'}</td>
        <td class="td-right">${e.proceeds != null ? fmtCr(e.proceeds) : '—'}</td>
        <td class="td-right">${e.moic != null ? e.moic.toFixed(2) + 'x' : '—'}</td>
        <td class="td-right">${e.irr_pct != null ? e.irr_pct.toFixed(1) + '%' : '—'}</td>
        <td style="font-size:11px;color:var(--text3)">${esc(e.gain_loss_nature || '—').toUpperCase()}</td>
        <td style="font-size:12px">${esc(e.buyer_name || '—')}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="11" class="table-empty">No exit scenarios found.</td></tr>';
  } catch(e) {
    if (tbody) tbody.innerHTML = '<tr><td colspan="11" class="table-empty">No data.</td></tr>';
  }
}
function filterExitScenarios() { _filterTableRows('exit-scen-tbody','exit-scen-search',null,null,'exit-scen-count','records'); }

/* ── Portfolio: Board Meetings ─────────────────────────────── */
async function loadPortfolioBoardMeetings() {
  const tbody = $('board-meet-tbody');
  if (!tbody) return;
  try {
    const fqs = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const data = await Auth.apiGet('/portfolio/board-meetings/' + fqs);
    const rows = data.meetings || [];

    if ($('bm-total'))     $('bm-total').textContent     = rows.length;
    const companies = new Set(rows.map(m => m.company_name));
    if ($('bm-companies')) $('bm-companies').textContent = companies.size;
    const future = rows.filter(m => m.next_meeting_date).sort((a,b) => a.next_meeting_date > b.next_meeting_date ? 1 : -1);
    if ($('bm-next')) $('bm-next').textContent = future.length ? future[0].next_meeting_date.slice(0,10) : '—';

    if ($('board-meet-count')) $('board-meet-count').textContent = `(${rows.length} meetings)`;
    tbody.innerHTML = rows.map((m, i) => {
      const agendaShort = m.agenda ? (m.agenda.length > 60 ? m.agenda.slice(0,60) + '…' : m.agenda) : '—';
      return `<tr data-search="${esc(((m.company_name||'')+' '+(m.scheme_name||'')+' '+(m.agenda||'')).toLowerCase())}">
        <td class="row-num td-center" style="color:var(--text3);font-size:12px">${i + 1}</td>
        <td class="td-bold">${esc(m.company_name)}</td>
        <td style="color:var(--text2);font-size:12px">${esc(m.scheme_name)}</td>
        <td style="font-size:12px">${m.meeting_date ? m.meeting_date.slice(0,10) : '—'}</td>
        <td style="font-size:12px;color:var(--text2);max-width:220px">${esc(agendaShort)}</td>
        <td class="td-center">${Array.isArray(m.attendees) ? m.attendees.length : '—'}</td>
        <td class="td-center">${Array.isArray(m.resolutions) ? m.resolutions.length : '—'}</td>
        <td style="font-size:12px;color:var(--text3)">${m.next_meeting_date ? m.next_meeting_date.slice(0,10) : '—'}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="8" class="table-empty">No board meetings found.</td></tr>';
  } catch(e) {
    if (tbody) tbody.innerHTML = '<tr><td colspan="8" class="table-empty">No data.</td></tr>';
  }
}
function filterBoardMeetings() { _filterTableRows('board-meet-tbody','board-meet-search',null,null,'board-meet-count','meetings'); }

/* ── Accounting ────────────────────────────────────────────── */
async function loadAccountingNAV() {
  try {
    const fqs  = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const navs = await Auth.apiGet('/accounting/nav/' + fqs);
    const arr  = (navs.results || navs || []).sort((a,b) => (b.nav_date||'') > (a.nav_date||'') ? 1 : -1);

    // Latest NAV per scheme → aggregate KPIs
    const latestByScheme = {};
    arr.forEach(n => { if (!latestByScheme[n.scheme]) latestByScheme[n.scheme] = n; });
    const latestArr = Object.values(latestByScheme);
    const totalNav  = latestArr.reduce((s, n) => s + parseFloat(n.total_nav || 0), 0);
    const avgNavPerUnit = latestArr.length
      ? latestArr.reduce((s, n) => s + parseFloat(n.nav_per_unit || 0), 0) / latestArr.length
      : 0;

    // Sum unrealized/realized gains from the latest NAV per scheme
    // (these are now stored directly on the NAVRecord from the Excel import)
    const totalUnrealized = latestArr.reduce((s, n) => s + parseFloat(n.unrealized_gains || 0), 0);
    const totalRealized   = latestArr.reduce((s, n) => s + parseFloat(n.realized_gains   || 0), 0);
    // Mgmt fee: sum management_fee_payable across all NAV records (cumulative YTD proxy)
    const totalMgmtFee    = arr.reduce((s, n) => s + parseFloat(n.management_fee_payable || 0), 0);

    if ($('acc-nav-val'))    $('acc-nav-val').textContent    = fmtCr(totalNav);
    if ($('acc-nav-unit'))   $('acc-nav-unit').textContent   = avgNavPerUnit > 0 ? fmtCr(avgNavPerUnit) : '—';
    if ($('acc-mgmt-fee'))   $('acc-mgmt-fee').textContent   = totalMgmtFee > 0 ? fmtCr(totalMgmtFee) : '—';
    if ($('acc-unrealized')) $('acc-unrealized').textContent = totalUnrealized > 0 ? fmtCr(totalUnrealized) : '—';
    if ($('acc-realized'))   $('acc-realized').textContent   = totalRealized   > 0 ? fmtCr(totalRealized)   : '—';

    const tbody = $('acc-nav-hist-tbody');
    if (tbody) {
      tbody.innerHTML = arr.slice(0,12).map(n => {
        const qoqEl = n.nav_per_unit ? parseFloat(n.nav_per_unit).toFixed(4) : '—';
        return `<tr>
          <td>${n.nav_date || '—'}</td>
          <td>${esc(n.scheme_name || '—')}</td>
          <td class="td-right">${fmtCr(parseFloat(n.total_nav||0))}</td>
          <td class="td-right">${qoqEl}</td>
        </tr>`;
      }).join('') || '<tr><td colspan="4" class="table-empty">No NAV records.</td></tr>';
    }

    // NAV trend chart
    const navByDate = {};
    arr.forEach(n => {
      navByDate[n.nav_date] = (navByDate[n.nav_date] || 0) + parseFloat(n.total_nav || 0);
    });
    const chartVals = Object.entries(navByDate).sort(([a],[b]) => a<b?-1:1).slice(-6).map(([,v])=>v);
    renderMiniChart('acc-nav-chart', chartVals, '#2563eb');
    const navDates = Object.keys(navByDate).sort().slice(-6);
    ['nav-date-1','nav-date-2'].forEach((id,i) => { const el=$(id); if(el&&navDates[i]) el.textContent=navDates[i].slice(0,7); });
    const lastDateEl = $('nav-date-last');
    if (lastDateEl && navDates.length) lastDateEl.textContent = navDates[navDates.length-1].slice(0,7);

  } catch(e) { console.error('NAV load error:', e); }
}

async function renderWaterfall() {
  const el = $('acc-wf');
  if (!el) return;

  try {
    const fqs  = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const carryData = await Auth.apiGet('/accounting/carry/' + fqs);
    const carries = carryData.results || carryData || [];

    if (carries.length) {
      let totalCalled=0, totalDist=0, prefReturn=0, carryGross=0, carryNet=0, clawback=0;
      carries.forEach(c => {
        totalCalled  += parseFloat(c.total_called_capital    || 0);
        totalDist    += parseFloat(c.total_distributions     || 0);
        prefReturn   += parseFloat(c.preferred_return_amount || 0);
        carryGross   += parseFloat(c.carry_amount_gross      || 0);
        carryNet     += parseFloat(c.carry_amount_net        || 0);
        clawback     += parseFloat(c.gp_clawback_provision   || 0);
      });
      const gpCatchup = Math.max(0, carryGross - carryNet);
      const items = [
        { label:'Return of Capital', val:totalCalled, color:'#2563eb' },
        { label:'Preferred Return',  val:prefReturn,  color:'#10b981' },
        { label:'GP Catch-up',       val:gpCatchup,   color:'#f59e0b' },
        { label:'Carried Interest',  val:carryNet,    color:'#8b5cf6' },
      ];
      const maxVal = Math.max(...items.map(i=>i.val), 1);
      el.innerHTML = items.map(it => {
        const pct = Math.round(it.val / maxVal * 100);
        return `<div class="v5-wf-bar">
          <div class="v5-wf-label">${it.label}</div>
          <div class="v5-wf-track"><div class="v5-wf-fill" style="width:${pct}%;background:${it.color}">${pct}%</div></div>
          <div class="v5-wf-num">${fmtCr(it.val)} Cr</div>
        </div>`;
      }).join('') + '<div style="font-size:10px;color:var(--text3);margin-top:10px">European model · 8% Hurdle · 20% Carry · 100% GP Catch-up</div>';

      // Carry & Clawback Analysis
      const carryEl = $('acc-carry');
      if (carryEl) {
        const statusMap = { indicative:'Indicative', crystallised:'Crystallised', paid:'Paid' };
        const latestCarry = carries[0] || {};
        const statusLabel = statusMap[latestCarry.calculation_status] || 'Indicative';
        carryEl.innerHTML = `
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px">
            <div class="v5-kpi-card gold" style="padding:14px">
              <div class="v5-kpi-label">Carry Base</div>
              <div class="v5-kpi-value" style="font-size:1.2rem">${fmtCr(carries.reduce((s,c)=>s+parseFloat(c.carry_base||0),0))} Cr</div>
              <div class="v5-kpi-sub">Profit above hurdle</div>
            </div>
            <div class="v5-kpi-card purple" style="padding:14px">
              <div class="v5-kpi-label">GP Carry (Gross)</div>
              <div class="v5-kpi-value" style="font-size:1.2rem">${fmtCr(carryGross)} Cr</div>
              <div class="v5-kpi-sub">Before clawback</div>
            </div>
            <div class="v5-kpi-card cyan" style="padding:14px">
              <div class="v5-kpi-label">GP Carry (Net)</div>
              <div class="v5-kpi-value" style="font-size:1.2rem">${fmtCr(carryNet)} Cr</div>
              <div class="v5-kpi-sub">After clawback provision</div>
            </div>
            <div class="v5-kpi-card blue" style="padding:14px">
              <div class="v5-kpi-label">Clawback Provision</div>
              <div class="v5-kpi-value" style="font-size:1.2rem">${fmtCr(clawback)} Cr</div>
              <div class="v5-kpi-sub">Excess carry to LPs</div>
            </div>
          </div>
          <div style="font-size:11px;color:var(--text3)">Status: ${statusLabel} · Calculation date: ${latestCarry.calculation_date || '—'}</div>`;
      }
    } else {
      // No carry records — render static waterfall bars
      const items = [
        { label:'Return of Capital', pct:55, color:'#2563eb', val:'—' },
        { label:'Preferred Return',  pct:22, color:'#10b981', val:'—' },
        { label:'GP Catch-up',       pct:8,  color:'#f59e0b', val:'—' },
        { label:'Carried Interest',  pct:15, color:'#8b5cf6', val:'—' },
      ];
      el.innerHTML = items.map(it => `<div class="v5-wf-bar">
        <div class="v5-wf-label">${it.label}</div>
        <div class="v5-wf-track"><div class="v5-wf-fill" style="width:${it.pct}%;background:${it.color}">${it.pct}%</div></div>
        <div class="v5-wf-num">${it.val}</div>
      </div>`).join('') + '<div style="font-size:10px;color:var(--text3);margin-top:10px">European model · 8% Hurdle · 20% Carry · 100% GP Catch-up</div>';

      const carryEl = $('acc-carry');
      if (carryEl) {
        carryEl.innerHTML = '<div style="color:var(--text3);font-size:11px;padding:8px">Run waterfall engine to compute carry & clawback.</div>';
      }
    }
  } catch(e) {
    console.error('Waterfall render error:', e);
  }

  // Init waterfall simulator on first render
  if (!window._wfSimInit) {
    window._wfSimInit = true;
    _initWaterfallSim();
  }
}

async function loadCapitalCalls() {
  try {
    const fqs  = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const data = await Auth.apiGet('/lp/capital-calls/' + fqs);
    const calls = data.results || data || [];
    const tbody = $('acc-calls-tbody');
    if (!tbody) return;

    let totalCalled = 0;
    calls.forEach(c => { totalCalled += parseFloat(c.total_call_amount || 0); });

    let n = 0;
    tbody.innerHTML = calls.map(c => {
      const purposeFull = c.purpose || c.scheme_name || '—';
      const parts = purposeFull.split(' — ');
      const lpName = parts.length > 1 ? parts[0] : purposeFull;
      const purposeText = parts.length > 1 ? parts.slice(1).join(' — ') : '—';
      const statusCls = (c.call_status || '').toLowerCase().replace('_', '-');
      const statusVal = (c.call_status || '').toLowerCase();
      n++;
      const searchBlob = [lpName, c.call_date, c.status_display, purposeText].join(' ').toLowerCase();
      return `<tr data-search="${esc(searchBlob)}" data-status="${statusVal}">
        <td class="row-num td-center">${n}</td>
        <td class="td-bold">${esc(lpName)}</td>
        <td>${c.call_date || '—'}</td>
        <td class="td-right">${fmtCr(parseFloat(c.total_call_amount||0))}</td>
        <td class="td-center"><span class="v5-status ${statusCls}">${esc(c.status_display || c.call_status || '—')}</span></td>
        <td>${esc(purposeText)}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="6" class="table-empty">No capital calls for this fund.</td></tr>';

    const countEl = $('calls-count');
    if (countEl) countEl.textContent = `(${n} records)`;
    const sumEl = $('acc-calls-total');
    if (sumEl) sumEl.textContent = fmtCr(totalCalled);
  } catch(e) {}
}

function filterCapitalCalls() {
  const tbody = $('acc-calls-tbody');
  if (!tbody) return;
  const q      = ($('calls-search')        || {value:''}).value.toLowerCase();
  const status = ($('calls-status-filter') || {value:''}).value;
  let n = 0;
  Array.from(tbody.rows).forEach(tr => {
    const blob = (tr.dataset.search || '').toLowerCase();
    const show = (!q || blob.includes(q)) && (!status || tr.dataset.status === status);
    tr.style.display = show ? '' : 'none';
    if (show) {
      n++;
      const numCell = tr.querySelector('td.row-num');
      if (numCell) numCell.textContent = n;
    }
  });
  const countEl = $('calls-count');
  if (countEl) countEl.textContent = `(${n} records)`;
}

async function loadDistributions() {
  try {
    const fqs  = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const data = await Auth.apiGet('/lp/distributions/' + fqs);
    const dists = data.results || data || [];
    const tbody = $('acc-dist-tbody');
    if (!tbody) return;

    let totalDist = 0;
    dists.forEach(d => { totalDist += parseFloat(d.total_net_amount || 0); });

    tbody.innerHTML = dists.map(d => `<tr>
      <td class="td-bold">${esc(d.scheme_name || '—')}</td>
      <td>${d.distribution_date || '—'}</td>
      <td class="td-right">${fmtCr(parseFloat(d.total_net_amount||0))}</td>
      <td>${esc(d.type_display || d.distribution_type || '—')}</td>
      <td class="td-center"><span class="v5-status ${(d.distribution_status||'active').toLowerCase()}">${esc(d.status_display || d.distribution_status || '—')}</span></td>
    </tr>`).join('') || '<tr><td colspan="5" class="table-empty">No distributions for this fund.</td></tr>';

    const sumEl = $('acc-dist-total');
    if (sumEl) sumEl.textContent = fmtCr(totalDist);
  } catch(e) {}
}

async function loadFundPL() {
  const body = $('acc-fpl-body');
  if (!body) return;
  try {
    const fqs  = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const navs = await Auth.apiGet('/accounting/nav/' + fqs);
    const arr  = (navs.results || navs || []).sort((a,b) => (a.nav_date||'') < (b.nav_date||'') ? -1 : 1);
    if (!arr.length) {
      body.innerHTML = '<div style="color:var(--text3);font-size:11px;padding:8px">No NAV data. Re-import your fund Excel to populate Fund P&amp;L.</div>';
      return;
    }
    const rows = arr.map(n => {
      const unrealized = parseFloat(n.unrealized_gains || 0);
      const realized   = parseFloat(n.realized_gains   || 0);
      const mgmtFee    = parseFloat(n.management_fee_payable || 0);
      const otherLiab  = parseFloat(n.other_liabilities || 0);
      const grossIncome = unrealized + realized;
      const totalExp    = mgmtFee + otherLiab;
      const netPL       = grossIncome - totalExp;
      const netCls      = netPL >= 0 ? 'v5-text-green' : 'v5-text-red';
      return `<tr>
        <td>${n.nav_date || '—'}</td>
        <td>${esc(n.scheme_name || '—')}</td>
        <td class="td-right">${unrealized > 0 ? fmtCr(unrealized)+' Cr' : '—'}</td>
        <td class="td-right">${realized   > 0 ? fmtCr(realized)+' Cr'   : '—'}</td>
        <td class="td-right" style="color:var(--accent-red)">${mgmtFee > 0 ? fmtCr(mgmtFee)+' Cr' : '—'}</td>
        <td class="td-right ${netCls}">${fmtCr(netPL)} Cr</td>
      </tr>`;
    }).join('');
    body.innerHTML = `
      <div class="v5-table-scroll">
        <table class="v5-table">
          <thead><tr>
            <th>Period</th><th>Scheme</th>
            <th class="td-right">Unrealized Gains</th>
            <th class="td-right">Realized Gains</th>
            <th class="td-right">Mgmt Fee</th>
            <th class="td-right">Net P&amp;L</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  } catch(e) {
    if (body) body.innerHTML = '<div style="color:var(--text3);font-size:11px;padding:8px">Could not load Fund P&amp;L data.</div>';
  }
}

/* ══════════════════════════════════════════════════════════════
   FUND ACCOUNTING — Extended Tabs (NAV Records, Carry, Ledger,
   Management Fees, Chart of Accounts, Trial Balance, Financials)
   All functions use _ctx.schemeIds for automatic fund filtering.
══════════════════════════════════════════════════════════════ */

// Shared state for extended accounting tabs
let _accSchemes   = [];   // { id, name, fund_name }
let _accNav       = [];
let _accCarry     = [];
let _accLedger    = [];
let _accFees      = [];
let _accCOA       = [];
let _accSchemesLoaded = false;

// Formatting helpers (accounting-specific)
function _accFmt(v) {
  if (!v && v !== 0) return '—';
  const n = parseFloat(v);
  if (isNaN(n)) return '—';
  if (Math.abs(n) >= 1e7) return `₹${(n / 1e7).toFixed(2)} Cr`;
  if (Math.abs(n) >= 1e5) return `₹${(n / 1e5).toFixed(2)} L`;
  return `₹${n.toLocaleString('en-IN', {maximumFractionDigits: 2})}`;
}
function _accDate(d) {
  return d ? new Date(d).toLocaleDateString('en-IN', {day:'2-digit',month:'short',year:'numeric'}) : '—';
}

// Load all schemes once per session (respects _ctx.schemeIds filter)
async function _loadAccSchemes() {
  if (_accSchemesLoaded) return;
  try {
    const funds = await Auth.apiGet('/funds/');
    const farr  = Array.isArray(funds) ? funds : (funds.results || []);
    _accSchemes = [];
    for (const f of farr) {
      const ss = await Auth.apiGet(`/funds/${f.id}/schemes/`);
      for (const s of (Array.isArray(ss) ? ss : (ss.results || []))) {
        _accSchemes.push({ id: s.id, name: s.name, fund_name: f.name });
      }
    }
    _accSchemesLoaded = true;
  } catch(e) {}
}

// Populate a <select> with relevant schemes (filtered to _ctx.schemeIds if set)
function _accPopulateSelect(selectId) {
  const sel = $(selectId);
  if (!sel) return;
  // Keep first "All Schemes / Select Scheme" option
  const firstOpt = sel.options[0];
  sel.innerHTML = '';
  sel.appendChild(firstOpt);
  const show = _ctx.schemeIds && _ctx.schemeIds.length
    ? _accSchemes.filter(s => _ctx.schemeIds.includes(s.id))
    : _accSchemes;
  show.forEach(s => {
    const o = document.createElement('option');
    o.value = s.id;
    o.textContent = `${s.fund_name} → ${s.name}`;
    sel.appendChild(o);
  });
}

// ── NAV RECORDS ──────────────────────────────────────────────
async function loadAccNAVRecords() {
  await _loadAccSchemes();

  const reloadNav = async () => {
    const recon = $('acc-nav-recon')?.value || '';
    const params = [];
    if (_ctx.fundId) params.push(`fund=${_ctx.fundId}`);
    if (recon) params.push(`reconciled=${recon}`);
    try {
      const raw = await Auth.apiGet('/accounting/nav/' + (params.length ? '?' + params.join('&') : ''));
      _accNav = (raw.results || raw || []).sort((a,b) => (b.nav_date||'') > (a.nav_date||'') ? 1 : -1);
    } catch(e) { _accNav = []; }
    _renderAccNAV();
  };

  const reconSel = $('acc-nav-recon');
  if (reconSel) reconSel.onchange = reloadNav;

  const btnNew = $('acc-btn-new-nav');
  if (btnNew) btnNew.onclick = () => _accOpenNAVForm();

  await reloadNav();
}

function _renderAccNAV() {
  const el = $('acc-nav-records-list');
  if (!el) return;
  if (!_accNav.length) { el.innerHTML = '<div class="acc-empty">No NAV records found. Create one using the button above.</div>'; return; }
  el.innerHTML = _accNav.map(nav => {
    const reconBadge = nav.depository_reconciled
      ? `<span class="reconciled-yes">&#10003; ${esc(nav.depository_type?.toUpperCase() || 'CDSL')} Reconciled</span>`
      : `<span class="reconciled-no">&#9888; Unreconciled</span>`;
    return `
      <div class="nav-card">
        <div class="nav-card-header">
          <div>
            <div class="nav-card-title">${esc(nav.scheme_name || '—')} &middot; ${_accDate(nav.nav_date)}</div>
            <div class="nav-card-meta">Depository: ${esc(nav.depository_type || '—')} &middot; Posted: ${_accDate(nav.created_at)}</div>
          </div>
          ${reconBadge}
        </div>
        <div class="nav-card-metrics">
          <div class="nav-metric"><span class="label">Total NAV</span><span class="value">${_accFmt(nav.total_nav)}</span></div>
          <div class="nav-metric"><span class="label">Units Outstanding</span><span class="value">${nav.total_units_outstanding ? parseFloat(nav.total_units_outstanding).toLocaleString('en-IN',{maximumFractionDigits:4}) : '—'}</span></div>
          <div class="nav-metric"><span class="label">NAV per Unit</span><span class="value highlight">&#8377;${nav.nav_per_unit ? parseFloat(nav.nav_per_unit).toFixed(4) : '—'}</span></div>
          ${nav.depository_variance_amount ? `<div class="nav-metric"><span class="label">Variance</span><span class="value" style="color:var(--accent-red)">${_accFmt(nav.depository_variance_amount)}</span></div>` : ''}
        </div>
        <div class="nav-breakdown">
          <div class="breakdown-item"><span class="label">Investments (FV)</span><span class="value">${_accFmt(nav.investments_at_fair_value)}</span></div>
          <div class="breakdown-item"><span class="label">Cash &amp; Equivalents</span><span class="value">${_accFmt(nav.cash_and_equivalents)}</span></div>
          <div class="breakdown-item"><span class="label">Receivables</span><span class="value">${_accFmt(nav.receivables)}</span></div>
          <div class="breakdown-item"><span class="label">Mgmt Fee Payable</span><span class="value" style="color:var(--accent-red)">${_accFmt(nav.management_fee_payable)}</span></div>
          <div class="breakdown-item"><span class="label">Other Liabilities</span><span class="value" style="color:var(--accent-red)">${_accFmt(nav.other_liabilities)}</span></div>
        </div>
        <div class="card-actions" style="margin-top:12px;">
          <button class="btn-action" onclick="(async()=>{try{const d=await Auth.apiGet('/accounting/nav/${nav.id}/');_accOpenNAVForm(d);}catch(e){_accOpenNAVForm(${JSON.stringify(JSON.stringify(nav)).slice(1,-1)});}})()">Edit</button>
        </div>
      </div>`;
  }).join('');
}

function _accOpenNAVForm(existing = null) {
  const isEdit = !!existing;
  const schemeOpts = [{value:'',label:'— Select Scheme —'}].concat(_accSchemes.map(s => ({value:s.id,label:`${s.fund_name} → ${s.name}`})));
  _accOpenModal(isEdit ? 'Edit NAV Record' : 'Record NAV', [
    {name:'scheme', label:'Scheme', type:'select', required:true, options:schemeOpts, def:existing?.scheme||''},
    {name:'nav_date', label:'NAV Date', type:'date', required:true, def:existing?.nav_date||''},
    {name:'total_nav', label:'Total NAV (₹)', type:'number', required:true, step:'0.01', def:existing?.total_nav||''},
    {name:'total_units_outstanding', label:'Units Outstanding', type:'number', required:true, step:'0.0001', def:existing?.total_units_outstanding||''},
    {name:'nav_per_unit', label:'NAV per Unit (₹)', type:'number', required:true, step:'0.0001', def:existing?.nav_per_unit||''},
    {name:'investments_at_fair_value', label:'Investments at FV (₹)', type:'number', step:'0.01', def:existing?.investments_at_fair_value||''},
    {name:'cash_and_equivalents', label:'Cash & Equivalents (₹)', type:'number', step:'0.01', def:existing?.cash_and_equivalents||''},
    {name:'receivables', label:'Receivables (₹)', type:'number', step:'0.01', def:existing?.receivables||''},
    {name:'management_fee_payable', label:'Mgmt Fee Payable (₹)', type:'number', step:'0.01', def:existing?.management_fee_payable||''},
    {name:'other_liabilities', label:'Other Liabilities (₹)', type:'number', step:'0.01', def:existing?.other_liabilities||''},
    {name:'depository_type', label:'Depository', type:'select', def:existing?.depository_type||'cdsl', options:[{value:'cdsl',label:'CDSL'},{value:'nsdl',label:'NSDL'}]},
    {name:'depository_variance_amount', label:'Depository Variance (₹)', type:'number', step:'0.01', def:existing?.depository_variance_amount||''},
  ], async (data) => {
    const variance = parseFloat(data.depository_variance_amount || 0);
    data.depository_reconciled = variance === 0;
    if (isEdit) await Auth.apiPut(`/accounting/nav/${existing.id}/`, data);
    else await Auth.apiPost('/accounting/nav/', data);
    await loadAccNAVRecords();
  });
}

// ── CARRIED INTEREST ─────────────────────────────────────────
async function loadAccCarried() {
  await _loadAccSchemes();

  const reload = async () => {
    const params = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    try {
      const raw = await Auth.apiGet('/accounting/carry/' + params);
      _accCarry = raw.results || raw || [];
    } catch(e) { _accCarry = []; }
    _renderAccCarried();
  };

  const btnNew = $('acc-btn-new-carry');
  if (btnNew) btnNew.onclick = () => _accOpenCarryForm();
  await reload();
}

function _renderAccCarried() {
  const el = $('acc-carried-list');
  if (!el) return;
  if (!_accCarry.length) { el.innerHTML = '<div class="acc-empty">No carried interest records found.</div>'; return; }
  el.innerHTML = _accCarry.map(carry => `
    <div class="carry-card">
      <div class="carry-card-header">
        <div>
          <div class="carry-card-title">${esc(carry.scheme_name || '—')} &middot; Carry Calculation</div>
          <div class="carry-card-meta">Date: ${_accDate(carry.calculation_date)}</div>
        </div>
        <span class="carry-badge carry-${carry.calculation_status||'indicative'}">${esc(carry.status_display || carry.calculation_status || '—')}</span>
      </div>
      <div class="waterfall-grid">
        <div class="waterfall-step"><div class="step-label">Total Distributions</div><div class="step-value">${_accFmt(carry.total_distributions)}</div></div>
        <div class="waterfall-step"><div class="step-label">Called Capital</div><div class="step-value">${_accFmt(carry.total_called_capital)}</div></div>
        <div class="waterfall-step"><div class="step-label">Preferred Return</div><div class="step-value">${_accFmt(carry.preferred_return_amount)}</div></div>
        <div class="waterfall-step"><div class="step-label">Carry Base</div><div class="step-value">${_accFmt(carry.carry_base)}</div></div>
        <div class="waterfall-step carry-highlight"><div class="step-label">Gross Carry</div><div class="step-value">${_accFmt(carry.carry_amount_gross)}</div></div>
        <div class="waterfall-step carry-highlight"><div class="step-label">Net Carry (after clawback)</div><div class="step-value">${_accFmt(carry.carry_amount_net)}</div></div>
        ${carry.gp_clawback_provision ? `<div class="waterfall-step"><div class="step-label">GP Clawback Provision</div><div class="step-value" style="color:var(--accent-red)">${_accFmt(carry.gp_clawback_provision)}</div></div>` : ''}
      </div>
      ${carry.notes ? `<p style="font-size:12px;color:var(--text3);margin-bottom:12px;">${esc(carry.notes)}</p>` : ''}
      <div class="card-actions">
        <button class="btn-action" onclick="_accOpenCarryForm(${JSON.stringify(JSON.stringify(carry)).slice(1,-1)})">Edit</button>
      </div>
    </div>`).join('');
}

function _accOpenCarryForm(existing = null) {
  if (typeof existing === 'string') { try { existing = JSON.parse(existing); } catch(e) { existing = null; } }
  const isEdit = !!existing;
  const schemeOpts = [{value:'',label:'— Select Scheme —'}].concat(_accSchemes.map(s => ({value:s.id,label:`${s.fund_name} → ${s.name}`})));
  _accOpenModal(isEdit ? 'Edit Carry Calculation' : 'Compute Carried Interest', [
    {name:'scheme', label:'Scheme', type:'select', required:true, options:schemeOpts, def:existing?.scheme||''},
    {name:'calculation_date', label:'Calculation Date', type:'date', required:true, def:existing?.calculation_date||''},
    {name:'total_distributions', label:'Total Distributions (₹)', type:'number', required:true, step:'0.01', def:existing?.total_distributions||''},
    {name:'total_called_capital', label:'Total Called Capital (₹)', type:'number', required:true, step:'0.01', def:existing?.total_called_capital||''},
    {name:'preferred_return_amount', label:'Preferred Return (₹)', type:'number', step:'0.01', def:existing?.preferred_return_amount||''},
    {name:'carry_base', label:'Carry Base (₹)', type:'number', step:'0.01', def:existing?.carry_base||''},
    {name:'carry_amount_gross', label:'Gross Carry (₹)', type:'number', step:'0.01', def:existing?.carry_amount_gross||''},
    {name:'carry_amount_net', label:'Net Carry (₹)', type:'number', step:'0.01', def:existing?.carry_amount_net||''},
    {name:'gp_clawback_provision', label:'GP Clawback Provision (₹)', type:'number', step:'0.01', def:existing?.gp_clawback_provision||''},
    {name:'calculation_status', label:'Status', type:'select', def:existing?.calculation_status||'indicative', options:[{value:'indicative',label:'Indicative'},{value:'crystallised',label:'Crystallised'},{value:'paid',label:'Paid'}]},
    {name:'notes', label:'Notes', type:'textarea', def:existing?.notes||''},
  ], async (data) => {
    if (isEdit) await Auth.apiPut(`/accounting/carry/${existing.id}/`, data);
    else await Auth.apiPost('/accounting/carry/', data);
    await loadAccCarried();
  });
}

// ── FUND LEDGER ───────────────────────────────────────────────
async function loadAccLedger() {
  await _loadAccSchemes();

  const reload = async () => {
    const refType = $('acc-ledger-reftype')?.value || '';
    const params  = [];
    if (_ctx.fundId) params.push(`fund=${_ctx.fundId}`);
    if (refType) params.push(`reference_type=${refType}`);
    try {
      const raw = await Auth.apiGet('/accounting/ledger/' + (params.length ? '?' + params.join('&') : ''));
      _accLedger = raw.results || raw || [];
    } catch(e) { _accLedger = []; }
    _renderAccLedger();
  };

  const refSel = $('acc-ledger-reftype');
  if (refSel) refSel.onchange = reload;
  const btnNew = $('acc-btn-new-entry');
  if (btnNew) btnNew.onclick = () => _accOpenLedgerForm();
  await reload();
}

function _renderAccLedger() {
  const tbody = $('acc-ledger-tbody');
  if (!tbody) return;
  if (!_accLedger.length) { tbody.innerHTML = '<tr><td colspan="8" class="acc-empty">No journal entries found.</td></tr>'; return; }
  tbody.innerHTML = _accLedger.map(entry => `
    <tr ${entry.is_reversed ? 'class="ledger-reversed"' : ''}>
      <td class="ledger-entry-no">${esc(entry.journal_entry_number||'—')}</td>
      <td style="font-size:12px;">${_accDate(entry.entry_date)}</td>
      <td style="font-size:12px;max-width:200px;">${esc(entry.description)}</td>
      <td style="font-size:12px;">${esc(entry.debit_account_name||'—')}</td>
      <td style="font-size:12px;">${esc(entry.credit_account_name||'—')}</td>
      <td class="ledger-amount">${_accFmt(entry.amount)}</td>
      <td style="font-size:11px;font-family:var(--font-mono);">${esc(entry.reference_type_display||entry.reference_type||'—')}</td>
      <td>${entry.is_reversed ? '<span class="fee-badge fee-waived">REVERSED</span>' : '<span class="fee-badge fee-paid">POSTED</span>'}</td>
    </tr>`).join('');
}

function _accOpenLedgerForm() {
  const schemeOpts = [{value:'',label:'— Select Scheme —'}].concat(_accSchemes.map(s => ({value:s.id,label:`${s.fund_name} → ${s.name}`})));
  const acctOpts   = [{value:'',label:'— Select Account —'}].concat(_accCOA.map(a => ({value:a.id,label:`${a.account_code} — ${a.account_name}`})));
  _accOpenModal('Post Journal Entry', [
    {name:'scheme', label:'Scheme', type:'select', required:true, options:schemeOpts},
    {name:'entry_date', label:'Entry Date', type:'date', required:true},
    {name:'description', label:'Description', type:'textarea', required:true},
    {name:'debit_account', label:'Debit Account', type:'select', required:true, options:acctOpts},
    {name:'credit_account', label:'Credit Account', type:'select', required:true, options:acctOpts},
    {name:'amount', label:'Amount (₹)', type:'number', required:true, step:'0.01'},
    {name:'reference_type', label:'Reference Type', type:'select', def:'other', options:[
      {value:'capital_call',label:'Capital Call'},{value:'distribution',label:'Distribution'},
      {value:'investment',label:'Investment'},{value:'valuation',label:'Valuation'},
      {value:'management_fee',label:'Management Fee'},{value:'carried_interest',label:'Carried Interest'},
      {value:'other',label:'Other'},
    ]},
    {name:'reference_id', label:'Reference ID (UUID, optional)', placeholder:'UUID of linked object'},
  ], async (data) => {
    if (!data.reference_id) delete data.reference_id;
    await Auth.apiPost('/accounting/ledger/', data);
    await loadAccLedger();
  });
}

// ── MANAGEMENT FEES ───────────────────────────────────────────
async function loadAccFees() {
  await _loadAccSchemes();

  const reload = async () => {
    const params = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    try {
      const raw = await Auth.apiGet('/accounting/fees/' + params);
      _accFees = raw.results || raw || [];
    } catch(e) { _accFees = []; }
    _renderAccFees();
  };

  const btnNew = $('acc-btn-new-fee');
  if (btnNew) btnNew.onclick = () => _accOpenFeeForm();
  await reload();
}

function _renderAccFees() {
  const tbody = $('acc-fee-tbody');
  if (!tbody) return;
  if (!_accFees.length) { tbody.innerHTML = '<tr><td colspan="10" class="acc-empty">No fee periods found.</td></tr>'; return; }
  tbody.innerHTML = _accFees.map(fee => `
    <tr>
      <td style="font-weight:600;font-size:13px;">${esc(fee.scheme_name||'—')}</td>
      <td style="font-size:12px;font-family:var(--font-mono);">${_accDate(fee.period_start)} → ${_accDate(fee.period_end)}</td>
      <td style="font-family:var(--font-mono);font-size:12px;">${_accFmt(fee.fee_basis_amount)}</td>
      <td style="font-family:var(--font-mono);">${fee.fee_rate ? parseFloat(fee.fee_rate).toFixed(4)+'%' : '—'}</td>
      <td style="font-family:var(--font-mono);font-weight:600;">${_accFmt(fee.fee_amount)}</td>
      <td style="font-family:var(--font-mono);color:var(--text3);">${_accFmt(fee.gst_amount)}</td>
      <td style="font-family:var(--font-mono);font-weight:700;color:var(--accent);">${_accFmt(fee.total_fee_with_gst)}</td>
      <td style="font-size:11px;font-family:var(--font-mono);">${esc(fee.invoice_number)||'—'}</td>
      <td><span class="fee-badge fee-${fee.fee_status||'draft'}">${esc(fee.status_display||fee.fee_status||'—')}</span></td>
      <td><button class="btn-action" onclick="_accOpenFeeForm(${JSON.stringify(JSON.stringify(fee)).slice(1,-1)})">Edit</button></td>
    </tr>`).join('');
}

function _accOpenFeeForm(existing = null) {
  if (typeof existing === 'string') { try { existing = JSON.parse(existing); } catch(e) { existing = null; } }
  const isEdit = !!existing;
  const schemeOpts = [{value:'',label:'— Select Scheme —'}].concat(_accSchemes.map(s => ({value:s.id,label:`${s.fund_name} → ${s.name}`})));
  _accOpenModal(isEdit ? 'Edit Fee Period' : 'Add Fee Period', [
    {name:'scheme', label:'Scheme', type:'select', required:true, options:schemeOpts, def:existing?.scheme||''},
    {name:'period_start', label:'Period Start', type:'date', required:true, def:existing?.period_start||''},
    {name:'period_end', label:'Period End', type:'date', required:true, def:existing?.period_end||''},
    {name:'fee_basis_amount', label:'Fee Basis Amount (₹)', type:'number', required:true, step:'0.01', def:existing?.fee_basis_amount||''},
    {name:'fee_rate', label:'Fee Rate (%)', type:'number', required:true, step:'0.0001', def:existing?.fee_rate||''},
    {name:'fee_amount', label:'Fee Amount (₹)', type:'number', step:'0.01', def:existing?.fee_amount||''},
    {name:'gst_amount', label:'GST Amount (₹)', type:'number', step:'0.01', def:existing?.gst_amount||''},
    {name:'total_fee_with_gst', label:'Total with GST (₹)', type:'number', step:'0.01', def:existing?.total_fee_with_gst||''},
    {name:'fee_status', label:'Status', type:'select', def:existing?.fee_status||'draft', options:[
      {value:'draft',label:'Draft'},{value:'invoiced',label:'Invoiced'},
      {value:'paid',label:'Paid'},{value:'waived',label:'Waived'},
    ]},
    {name:'invoice_number', label:'Invoice Number', def:existing?.invoice_number||''},
    {name:'invoice_date', label:'Invoice Date', type:'date', def:existing?.invoice_date||''},
  ], async (data) => {
    if (!data.invoice_date) delete data.invoice_date;
    if (isEdit) await Auth.apiPut(`/accounting/fees/${existing.id}/`, data);
    else await Auth.apiPost('/accounting/fees/', data);
    await loadAccFees();
  });
}

// ── CHART OF ACCOUNTS ─────────────────────────────────────────
async function loadAccCOA() {
  await _loadAccSchemes();
  const typeSel = $('acc-coa-type');
  if (typeSel) typeSel.onchange = _renderAccCOA;
  const btnNew = $('acc-btn-new-account');
  if (btnNew) btnNew.onclick = () => _accOpenCOAForm();
  try {
    const raw = await Auth.apiGet('/accounting/chart-of-accounts/');
    _accCOA = raw.results || raw || [];
  } catch(e) { _accCOA = []; }
  _renderAccCOA();
}

function _renderAccCOA() {
  const typeFilter = $('acc-coa-type')?.value || '';
  const list = typeFilter ? _accCOA.filter(a => a.account_type === typeFilter) : _accCOA;
  const tbody = $('acc-coa-tbody');
  if (!tbody) return;
  if (!list.length) { tbody.innerHTML = '<tr><td colspan="7" class="acc-empty">No accounts found.</td></tr>'; return; }
  tbody.innerHTML = list.map(acc => `
    <tr>
      <td style="font-family:var(--font-mono);font-size:12px;font-weight:700;">${esc(acc.account_code)}</td>
      <td style="font-weight:600;">${esc(acc.account_name)}</td>
      <td><span class="acc-type-badge acc-type-${acc.account_type}">${esc(acc.account_type_display||acc.account_type)}</span></td>
      <td style="font-size:12px;">${esc(acc.parent_account_name)||'—'}</td>
      <td style="font-size:12px;color:var(--text3);max-width:200px;">${esc(acc.description)||'—'}</td>
      <td>${acc.is_active ? '<span class="fee-badge fee-paid">Active</span>' : '<span class="fee-badge fee-waived">Inactive</span>'}</td>
      <td><button class="btn-action" onclick="_accOpenCOAForm(${JSON.stringify(JSON.stringify(acc)).slice(1,-1)})">Edit</button></td>
    </tr>`).join('');
}

function _accOpenCOAForm(existing = null) {
  if (typeof existing === 'string') { try { existing = JSON.parse(existing); } catch(e) { existing = null; } }
  const isEdit = !!existing;
  const parentOpts = [{value:'',label:'— None (Top Level) —'}].concat(
    _accCOA.filter(a => !existing || a.id !== existing.id).map(a => ({value:a.id,label:`${a.account_code} — ${a.account_name}`}))
  );
  _accOpenModal(isEdit ? 'Edit Account' : 'Add Account', [
    {name:'account_code', label:'Account Code', required:true, placeholder:'e.g. 1001', def:existing?.account_code||''},
    {name:'account_name', label:'Account Name', required:true, def:existing?.account_name||''},
    {name:'account_type', label:'Account Type', type:'select', required:true, def:existing?.account_type||'asset', options:[
      {value:'asset',label:'Asset'},{value:'liability',label:'Liability'},{value:'equity',label:'Equity'},
      {value:'income',label:'Income'},{value:'expense',label:'Expense'},
    ]},
    {name:'parent_account', label:'Parent Account (optional)', type:'select', options:parentOpts, def:existing?.parent_account||''},
    {name:'description', label:'Description', type:'textarea', def:existing?.description||''},
  ], async (data) => {
    if (!data.parent_account) delete data.parent_account;
    if (isEdit) await Auth.apiPut(`/accounting/chart-of-accounts/${existing.id}/`, data);
    else await Auth.apiPost('/accounting/chart-of-accounts/', data);
    await loadAccCOA();
  });
}

// ── TRIAL BALANCE ─────────────────────────────────────────────
async function loadAccTrialBalanceUI() {
  const btnGen = $('acc-btn-gen-tb');
  if (btnGen) btnGen.onclick = _generateAccTrialBalance;
}

async function _generateAccTrialBalance() {
  const schemeId = _ctx.schemeIds?.[0] || null;
  const asOfDate = $('acc-tb-date')?.value;
  const output   = $('acc-tb-output');
  if (!output) return;
  if (!schemeId) { output.innerHTML = '<div class="acc-empty">Please select a fund from the top navigation to generate a trial balance.</div>'; return; }
  output.innerHTML = '<div class="acc-empty">Generating trial balance…</div>';
  try {
    const url = `/accounting/schemes/${schemeId}/trial-balance/` + (asOfDate ? `?as_of=${asOfDate}` : '');
    const data = await Auth.apiGet(url);
    _renderAccTrialBalance(data, asOfDate);
  } catch(e) {
    _renderAccTrialBalanceFallback(schemeId, asOfDate);
  }
}

function _renderAccTrialBalance(data, asOfDate) {
  const output = $('acc-tb-output');
  if (!output) return;
  const rows = data.accounts || data || [];
  if (!rows.length) { output.innerHTML = '<div class="acc-empty">No ledger entries for this scheme.</div>'; return; }
  const totalDebit  = rows.reduce((s,r) => s + parseFloat(r.total_debit  || 0), 0);
  const totalCredit = rows.reduce((s,r) => s + parseFloat(r.total_credit || 0), 0);
  const balanced = Math.abs(totalDebit - totalCredit) < 0.01;
  output.innerHTML = `
    <div class="tb-header">
      <span class="tb-title">Trial Balance${asOfDate ? ' — as of ' + _accDate(asOfDate) : ''}</span>
      <span class="tb-balance-badge ${balanced ? 'balanced' : 'unbalanced'}">
        ${balanced ? '&#10003; Balanced' : '&#9888; Out of Balance by ' + _accFmt(Math.abs(totalDebit - totalCredit))}
      </span>
    </div>
    <div class="acc-table-wrap">
      <table class="acc-table">
        <thead><tr><th>Account Code</th><th>Account Name</th><th>Type</th><th style="text-align:right;">Debit</th><th style="text-align:right;">Credit</th></tr></thead>
        <tbody>
          ${rows.map(r => `<tr>
            <td style="font-family:var(--font-mono);font-size:12px;">${esc(r.account_code)}</td>
            <td>${esc(r.account_name)}</td>
            <td><span class="acc-type-badge acc-type-${r.account_type}">${esc(r.account_type_display||r.account_type)}</span></td>
            <td style="text-align:right;font-family:var(--font-mono);">${r.total_debit  ? _accFmt(r.total_debit)  : '—'}</td>
            <td style="text-align:right;font-family:var(--font-mono);">${r.total_credit ? _accFmt(r.total_credit) : '—'}</td>
          </tr>`).join('')}
          <tr class="tb-totals-row">
            <td colspan="3" style="font-weight:700;">TOTALS</td>
            <td style="text-align:right;font-weight:700;font-family:var(--font-mono);">${_accFmt(totalDebit)}</td>
            <td style="text-align:right;font-weight:700;font-family:var(--font-mono);">${_accFmt(totalCredit)}</td>
          </tr>
        </tbody>
      </table>
    </div>`;
}

function _renderAccTrialBalanceFallback(schemeId, asOfDate) {
  const entries = _accLedger.filter(e => {
    if (String(e.scheme) !== String(schemeId)) return false;
    if (asOfDate && e.entry_date > asOfDate) return false;
    return true;
  });
  if (!entries.length) { const o=$('acc-tb-output'); if(o) o.innerHTML='<div class="acc-empty">No ledger entries for this scheme. Post journal entries first.</div>'; return; }
  const accMap = {};
  const getA = (id) => {
    if (!id) return null;
    if (!accMap[id]) {
      const a = _accCOA.find(c => String(c.id) === String(id)) || {account_code:id,account_name:'(Unknown)',account_type:'asset'};
      accMap[id] = {...a, total_debit:0, total_credit:0};
    }
    return accMap[id];
  };
  entries.forEach(e => {
    const da = getA(e.debit_account);
    const ca = getA(e.credit_account);
    const amt = parseFloat(e.amount || 0);
    if (da) da.total_debit  += amt;
    if (ca) ca.total_credit += amt;
  });
  const rows = Object.values(accMap).filter(a => a.total_debit > 0 || a.total_credit > 0);
  _renderAccTrialBalance({accounts: rows}, asOfDate);
}

// ── FINANCIAL STATEMENTS ──────────────────────────────────────
async function loadAccFinStatementsUI() {
  const btnGen = $('acc-btn-gen-fin');
  if (btnGen) btnGen.onclick = _generateAccFinancials;
  const btnExp = $('acc-btn-export-fin');
  if (btnExp) btnExp.onclick = _exportAccFinPDF;
}

async function _generateAccFinancials() {
  const schemeId = _ctx.schemeIds?.[0] || null;
  const stmtType = $('acc-fin-type')?.value || 'bs';
  const output   = $('acc-fin-output');
  if (!output) return;
  if (!schemeId) { output.innerHTML = '<div class="acc-empty">Please select a fund from the top navigation to generate a financial statement.</div>'; return; }
  output.innerHTML = '<div class="acc-empty">Generating financial statement…</div>';
  try {
    const data = await Auth.apiGet(`/accounting/schemes/${schemeId}/financials/${stmtType}/`);
    _renderAccFinancials(stmtType, data);
  } catch(e) {
    _renderAccFinancialsFallback(schemeId, stmtType);
  }
}

function _renderAccFinancials(type, data) {
  const output = $('acc-fin-output');
  if (!output) return;
  const titles = {bs:'Balance Sheet', is:'Income Statement', cf:'Cash Flow Statement'};
  const title = titles[type] || 'Financial Statement';

  if (type === 'bs') {
    const assets = data.assets || [], liab = data.liabilities || [], equity = data.equity || [];
    const totA = data.total_assets || assets.reduce((s,r)=>s+parseFloat(r.balance||0),0);
    const totL = data.total_liabilities || liab.reduce((s,r)=>s+parseFloat(r.balance||0),0);
    const totE = data.total_equity || equity.reduce((s,r)=>s+parseFloat(r.balance||0),0);
    output.innerHTML = `
      <div class="fin-header"><span class="fin-title">${title}</span><span class="fin-subtitle">Ind AS 109 Compliant</span></div>
      <div class="fin-columns">
        <div class="fin-section">
          <div class="fin-section-title">Assets</div>
          ${assets.map(r=>`<div class="fin-row"><span>${esc(r.account_name)}</span><span class="mono">${_accFmt(r.balance)}</span></div>`).join('')||'<div class="fin-row muted">No asset accounts</div>'}
          <div class="fin-row fin-total"><span>Total Assets</span><span class="mono">${_accFmt(totA)}</span></div>
        </div>
        <div class="fin-section">
          <div class="fin-section-title">Liabilities</div>
          ${liab.map(r=>`<div class="fin-row"><span>${esc(r.account_name)}</span><span class="mono">${_accFmt(r.balance)}</span></div>`).join('')||'<div class="fin-row muted">No liability accounts</div>'}
          <div class="fin-row fin-total"><span>Total Liabilities</span><span class="mono">${_accFmt(totL)}</span></div>
          <div class="fin-section-title" style="margin-top:24px;">Equity</div>
          ${equity.map(r=>`<div class="fin-row"><span>${esc(r.account_name)}</span><span class="mono">${_accFmt(r.balance)}</span></div>`).join('')||'<div class="fin-row muted">No equity accounts</div>'}
          <div class="fin-row fin-total"><span>Total Equity</span><span class="mono">${_accFmt(totE)}</span></div>
          <div class="fin-row fin-total fin-grand-total"><span>Total Liabilities + Equity</span><span class="mono">${_accFmt(totL+totE)}</span></div>
        </div>
      </div>`;
  } else if (type === 'is') {
    const income = data.income || [], expenses = data.expenses || [];
    const totI = data.total_income   || income.reduce((s,r)=>s+parseFloat(r.balance||0),0);
    const totX = data.total_expenses || expenses.reduce((s,r)=>s+parseFloat(r.balance||0),0);
    const net  = totI - totX;
    output.innerHTML = `
      <div class="fin-header"><span class="fin-title">${title}</span><span class="fin-subtitle">Ind AS 109 Compliant</span></div>
      <div class="fin-section" style="max-width:700px;">
        <div class="fin-section-title">Income</div>
        ${income.map(r=>`<div class="fin-row"><span>${esc(r.account_name)}</span><span class="mono">${_accFmt(r.balance)}</span></div>`).join('')||'<div class="fin-row muted">No income recorded</div>'}
        <div class="fin-row fin-total"><span>Total Income</span><span class="mono">${_accFmt(totI)}</span></div>
        <div class="fin-section-title" style="margin-top:24px;">Expenses</div>
        ${expenses.map(r=>`<div class="fin-row"><span>${esc(r.account_name)}</span><span class="mono" style="color:var(--accent-red)">${_accFmt(r.balance)}</span></div>`).join('')||'<div class="fin-row muted">No expenses recorded</div>'}
        <div class="fin-row fin-total"><span>Total Expenses</span><span class="mono" style="color:var(--accent-red)">${_accFmt(totX)}</span></div>
        <div class="fin-row fin-total fin-grand-total" style="border-top:2px solid var(--accent-cyan);">
          <span>Net Income / (Loss)</span>
          <span class="mono" style="color:${net>=0?'var(--accent-green)':'var(--accent-red)'};">${_accFmt(Math.abs(net))}${net<0?' (Loss)':''}</span>
        </div>
      </div>`;
  } else if (type === 'cf') {
    const op = data.operating || [], inv = data.investing || [], fin = data.financing || [];
    const netCF = parseFloat(data.net_cash_flow || 0);
    const cfRow = (r) => `<div class="fin-row"><span>${esc(r.description)}</span><span class="mono">${r.amount<0?'(':''}${_accFmt(Math.abs(r.amount))}${r.amount<0?')':''}</span></div>`;
    output.innerHTML = `
      <div class="fin-header"><span class="fin-title">${title}</span><span class="fin-subtitle">Ind AS 7 Compliant</span></div>
      <div class="fin-section" style="max-width:700px;">
        <div class="fin-section-title">Operating Activities</div>
        ${op.map(cfRow).join('')||'<div class="fin-row muted">No operating flows</div>'}
        <div class="fin-section-title" style="margin-top:24px;">Investing Activities</div>
        ${inv.map(cfRow).join('')||'<div class="fin-row muted">No investing flows</div>'}
        <div class="fin-section-title" style="margin-top:24px;">Financing Activities</div>
        ${fin.map(cfRow).join('')||'<div class="fin-row muted">No financing flows</div>'}
        <div class="fin-row fin-total fin-grand-total" style="margin-top:16px;border-top:2px solid var(--accent-cyan);">
          <span>Net Change in Cash</span>
          <span class="mono" style="color:${netCF>=0?'var(--accent-green)':'var(--accent-red)'};">${_accFmt(Math.abs(netCF))} ${netCF<0?'(Outflow)':'(Inflow)'}</span>
        </div>
      </div>`;
  }
}

function _renderAccFinancialsFallback(schemeId, type) {
  const schemeEntries = _accLedger.filter(e => String(e.scheme) === String(schemeId));
  const output = $('acc-fin-output');
  if (!schemeEntries.length) { if(output) output.innerHTML='<div class="acc-empty">No ledger entries found for this scheme. Post journal entries first.</div>'; return; }
  const accBalance = {};
  schemeEntries.forEach(e => {
    const amt = parseFloat(e.amount || 0);
    if (e.debit_account)  accBalance[e.debit_account]  = (accBalance[e.debit_account]  || 0) + amt;
    if (e.credit_account) accBalance[e.credit_account] = (accBalance[e.credit_account] || 0) - amt;
  });
  const rows = Object.entries(accBalance).map(([id, bal]) => {
    const acc = _accCOA.find(a => String(a.id) === String(id)) || {account_name:id, account_type:'asset'};
    return {...acc, balance: Math.abs(bal)};
  });
  const byType = (t) => rows.filter(r => r.account_type === t);
  _renderAccFinancials(type, {assets:byType('asset'),liabilities:byType('liability'),equity:byType('equity'),income:byType('income'),expenses:byType('expense')});
}

function _exportAccFinPDF() {
  const schemeId = _ctx.schemeIds?.[0] || null;
  const stmtType = $('acc-fin-type')?.value || 'bs';
  const output   = $('acc-fin-output');
  if (!schemeId) { alert('Please select a fund from the top navigation first.'); return; }
  if (!output || !output.innerHTML.trim() || output.querySelector('.acc-empty')) { alert('Please generate the financial statement before exporting.'); return; }
  const titles = {bs:'Balance Sheet',is:'Income Statement',cf:'Cash Flow Statement'};
  const scheme  = _accSchemes.find(s => String(s.id) === String(schemeId));
  const pw = window.open('', '_blank');
  pw.document.write(`<!DOCTYPE html><html><head><title>${titles[stmtType]} — ${scheme?.name||'Scheme'}</title>
    <style>body{font-family:sans-serif;margin:40px;color:#111}h1{font-size:20px;margin-bottom:4px}h2{font-size:14px;color:#555;margin-bottom:24px}
    .fin-row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #eee}
    .fin-total{font-weight:bold;border-top:2px solid #333}.fin-grand-total{border-top:3px double #333;font-size:15px}
    .fin-section-title{font-weight:bold;margin-top:20px;margin-bottom:8px;color:#333;text-transform:uppercase;font-size:12px;letter-spacing:1px}</style>
    </head><body><h1>${titles[stmtType]}</h1><h2>${scheme?.fund_name||''} — ${scheme?.name||''}</h2>${output.innerHTML}</body></html>`);
  pw.document.close(); pw.print();
}

// ── SHARED MODAL ENGINE ───────────────────────────────────────
let _accModalCallback = null;

function _accOpenModal(title, fields, callback) {
  const overlay = $('acc-modal-overlay');
  const titleEl = $('acc-modal-title');
  const fieldsEl = $('acc-modal-fields');
  if (!overlay || !titleEl || !fieldsEl) return;

  titleEl.textContent = title;
  fieldsEl.innerHTML  = '';
  fields.forEach(f => {
    const div = document.createElement('div');
    div.style.cssText = 'display:flex;flex-direction:column;gap:4px;';
    const label = document.createElement('label');
    label.textContent = f.label || f.name;
    label.style.cssText = 'font-size:11px;color:var(--text3,#64748b);text-transform:uppercase;letter-spacing:0.5px;';
    div.appendChild(label);
    let ctrl;
    if (f.type === 'select') {
      ctrl = document.createElement('select');
      ctrl.name = f.name;
      if (f.required) ctrl.required = true;
      ctrl.style.cssText = 'padding:8px 10px;background:var(--bg-input,#0d1117);border:1px solid var(--border,#1e2433);border-radius:6px;color:var(--text1,#e2e8f0);font-size:13px;';
      (f.options || []).forEach(o => {
        const opt = document.createElement('option');
        opt.value = o.value; opt.textContent = o.label;
        if (String(o.value) === String(f.def || '')) opt.selected = true;
        ctrl.appendChild(opt);
      });
    } else if (f.type === 'textarea') {
      ctrl = document.createElement('textarea');
      ctrl.name = f.name; ctrl.rows = 3;
      if (f.required) ctrl.required = true;
      ctrl.value = f.def || '';
      ctrl.placeholder = f.placeholder || '';
      ctrl.style.cssText = 'padding:8px 10px;background:var(--bg-input,#0d1117);border:1px solid var(--border,#1e2433);border-radius:6px;color:var(--text1,#e2e8f0);font-size:13px;resize:vertical;';
    } else {
      ctrl = document.createElement('input');
      ctrl.type = f.type || 'text'; ctrl.name = f.name;
      if (f.required) ctrl.required = true;
      if (f.step) ctrl.step = f.step;
      ctrl.value = f.def || '';
      ctrl.placeholder = f.placeholder || '';
      ctrl.style.cssText = 'padding:8px 10px;background:var(--bg-input,#0d1117);border:1px solid var(--border,#1e2433);border-radius:6px;color:var(--text1,#e2e8f0);font-size:13px;';
    }
    div.appendChild(ctrl);
    fieldsEl.appendChild(div);
  });

  _accModalCallback = callback;
  overlay.style.display = 'flex';

  $('acc-modal-close').onclick  = _accCloseModal;
  $('acc-modal-cancel').onclick = _accCloseModal;
  $('acc-modal-form').onsubmit  = _accHandleModalSubmit;
}

function _accCloseModal() {
  const overlay = $('acc-modal-overlay');
  if (overlay) overlay.style.display = 'none';
  _accModalCallback = null;
}

async function _accHandleModalSubmit(e) {
  e.preventDefault();
  if (!_accModalCallback) return;
  const form = $('acc-modal-form');
  const textFields = ['description','notes','account_code','account_name','invoice_number','reference_id'];
  const data = {};
  new FormData(form).forEach((v, k) => {
    if (v === '') return;
    const n = Number(v);
    data[k] = (!isNaN(n) && v.trim() !== '' && !textFields.includes(k)) ? n : v;
  });
  const btn = $('acc-modal-submit');
  if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
  try {
    await _accModalCallback(data);
    _accCloseModal();
  } catch(err) {
    alert('Error: ' + (err.message || 'Save failed'));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Save'; }
  }
}

/* ── Waterfall Simulator (inline in Fund Accounting) ─────── */
function _initWaterfallSim() {
  const schemeSelect = $('wf-sim-scheme');
  if (!schemeSelect) return;

  if (_ctx.schemeIds && _ctx.schemeIds.length) {
    _ctx.schemeIds.forEach(sid => {
      const opt = document.createElement('option');
      opt.value = sid;
      opt.textContent = _ctx.fundName || 'Current Fund';
      schemeSelect.appendChild(opt);
    });
  } else {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = 'All Schemes';
    schemeSelect.appendChild(opt);
  }

  const sliders = [
    { id:'wf-sim-dist',   valId:'wf-sim-dist-val',   fmt: v => `₹${v} Cr` },
    { id:'wf-sim-called', valId:'wf-sim-called-val',  fmt: v => `₹${v} Cr` },
    { id:'wf-sim-hurdle', valId:'wf-sim-hurdle-val',  fmt: v => `${parseFloat(v).toFixed(1)}%` },
    { id:'wf-sim-carry',  valId:'wf-sim-carry-val',   fmt: v => `${v}%` },
    { id:'wf-sim-tenure', valId:'wf-sim-tenure-val',  fmt: v => `${v} years` },
  ];
  sliders.forEach(({ id, valId, fmt }) => {
    const inp = $(id); const disp = $(valId);
    if (!inp || !disp) return;
    disp.textContent = fmt(inp.value);
    inp.oninput = () => { disp.textContent = fmt(inp.value); _runWaterfallSim(); };
  });
  _runWaterfallSim();
}

function _runWaterfallSim() {
  const fmtCrLocal = v => {
    if (Math.abs(v) >= 1e9) return `₹${(v/1e9).toFixed(2)}B`;
    if (Math.abs(v) >= 1e7) return `₹${(v/1e7).toFixed(1)} Cr`;
    if (Math.abs(v) >= 1e5) return `₹${(v/1e5).toFixed(1)} L`;
    return `₹${v.toLocaleString('en-IN', {maximumFractionDigits:0})}`;
  };
  const totalDist = parseFloat($('wf-sim-dist')?.value || 200) * 1e7;
  const calledCap = parseFloat($('wf-sim-called')?.value || 100) * 1e7;
  const hurdlePct = parseFloat($('wf-sim-hurdle')?.value || 8) / 100;
  const carryPct  = parseFloat($('wf-sim-carry')?.value || 20) / 100;
  const tenure    = parseFloat($('wf-sim-tenure')?.value || 7);

  const prefReturn = calledCap * (Math.pow(1 + hurdlePct, tenure) - 1);
  const carryBase  = Math.max(0, totalDist - calledCap - prefReturn);
  const gpCarry    = carryBase * carryPct;
  const lpTotal    = totalDist - gpCarry;
  const moic       = calledCap > 0 ? totalDist / calledCap : 0;
  const lpMoic     = calledCap > 0 ? lpTotal / calledCap : 0;
  const pct = v => totalDist > 0 ? Math.round(v / totalDist * 100) : 0;

  const steps = [
    { name:'Total Distributions',           label:'Gross pool to be distributed',                                    amt:totalDist,  pct:100,         color:'#4a9eff' },
    { name:'Return of Capital (LP)',         label:'Called capital returned to LPs first',                           amt:calledCap,  pct:pct(calledCap),  color:'#4a9eff' },
    { name:'Preferred Return (LP)',          label:`Hurdle: ${(hurdlePct*100).toFixed(1)}% compounded over ${tenure} yrs`, amt:prefReturn,  pct:pct(prefReturn), color:'#f6a623' },
    { name:'Carry Base (GP)',                label:'Profit above hurdle subject to carry',                           amt:carryBase,  pct:pct(carryBase),  color:'#a78bfa' },
    { name:`GP Carried Interest (${Math.round(carryPct*100)}%)`, label:'GP share of carry base',              amt:gpCarry,    pct:pct(gpCarry),    color:'#3ecf8e' },
    { name:'LP Net Proceeds',                label:'Total LP receives',                                              amt:lpTotal,    pct:pct(lpTotal),    color:'#4a9eff' },
  ];

  const stepsEl = $('wf-sim-steps');
  if (stepsEl) {
    stepsEl.innerHTML = steps.map(s => `
      <div class="wf-step" style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--border1)">
        <div style="flex:1;min-width:0">
          <div style="font-size:12px;font-weight:600;color:var(--text1)">${s.name}</div>
          <div style="font-size:10px;color:var(--text3);margin-top:2px">${s.label}</div>
        </div>
        <div style="width:160px;flex-shrink:0">
          <div style="height:8px;background:var(--bg3);border-radius:4px;overflow:hidden">
            <div style="height:100%;width:${s.pct}%;background:${s.color};border-radius:4px;transition:width 0.3s"></div>
          </div>
        </div>
        <div style="font-size:12px;font-weight:700;color:${s.color};width:90px;text-align:right;flex-shrink:0">${fmtCrLocal(Math.max(0,s.amt))}</div>
      </div>`).join('');
  }

  const summaryEl = $('wf-sim-summary');
  if (summaryEl) {
    summaryEl.innerHTML = `
      <div style="display:flex;gap:20px;margin-top:14px;flex-wrap:wrap">
        <div class="v5-kpi-card blue" style="padding:12px;flex:1;min-width:120px">
          <div class="v5-kpi-label">Fund MoIC</div>
          <div class="v5-kpi-value" style="font-size:1.3rem">${moic.toFixed(2)}x</div>
        </div>
        <div class="v5-kpi-card green" style="padding:12px;flex:1;min-width:120px">
          <div class="v5-kpi-label">GP Carry</div>
          <div class="v5-kpi-value" style="font-size:1.3rem">${fmtCrLocal(Math.max(0,gpCarry))}</div>
        </div>
        <div class="v5-kpi-card cyan" style="padding:12px;flex:1;min-width:120px">
          <div class="v5-kpi-label">LP Net MoIC</div>
          <div class="v5-kpi-value" style="font-size:1.3rem">${lpMoic.toFixed(2)}x</div>
        </div>
      </div>`;
  }
}

/* ── Financials (P&L) ──────────────────────────────────────── */
async function loadFinancials() {
  const tbody = $('fin-pl-tbody');
  if (!tbody) return;
  try {
    // Guard: always scope to selected fund to avoid loading cross-fund BvA data
    if (!_ctx.fundId) {
      tbody.innerHTML = '<tr><td colspan="7" class="table-empty">Select a fund to view financials.</td></tr>';
      if ($('fin-pl-count')) $('fin-pl-count').textContent = '';
      return;
    }
    // Fetch BvA records and pivot to per-company P&L (latest period per company)
    const plUrl = `/mis/bva/?fund=${_ctx.fundId}`;
    const data = await Auth.apiGet(plUrl);
    let arr  = data.results || data || [];

    // Fallback: if no per-company BvA data, render fund-level ConsolidatedMIS P&L
    if (!arr.length && _ctx.fundId) {
      await _loadFinancialsFundLevel(tbody);
      return;
    }

    if (!arr.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="table-empty">No financial data. Import financials first.</td></tr>';
      return;
    }

    // Group by company, take latest period, collect key line items
    const byCompany = {};
    for (const r of arr) {
      const cname = r.company_name || String(r.portfolio_company || '—');
      if (!byCompany[cname]) byCompany[cname] = {};
      const periodKey = `${r.period_year || 0}-${String(r.period_month || 0).padStart(2,'0')}`;
      const existing  = byCompany[cname];
      // Keep the record from the latest period
      const existKey  = existing.__latestKey__ || '';
      if (periodKey >= existKey) {
        existing.__latestKey__ = periodKey;
        existing.__period__    = r.period_month
          ? `${['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][r.period_month]}-${r.period_year}`
          : String(r.period_year || '—');
      }
      if (r.actual_inr != null) existing[r.line_item] = parseFloat(r.actual_inr);
    }

    const companies = Object.entries(byCompany)
      .filter(([k]) => !k.startsWith('__'))
      .sort((a, b) => (b[1].revenue || 0) - (a[1].revenue || 0));

    let n = 0;
    tbody.innerHTML = companies.map(([cname, fin]) => {
      const rev    = fin.revenue    ?? fin.total_revenue ?? null;
      const ebitda = fin.ebitda     ?? null;
      const pat    = fin.pat        ?? null;
      const ebitdaPct = (rev && ebitda != null && rev > 0)
        ? (ebitda / rev * 100).toFixed(1) + '%' : '—';
      n++;
      const searchBlob = [cname, fin.__period__].join(' ').toLowerCase();
      return `<tr data-search="${esc(searchBlob)}">
        <td class="row-num td-center">${n}</td>
        <td class="td-bold">${esc(cname)}</td>
        <td class="td-right">${rev    != null ? fmtCr(rev)    : '—'}</td>
        <td class="td-right">${ebitda != null ? fmtCr(ebitda) : '—'}</td>
        <td class="td-right">${pat    != null ? fmtCr(pat)    : '—'}</td>
        <td class="td-right">${ebitdaPct}</td>
        <td>${esc(fin.__period__ || '—')}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="7" class="table-empty">No financial data. Import financials first.</td></tr>';

    const countEl = $('fin-pl-count');
    if (countEl) countEl.textContent = `(${n} companies)`;
  } catch(e) {
    tbody.innerHTML = '<tr><td colspan="7" class="table-empty">No data available.</td></tr>';
  }
}

async function _loadFinancialsFundLevel(tbody) {
  // Render fund-level ConsolidatedMIS monthly P&L when no per-company BvA data exists.
  // Groups rows by period, shows each period as one row with fund-aggregated financials.
  try {
    let url = `/mis/consolidated/?fund=${_ctx.fundId}`;
    const misData = await Auth.apiGet(url);
    const misArr  = misData.results || misData || [];

    const PL_ITEMS = ['revenue', 'total_revenue', 'cogs', 'gross_profit', 'ebitda', 'ebit', 'pbt', 'pat'];
    const plRecs   = misArr.filter(r => PL_ITEMS.includes(r.line_item));

    if (!plRecs.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="table-empty">No financial data. Import financials first.</td></tr>';
      return;
    }

    // Group by period
    const byPeriod = {};
    const MONTHS = ['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    for (const r of plRecs) {
      const periodKey  = `${r.period_year || 0}-${String(r.period_month || 0).padStart(2,'0')}`;
      const periodLabel = r.period_month
        ? `${MONTHS[r.period_month]}-${r.period_year}`
        : String(r.period_year || '—');
      if (!byPeriod[periodKey]) byPeriod[periodKey] = { __label__: periodLabel };
      byPeriod[periodKey][r.line_item] = parseFloat(r.total_actual_inr);
    }

    const periods = Object.entries(byPeriod).sort((a, b) => b[0].localeCompare(a[0]));
    let n = 0;
    tbody.innerHTML = periods.map(([_key, fin]) => {
      const rev    = fin.revenue ?? fin.total_revenue ?? null;
      const ebitda = fin.ebitda  ?? null;
      const pat    = fin.pat     ?? null;
      const ebitdaPct = (rev && ebitda != null && rev > 0)
        ? (ebitda / rev * 100).toFixed(1) + '%' : '—';
      n++;
      return `<tr data-search="${esc(fin.__label__)}">
        <td class="row-num td-center">${n}</td>
        <td class="td-bold" style="color:var(--text3);font-style:italic;">Fund Level</td>
        <td class="td-right">${rev    != null ? fmtCr(rev)    : '—'}</td>
        <td class="td-right">${ebitda != null ? fmtCr(ebitda) : '—'}</td>
        <td class="td-right">${pat    != null ? fmtCr(pat)    : '—'}</td>
        <td class="td-right">${ebitdaPct}</td>
        <td>${esc(fin.__label__)}</td>
      </tr>`;
    }).join('');

    const countEl = $('fin-pl-count');
    if (countEl) countEl.textContent = `(${n} periods · fund level)`;
  } catch(e) {
    tbody.innerHTML = '<tr><td colspan="7" class="table-empty">No data available.</td></tr>';
  }
}

function filterFinPnl() {
  const tbody = $('fin-pl-tbody');
  if (!tbody) return;
  const q = ($('fin-pl-search') || {value:''}).value.toLowerCase();
  let n = 0;
  Array.from(tbody.rows).forEach(tr => {
    const blob = (tr.dataset.search || '').toLowerCase();
    const show = !q || blob.includes(q);
    tr.style.display = show ? '' : 'none';
    if (show) {
      n++;
      const numCell = tr.querySelector('td.row-num');
      if (numCell) numCell.textContent = n;
    }
  });
  const countEl = $('fin-pl-count');
  if (countEl) countEl.textContent = `(${n} companies)`;
}

async function loadBvA() {
  const tbody = $('fin-bva-tbody');
  if (!tbody) return;
  try {
    // Build URL: filter by current fund and request budget-set records first
    let url = '/mis/bva/?has_budget=1';
    if (_ctx.fundId) url += `&fund=${_ctx.fundId}`;

    const data = await Auth.apiGet(url);
    let arr = data.results || data || [];

    // If no budget records exist yet, fall back to all records for this fund
    // so the tab still shows actuals while budget is pending import
    if (!arr.length) {
      let fallbackUrl = '/mis/bva/';
      if (_ctx.fundId) fallbackUrl += `?fund=${_ctx.fundId}`;
      const fb = await Auth.apiGet(fallbackUrl);
      arr = fb.results || fb || [];
    }

    // Second fallback: render ConsolidatedMIS budget/actual when no BvA records at all
    if (!arr.length && _ctx.fundId) {
      await _loadBvAFundLevel(tbody);
      return;
    }

    if (!arr.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="table-empty">No financial data. Import financials first.</td></tr>';
      return;
    }

    // Show all budget records with serial # and search blob
    let n = 0;
    tbody.innerHTML = arr.map(r => {
      const v        = parseFloat(r.variance_inr || 0);
      const hasBudget = r.budget_inr != null;
      const favClass  = hasBudget
        ? (r.is_favorable ? 'v5-text-green' : 'v5-text-red')
        : '';
      const budgetVal  = hasBudget ? fmtCr(parseFloat(r.budget_inr)) : '—';
      const actualVal  = r.actual_inr != null ? fmtCr(parseFloat(r.actual_inr)) : '—';
      const varCell    = hasBudget && r.variance_inr != null
        ? `<span class="${favClass}">${v >= 0 ? '+' : ''}${fmtCr(v)}</span>`
        : '—';
      const favCell    = hasBudget
        ? (r.is_favorable === true
            ? '<span class="v5-status active">Yes</span>'
            : '<span class="v5-status overdue">No</span>')
        : '<span class="v5-status pending">—</span>';
      n++;
      const cname = r.company_name || String(r.portfolio_company || '—');
      const searchBlob = [cname, r.line_item_display || r.line_item].join(' ').toLowerCase();
      return `<tr data-search="${esc(searchBlob)}">
        <td class="row-num td-center">${n}</td>
        <td class="td-bold">${esc(cname)}</td>
        <td>${esc(r.line_item_display || r.line_item || '—')}</td>
        <td class="td-right">${budgetVal}</td>
        <td class="td-right">${actualVal}</td>
        <td class="td-right">${varCell}</td>
        <td class="td-center">${favCell}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="7" class="table-empty">No BvA data.</td></tr>';

    const countEl = $('fin-bva-count');
    if (countEl) countEl.textContent = `(${n} records)`;
  } catch(e) {
    tbody.innerHTML = '<tr><td colspan="7" class="table-empty">Error loading BvA data.</td></tr>';
  }
}

async function _loadBvAFundLevel(tbody) {
  // Show ConsolidatedMIS as BvA when no per-company BvA records exist for this fund.
  try {
    const misData = await Auth.apiGet(`/mis/consolidated/?fund=${_ctx.fundId}`);
    const misArr  = misData.results || misData || [];

    const SKIP_ITEMS = new Set(['net_irr', 'tvpi', 'portfolio_fv']);
    const recs = misArr.filter(r => !SKIP_ITEMS.has(r.line_item));

    if (!recs.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="table-empty">No financial data. Import financials first.</td></tr>';
      return;
    }

    const MONTHS = ['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    let n = 0;
    tbody.innerHTML = recs.map(r => {
      const actual = parseFloat(r.total_actual_inr ?? 0);
      const budget = parseFloat(r.total_budget_inr ?? 0);
      const variance = actual - budget;
      const hasBudget = r.total_budget_inr != null && budget !== 0;
      const periodLabel = r.period_month
        ? `${MONTHS[r.period_month]}-${r.period_year}`
        : String(r.period_year || '—');
      const favClass = hasBudget ? (variance >= 0 ? 'v5-text-green' : 'v5-text-red') : '';
      n++;
      return `<tr data-search="${esc((r.line_item || '') + ' ' + periodLabel)}">
        <td class="row-num td-center">${n}</td>
        <td class="td-bold" style="color:var(--text3);font-style:italic;">${esc(periodLabel)}</td>
        <td>${esc(r.line_item || '—')}</td>
        <td class="td-right">${hasBudget ? fmtCr(budget) : '—'}</td>
        <td class="td-right">${fmtCr(actual)}</td>
        <td class="td-right">${hasBudget ? `<span class="${favClass}">${variance >= 0 ? '+' : ''}${fmtCr(variance)}</span>` : '—'}</td>
        <td class="td-center"><span class="v5-status pending">Fund Level</span></td>
      </tr>`;
    }).join('');

    const countEl = $('fin-bva-count');
    if (countEl) countEl.textContent = `(${n} records · fund level)`;
  } catch(e) {
    tbody.innerHTML = '<tr><td colspan="7" class="table-empty">Error loading data.</td></tr>';
  }
}

function filterFinBvA() {
  const tbody = $('fin-bva-tbody');
  if (!tbody) return;
  const q = ($('fin-bva-search') || {value:''}).value.toLowerCase();
  let n = 0;
  Array.from(tbody.rows).forEach(tr => {
    const blob = (tr.dataset.search || '').toLowerCase();
    const show = !q || blob.includes(q);
    tr.style.display = show ? '' : 'none';
    if (show) {
      n++;
      const numCell = tr.querySelector('td.row-num');
      if (numCell) numCell.textContent = n;
    }
  });
  const countEl = $('fin-bva-count');
  if (countEl) countEl.textContent = `(${n} records)`;
}

async function loadConsolidated() {
  const el = $('fin-consolidated');
  if (!el) return;
  try {
    const fqs  = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const data = await Auth.apiGet('/mis/consolidated/' + fqs);
    const arr  = data.results || data || [];
    if (!arr.length) {
      el.querySelector('.v5-panel-body').innerHTML =
        '<div style="color:var(--text3);font-size:11px">No consolidated data yet. Import financials and run consolidation via MIS Consolidation page.</div>';
      return;
    }
    // Group by period_year + period_month, show summary table
    const periods = {};
    for (const r of arr) {
      const pkey = `${r.period_year}-${String(r.period_month || 0).padStart(2,'0')}`;
      if (!periods[pkey]) periods[pkey] = { year: r.period_year, month: r.period_month, items: {} };
      periods[pkey].items[r.line_item] = parseFloat(r.total_actual_inr || 0);
    }
    const MONTH_NAMES = ['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const rows = Object.entries(periods).sort((a,b) => b[0].localeCompare(a[0])).slice(0, 12);
    const table = `<table class="v5-table"><thead><tr>
      <th>Period</th><th class="td-right">Revenue</th><th class="td-right">EBITDA</th>
      <th class="td-right">PAT</th><th class="td-right">EBITDA%</th></tr></thead><tbody>
      ${rows.map(([pkey, pd]) => {
        const rev    = pd.items.revenue    ?? pd.items.total_revenue ?? 0;
        const ebitda = pd.items.ebitda     ?? 0;
        const pat    = pd.items.pat        ?? 0;
        const ebitdaPct = rev > 0 ? (ebitda / rev * 100).toFixed(1) + '%' : '—';
        const label  = pd.month ? `${MONTH_NAMES[pd.month]} ${pd.year}` : String(pd.year);
        return `<tr>
          <td class="td-bold">${esc(label)}</td>
          <td class="td-right">${fmtCr(rev)}</td>
          <td class="td-right">${fmtCr(ebitda)}</td>
          <td class="td-right">${fmtCr(pat)}</td>
          <td class="td-right">${ebitdaPct}</td>
        </tr>`;
      }).join('')}
    </tbody></table>`;
    el.querySelector('.v5-panel-body').innerHTML = table;
  } catch(e) {}
}

/* ── Valuations ────────────────────────────────────────────── */
async function loadValuations() {
  const tbody = $('val-tbody');
  if (!tbody) return;

  try {
    // Use investments which have latest_valuation field
    const schemeIds = _ctx.schemeIds;
    const invs = await getInvestmentsForContext(schemeIds);

    if (invs.length) {
      // Sort by FV descending
      const sorted = [...invs].sort((a,b) =>
        parseFloat(b.latest_valuation||0) - parseFloat(a.latest_valuation||0)
      );
      tbody.innerHTML = sorted.slice(0,40).map(inv => {
        const cost = parseFloat(inv.total_invested    || 0);
        const fv   = parseFloat(inv.latest_valuation  || 0);
        const moic = cost > 0 ? fmtX(fv / cost) : '—';
        return `<tr>
          <td class="td-bold">${esc(inv.company_name || inv.portfolio_company_name || '—')}</td>
          <td>${esc(inv.sector || '—')}</td>
          <td>${esc(inv.instrument_type_display || inv.instrument_type || '—')}</td>
          <td class="td-right">${cost > 0 ? fmtCr(cost) : '—'}</td>
          <td class="td-right">${fv   > 0 ? fmtCr(fv)   : '—'}</td>
          <td class="td-right v5-text-green">${moic}</td>
          <td>${inv.investment_date || '—'}</td>
        </tr>`;
      }).join('') || '<tr><td colspan="7" class="table-empty">No valuation records.</td></tr>';
      return;
    }
  } catch(e) {}

  tbody.innerHTML = '<tr><td colspan="7" class="table-empty">No valuation records.</td></tr>';
}

function renderValMethod() {
  const el = $('val-method-breakdown');
  if (!el) return;
  const methods = [
    { name:'Revenue Multiple', pct:38, color:'#2563eb' },
    { name:'EBITDA Multiple',  pct:28, color:'#10b981' },
    { name:'DCF',              pct:18, color:'#f59e0b' },
    { name:'P/BV Multiple',    pct:10, color:'#8b5cf6' },
    { name:'Cost Method',      pct:6,  color:'#ef4444' },
  ];
  el.innerHTML = methods.map(m => `
    <div class="v5-sector-bar">
      <div class="v5-sector-name">${m.name}</div>
      <div class="v5-sector-track"><div class="v5-sector-fill" style="width:${m.pct}%;background:${m.color}"></div></div>
      <div class="v5-sector-pct">${m.pct}%</div>
    </div>`).join('');
}

async function renderValBridge() {
  const el = $('val-bridge-body');
  if (!el) return;
  try {
    const schemeIds = _ctx.schemeIds;
    const invs = await getInvestmentsForContext(schemeIds);
    if (!invs || !invs.length) {
      el.innerHTML = '<div style="color:var(--text3);font-size:11px">No valuation data found. Import fund data first.</div>';
      return;
    }
    // Aggregate: totalCost, totalFV per sector
    let totalCost = 0, totalFV = 0;
    const bySector = {};
    for (const inv of invs) {
      const cost = parseFloat(inv.total_invested || 0);
      const fv   = parseFloat(inv.latest_valuation || inv.current_value || 0);
      if (!cost && !fv) continue;
      totalCost += cost;
      totalFV   += fv;
      const sec = inv.sector || 'Other';
      if (!bySector[sec]) bySector[sec] = { cost: 0, fv: 0 };
      bySector[sec].cost += cost;
      bySector[sec].fv   += fv;
    }
    const gain = totalFV - totalCost;
    const gainPct = totalCost > 0 ? (gain / totalCost * 100).toFixed(1) : '—';
    const gainColor = gain >= 0 ? '#34d399' : '#f87171';
    const moicVal = totalCost > 0 ? (totalFV / totalCost).toFixed(2) + 'x' : '—';

    // Build value bridge bars
    const maxVal = Math.max(totalCost, totalFV) || 1;
    const costPct = (totalCost / maxVal * 100).toFixed(1);
    const fvPct   = (totalFV   / maxVal * 100).toFixed(1);
    const gainBarPct = Math.abs(gain / maxVal * 100).toFixed(1);

    let sectorRows = Object.entries(bySector)
      .sort((a, b) => b[1].fv - a[1].fv)
      .map(([sec, d]) => {
        const g = d.fv - d.cost;
        const gColor = g >= 0 ? '#34d399' : '#f87171';
        const m = d.cost > 0 ? (d.fv / d.cost).toFixed(2) + 'x' : '—';
        return `<tr>
          <td>${esc(sec)}</td>
          <td class="td-right">${fmtCr(d.cost)}</td>
          <td class="td-right">${fmtCr(d.fv)}</td>
          <td class="td-right" style="color:${gColor}">${g >= 0 ? '+' : ''}${fmtCr(g)}</td>
          <td class="td-right">${m}</td>
        </tr>`;
      }).join('');

    el.innerHTML = `
      <div style="margin-bottom:16px">
        <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:12px">
          <div class="v5-kpi-card blue" style="flex:1;min-width:120px">
            <div class="v5-kpi-label">Total Cost</div>
            <div class="v5-kpi-value">${fmtCr(totalCost)}</div>
          </div>
          <div class="v5-kpi-card green" style="flex:1;min-width:120px">
            <div class="v5-kpi-label">Fair Value</div>
            <div class="v5-kpi-value">${fmtCr(totalFV)}</div>
          </div>
          <div class="v5-kpi-card" style="flex:1;min-width:120px;background:var(--card-bg)">
            <div class="v5-kpi-label">Unrealized Gain</div>
            <div class="v5-kpi-value" style="color:${gainColor}">${gain >= 0 ? '+' : ''}${fmtCr(gain)}</div>
            <div class="v5-kpi-sub">${gainPct}% · MOIC ${moicVal}</div>
          </div>
        </div>
        <div style="background:var(--card-bg);border-radius:8px;padding:12px;margin-bottom:12px">
          <div style="font-size:11px;color:var(--text3);margin-bottom:6px">Value Bridge (₹ Cr)</div>
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
            <div style="width:70px;font-size:11px;color:var(--text3)">Cost</div>
            <div style="flex:1;background:var(--border);border-radius:4px;height:18px;overflow:hidden">
              <div style="width:${costPct}%;height:100%;background:#2563eb;border-radius:4px"></div>
            </div>
            <div style="width:90px;text-align:right;font-size:12px;font-weight:600">${fmtCr(totalCost)}</div>
          </div>
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
            <div style="width:70px;font-size:11px;color:var(--text3)">Gain/Loss</div>
            <div style="flex:1;background:var(--border);border-radius:4px;height:18px;overflow:hidden">
              <div style="width:${gainBarPct}%;height:100%;background:${gainColor};border-radius:4px"></div>
            </div>
            <div style="width:90px;text-align:right;font-size:12px;font-weight:600;color:${gainColor}">${gain >= 0 ? '+' : ''}${fmtCr(gain)}</div>
          </div>
          <div style="display:flex;align-items:center;gap:8px">
            <div style="width:70px;font-size:11px;color:var(--text3)">Fair Value</div>
            <div style="flex:1;background:var(--border);border-radius:4px;height:18px;overflow:hidden">
              <div style="width:${fvPct}%;height:100%;background:#10b981;border-radius:4px"></div>
            </div>
            <div style="width:90px;text-align:right;font-size:12px;font-weight:600">${fmtCr(totalFV)}</div>
          </div>
        </div>
        <table class="v5-table" style="font-size:12px">
          <thead><tr><th>Sector</th><th class="td-right">Cost (Cr)</th><th class="td-right">FV (Cr)</th><th class="td-right">Gain (Cr)</th><th class="td-right">MOIC</th></tr></thead>
          <tbody>${sectorRows || '<tr><td colspan="5" class="table-empty">No sector data</td></tr>'}</tbody>
        </table>
      </div>`;
  } catch(e) {
    el.innerHTML = '<div style="color:var(--text3);font-size:11px">Value bridge requires imported valuation data.</div>';
  }
}

/* ── Investors ─────────────────────────────────────────────── */
async function loadInvestors() {
  try {
    // When a specific fund is selected, scope all three calls to that fund so
    // investors/commitments/calls from other funds never bleed through.
    const fundQS = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const [investorRes, commitRes, callsRes] = await Promise.allSettled([
      Auth.apiGet('/lp/investors/'    + fundQS),
      Auth.apiGet('/lp/commitments/'  + fundQS),
      Auth.apiGet('/lp/capital-calls/' + fundQS),
    ]);

    const lps    = (investorRes.value?.results || investorRes.value || []);
    const commits = (commitRes.value?.results  || commitRes.value  || []);
    const calls   = (callsRes.value?.results   || callsRes.value   || []);

    const elSub = $('inv-subtitle');
    if (elSub) elSub.textContent = `${lps.length} LPs · ${_ctx.fundName} · Commitments · Capital accounts · KYC/FATCA`;
    const sbLps = $('sb-lps');
    if (sbLps) sbLps.textContent = lps.length;

    // Aggregate commitment amounts per investor name
    const commitByInvestor = {};
    commits.forEach(c => {
      const key = c.investor_name || '—';
      commitByInvestor[key] = (commitByInvestor[key] || 0) + parseFloat(c.commitment_amount || 0);
    });

    // Total called from capital calls (already Cr)
    let totalCalled = 0;
    calls.forEach(c => { totalCalled += parseFloat(c.total_call_amount || 0); });

    const totalCommit = commits.reduce((s, c) => s + parseFloat(c.commitment_amount || 0), 0);

    const kpiEl = $('inv-kpis');
    if (kpiEl) {
      kpiEl.innerHTML = `
        <div class="v5-kpi-card blue"><div class="v5-kpi-label">Total LPs</div><div class="v5-kpi-value">${lps.length}</div><div class="v5-kpi-sub">Active investors</div></div>
        <div class="v5-kpi-card green"><div class="v5-kpi-label">Total Commitment</div><div class="v5-kpi-value">${fmtCr(totalCommit)}</div><div class="v5-kpi-sub">Corpus (Cr)</div></div>
        <div class="v5-kpi-card gold"><div class="v5-kpi-label">Amount Called</div><div class="v5-kpi-value">${fmtCr(totalCalled)}</div><div class="v5-kpi-sub">${totalCommit>0?(totalCalled/totalCommit*100).toFixed(0):0}% drawn</div></div>
        <div class="v5-kpi-card purple"><div class="v5-kpi-label">Distributions</div><div class="v5-kpi-value">—</div><div class="v5-kpi-sub">DPI —</div></div>
        <div class="v5-kpi-card cyan"><div class="v5-kpi-label">Undrawn</div><div class="v5-kpi-value">${fmtCr(Math.max(0,totalCommit-totalCalled))}</div><div class="v5-kpi-sub">Available (Cr)</div></div>
        <div class="v5-kpi-card red"><div class="v5-kpi-label">Overdue Calls</div><div class="v5-kpi-value">0</div><div class="v5-kpi-sub">All funded</div></div>`;
    }

    const tbody = $('lp-tbody');
    if (tbody) {
      tbody.innerHTML = lps.map(lp => {
        const commit = commitByInvestor[lp.investor_name] || 0;
        return `<tr>
          <td class="td-bold">${esc(lp.investor_name || '—')}</td>
          <td>${esc(lp.investor_type || '—')}</td>
          <td>—</td>
          <td class="td-right">${commit > 0 ? fmtCr(commit) : '—'}</td>
          <td class="td-right">—</td>
          <td class="td-right">—</td>
          <td class="td-center"><span class="v5-status ${lp.kyc_status === 'verified' || lp.kyc_status === 'completed' ? 'active' : 'pending'}">${esc(lp.kyc_status || 'pending')}</span></td>
        </tr>`;
      }).join('') || '<tr><td colspan="7" class="table-empty">No LP data.</td></tr>';
    }

    // Investor type distribution
    const typeEl = $('inv-type-bars');
    if (typeEl) {
      const types = {};
      lps.forEach(lp => { const t = lp.investor_type || 'Other'; types[t] = (types[t]||0)+1; });
      renderBarList(typeEl, types, '#2563eb');
    }

    // Store for sub pages
    window._lp_lps    = lps;
    window._lp_commits = commits;
    window._lp_calls   = calls;
  } catch(e) {
    console.error('Investors load error:', e);
  }
}

async function loadLPCapital() {
  try {
    // Build query string: prefer fund-level filter so we get exactly the investors
    // who belong to the selected fund; fall back to scheme list if no fund is selected.
    let url = '/lp/capital-accounts/';
    if (_ctx.fundId) {
      url += `?fund=${_ctx.fundId}`;
    } else if (_ctx.schemeIds && _ctx.schemeIds.length) {
      url += '?' + schemeQS(_ctx.schemeIds);
    }

    const accounts = await Auth.apiGet(url);
    const rows = accounts?.results || accounts || [];

    // Keep only the latest snapshot per commitment (highest as_of_date)
    const latestMap = {};
    rows.forEach(acc => {
      const key = acc.commitment;
      if (!latestMap[key] || acc.as_of_date > latestMap[key].as_of_date) {
        latestMap[key] = acc;
      }
    });
    const latest = Object.values(latestMap);

    const tbody = $('lp-cap-tbody');
    if (!tbody) return;
    tbody.innerHTML = latest.map(acc => {
      const unrealized = parseFloat(acc.unrealized_value || 0);
      const distributed = parseFloat(acc.distributed_capital || 0);
      const netPos = unrealized + distributed;  // total value realised + unrealised
      return `<tr>
        <td class="td-bold">${esc(acc.investor_name || '—')}</td>
        <td class="td-right">${fmtCr(parseFloat(acc.committed_capital || 0))}</td>
        <td class="td-right">${fmtCr(parseFloat(acc.called_capital || 0))}</td>
        <td class="td-right">${fmtCr(distributed)}</td>
        <td class="td-right">${fmtCr(parseFloat(acc.total_value || netPos))}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="5" class="table-empty">No capital account data.</td></tr>';
  } catch(e) {}
}

async function loadLPKYC() {
  try {
    const fqs  = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const data  = await Auth.apiGet('/lp/investors/' + fqs);
    const lps   = data.results || data || [];
    const tbody = $('lp-kyc-tbody');
    if (!tbody) return;

    const kyc = ks => `<span class="v5-status ${ks === 'verified' || ks === 'completed' ? 'active' : 'pending'}">${esc(ks || 'pending')}</span>`;
    tbody.innerHTML = lps.map(lp => `<tr>
      <td class="td-bold">${esc(lp.investor_name || '—')}</td>
      <td class="td-center">${kyc(lp.kyc_status)}</td>
      <td class="td-center">${kyc('verified')}</td>
      <td class="td-center">${kyc('verified')}</td>
      <td>—</td>
    </tr>`).join('') || '<tr><td colspan="5" class="table-empty">No KYC data.</td></tr>';
  } catch(e) {}
}

/* ── Compliance ────────────────────────────────────────────── */
async function loadCompliance() {
  try {
    const kpiEl = $('comp-kpis');
    if (kpiEl) {
      kpiEl.innerHTML = `
        <div class="v5-kpi-card green"><div class="v5-kpi-label">Compliant Filings</div><div class="v5-kpi-value" id="comp-ok">—</div><div class="v5-kpi-sub">Up to date</div></div>
        <div class="v5-kpi-card red"><div class="v5-kpi-label">Overdue</div><div class="v5-kpi-value" id="comp-overdue">—</div><div class="v5-kpi-sub">Action needed</div></div>
        <div class="v5-kpi-card gold"><div class="v5-kpi-label">Due in 30d</div><div class="v5-kpi-value" id="comp-due30">—</div><div class="v5-kpi-sub">Upcoming</div></div>
        <div class="v5-kpi-card blue"><div class="v5-kpi-label">Equity Alerts</div><div class="v5-kpi-value" id="comp-equity">—</div><div class="v5-kpi-sub">Threshold breaches</div></div>
        <div class="v5-kpi-card purple"><div class="v5-kpi-label">SEBI Reports</div><div class="v5-kpi-value" id="comp-sebi-count">—</div><div class="v5-kpi-sub">Filed</div></div>
        <div class="v5-kpi-card cyan"><div class="v5-kpi-label">Portfolio Cos</div><div class="v5-kpi-value" id="comp-cos-count">—</div><div class="v5-kpi-sub">Under monitoring</div></div>`;
    }

    // Endpoints: /compliance/reports/ and /compliance/alerts/
    const [reportsData, alertsData] = await Promise.allSettled([
      Auth.apiGet('/compliance/reports/'),
      Auth.apiGet('/compliance/alerts/'),
    ]);

    const sebi   = (reportsData.value?.results || reportsData.value || []);
    const equity = (alertsData.value?.results  || alertsData.value  || []);

    const compOk = sebi.filter(r => r.status === 'submitted' || r.status === 'filed' || r.status === 'completed').length;
    const compOd = sebi.filter(r => r.status === 'overdue').length;
    const compDue= sebi.filter(r => r.status === 'pending'  || r.status === 'due').length;

    if ($('comp-ok'))         $('comp-ok').textContent         = compOk;
    if ($('comp-overdue'))    $('comp-overdue').textContent    = compOd;
    if ($('comp-due30'))      $('comp-due30').textContent      = compDue;
    if ($('comp-equity'))     $('comp-equity').textContent     = equity.length;
    if ($('comp-sebi-count')) $('comp-sebi-count').textContent = sebi.length;
    if ($('comp-cos-count'))  $('comp-cos-count').textContent  = '—';

    const tbody = $('comp-tbody');
    if (tbody) {
      tbody.innerHTML = sebi.slice(0,20).map(r => `<tr>
        <td class="td-bold">${esc(r.report_type || r.filing_type || '—')}</td>
        <td>SEBI</td>
        <td>${r.due_date || '—'}</td>
        <td class="td-center"><span class="v5-status ${(r.status||'').toLowerCase()}">${esc(r.status || '—')}</span></td>
        <td>${r.filed_date || r.submission_date || '—'}</td>
      </tr>`).join('') || '<tr><td colspan="5" class="table-empty">No compliance records. Import compliance data first.</td></tr>';
    }

    const healthEl = $('comp-health');
    if (healthEl) {
      const total = sebi.length || 1;
      const okPct = Math.round(compOk / total * 100);
      healthEl.innerHTML = `
        <div style="margin-bottom:12px">
          <div style="display:flex;justify-content:space-between;margin-bottom:5px">
            <span style="font-size:11px;color:var(--text2)">Overall Compliance Rate</span>
            <span style="font-size:12px;font-weight:700;color:#34d399">${okPct}%</span>
          </div>
          <div class="v5-progress-bar-wrap"><div class="v5-progress-bar-fill" style="width:${okPct}%"></div></div>
        </div>
        <div class="v5-insight-card gold"><div class="v5-insight-title">&#9888; ${compOd} overdue filings</div>Action required for ${compOd} regulatory submissions.</div>
        <div class="v5-insight-card green"><div class="v5-insight-title">&#10003; ${compOk} filings current</div>SEBI, FEMA, PMLA records up to date.</div>`;
    }
  } catch(e) {
    console.error('Compliance load error:', e);
  }
}

async function loadSEBI() {
  try {
    const data  = await Auth.apiGet('/compliance/reports/');
    const arr   = data.results || data || [];
    const tbody = $('sebi-tbody');
    if (!tbody) return;
    tbody.innerHTML = arr.map(r => `<tr>
      <td class="td-bold">${esc(r.report_type || '—')}</td>
      <td>${r.period || '—'}</td>
      <td>${r.due_date || '—'}</td>
      <td class="td-center"><span class="v5-status ${(r.status||'').toLowerCase()}">${esc(r.status || '—')}</span></td>
      <td class="td-right">—</td>
    </tr>`).join('') || '<tr><td colspan="5" class="table-empty">No SEBI reports.</td></tr>';
  } catch(e) {}
}

async function loadCompAlerts() {
  try {
    const data = await Auth.apiGet('/compliance/alerts/');
    const arr  = data.results || data || [];
    const el   = $('comp-alerts-body');
    if (!el) return;
    if (!arr.length) { el.innerHTML = '<div style="color:var(--text3);font-size:11px">No active compliance alerts.</div>'; return; }
    el.innerHTML = arr.map(a => `
      <div class="v5-insight-card ${a.severity === 'critical' || a.severity === 'high' ? 'red' : 'gold'}">
        <div class="v5-insight-title">${esc(a.alert_type || a.company_name || '—')}</div>
        ${esc(a.description || a.message || '—')}
        <div style="font-size:9px;color:var(--text3);margin-top:4px">${a.created_at || '—'}</div>
      </div>`).join('');
  } catch(e) {
    const el = $('comp-alerts-body');
    if (el) el.innerHTML = '<div style="color:var(--text3);font-size:11px">No alert data.</div>';
  }
}

async function loadCompCalendar() {
  try {
    const data  = await Auth.apiGet('/compliance/calendar/');
    const arr   = data.results || data || [];
    const tbody = $('cal-tbody');
    if (!tbody) return;
    const now = new Date();
    tbody.innerHTML = arr.map(e => {
      const due      = new Date(e.due_date || e.deadline || '');
      const daysLeft = isNaN(due) ? '—' : Math.ceil((due - now) / 86400000);
      const daysStr  = typeof daysLeft === 'number'
        ? (daysLeft < 0 ? `<span style="color:#f87171">${daysLeft}d overdue</span>` : `${daysLeft}d`)
        : '—';
      return `<tr>
        <td class="td-bold">${esc(e.event_type || e.title || '—')}</td>
        <td>${e.due_date || e.deadline || '—'}</td>
        <td class="td-center"><span class="v5-status ${(e.status||'').toLowerCase()}">${esc(e.status||'—')}</span></td>
        <td>${daysStr}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="4" class="table-empty">No calendar events.</td></tr>';
  } catch(e) {}
}

/* ── Benchmarks & Market ───────────────────────────────────── */
function renderBenchmarks() {
  const benchIndia = $('bench-india');
  if (benchIndia) {
    const peers = [
      { name:'Sequoia India VIII', irr:'26.2%', moic:'2.1x', dpi:'0.38x', vintage:'2022' },
      { name:'ChrysCapital IX',    irr:'22.4%', moic:'1.87x', dpi:'0.45x', vintage:'2021' },
      { name:'Kedaara III',        irr:'19.8%', moic:'1.72x', dpi:'0.52x', vintage:'2021' },
      { name:'Your Fund',          irr:'24.3%', moic:'1.95x', dpi:'0.42x', vintage:'2022' },
    ];
    benchIndia.innerHTML = peers.map((p, i) => `
      <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border);${i===3?'background:rgba(37,99,235,0.05);margin:0 -16px;padding:8px 16px;border-radius:6px':''}">
        <div><div style="font-size:12px;font-weight:${i===3?700:500};color:${i===3?'var(--accent3)':'var(--text)'}">${p.name}</div><div style="font-size:10px;color:var(--text3)">Vintage ${p.vintage}</div></div>
        <div style="display:flex;gap:18px;text-align:right">
          <div><div style="font-size:12px;font-weight:600;color:${i===3?'#34d399':'var(--text)'}">${p.irr}</div><div style="font-size:9px;color:var(--text3)">Net IRR</div></div>
          <div><div style="font-size:12px;font-weight:600">${p.moic}</div><div style="font-size:9px;color:var(--text3)">MOIC</div></div>
          <div><div style="font-size:12px;font-weight:600">${p.dpi}</div><div style="font-size:9px;color:var(--text3)">DPI</div></div>
        </div>
      </div>`).join('');
  }

  const benchGlobal = $('bench-global');
  if (benchGlobal) {
    benchGlobal.innerHTML = `
      <div class="v5-insight-card"><div class="v5-insight-title">Global PE Median (2022 Vintage)</div>Net IRR: 18.4% · MOIC: 1.62x · DPI: 0.31x</div>
      <div class="v5-insight-card green"><div class="v5-insight-title">Your Fund vs Global Median</div>IRR +5.9pts · MOIC +0.33x — Top Quartile Performance</div>`;
  }

  const benchSaaS = $('bench-saas');
  if (benchSaaS) {
    benchSaaS.innerHTML = `
      <div class="v5-insight-card"><div class="v5-insight-title">SaaS Benchmarks — India Portfolio</div>NRR: 118% · Churn: 2.4% · LTV/CAC: 4.2x</div>
      <div class="v5-insight-card green"><div class="v5-insight-title">Unit Economics: Healthy</div>All SaaS metrics above benchmarks for Indian B2B SaaS cohort.</div>`;
  }
}

function renderMarket() {
  const mktSectors = $('mkt-sectors');
  if (mktSectors) {
    const sectors = [
      { name:'Fintech',      score:92, trend:'up',   note:'Regulatory tailwinds, UPI 3.0' },
      { name:'HealthTech',   score:88, trend:'up',   note:'Post-COVID digitisation wave' },
      { name:'D2C Brands',   score:76, trend:'down', note:'Margin pressure from competition' },
      { name:'CleanTech',    score:84, trend:'up',   note:'PLI schemes accelerating adoption' },
      { name:'EdTech',       score:65, trend:'down', note:'Post-pandemic normalisation' },
      { name:'AgriTech',     score:78, trend:'up',   note:'Rural digital penetration rising' },
    ];
    mktSectors.innerHTML = sectors.map(s => `
      <div style="display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid var(--border)">
        <div style="flex:1"><div style="font-size:12px;font-weight:600;color:var(--text)">${s.name}</div><div style="font-size:10px;color:var(--text3)">${s.note}</div></div>
        <div style="text-align:right"><div style="font-size:14px;font-weight:800;color:${s.trend==='up'?'#34d399':'#f87171'}">${s.score}</div><div style="font-size:9px;color:var(--text3)">Opp. Score</div></div>
      </div>`).join('');
  }

  const mktDeals = $('mkt-deals');
  if (mktDeals) {
    mktDeals.innerHTML = `
      <div class="v5-insight-card"><div class="v5-insight-title">Deal: Rapid Commerce — Series C</div>₹180 Cr · Lead: ChrysCapital · Q-Commerce, Tier 2 expansion</div>
      <div class="v5-insight-card gold"><div class="v5-insight-title">Deal: NeuralMed — Series B</div>₹95 Cr · Co-invest opportunity · AI diagnostic imaging</div>
      <div class="v5-insight-card green"><div class="v5-insight-title">Exit Watch: BrightEdu</div>IPO filing expected Q3 FY26 · Projected 3.8-4.2x MOIC</div>`;
  }

  const mktMacro = $('mkt-macro');
  if (mktMacro) {
    const macros = [
      { label:'India GDP Growth',   value:'6.8%',   trend:'up' },
      { label:'RBI Repo Rate',      value:'6.50%',  trend:'' },
      { label:'INR/USD',            value:'83.4',   trend:'down' },
      { label:'Nifty 50 YTD',       value:'+14.2%', trend:'up' },
      { label:'Startup Funding YTD',value:'$4.2B',  trend:'down' },
      { label:'PE Deployment YTD',  value:'$8.1B',  trend:'up' },
    ];
    mktMacro.innerHTML = `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px">${
      macros.map(m => `<div class="v5-metric-item"><div class="v5-metric-label">${m.label}</div><div class="v5-metric-value" style="color:${m.trend==='up'?'#34d399':m.trend==='down'?'#f87171':'var(--white)'}">${m.value}</div></div>`).join('')
    }</div>`;
  }
}

/* ── Analytics ─────────────────────────────────────────────── */
function renderAnalytics() {
  const ins = $('ai-insights-mini');
  if (ins) {
    ins.innerHTML = `
      <div class="v5-insight-card gold"><div class="v5-insight-title">&#9888; Portfolio risk needs attention</div>Run Analysis to get Gemini AI-powered insights for this fund.</div>
      <div class="v5-insight-card green"><div class="v5-insight-title">&#8593; Click Run Analysis above</div>Get exit predictions, revenue forecasts and peer benchmarks instantly.</div>
      <div class="v5-insight-card blue"><div class="v5-insight-title">&#128302; AI-powered MIS Reports</div>Navigate to MIS Reports tab to generate Fund-Level and Company-Level reports.</div>`;
  }
  const topPerf = $('top-performers');
  if (topPerf) topPerf.innerHTML = '<div style="color:var(--text3);font-size:11px;padding:8px">Run Analysis to populate top performers.</div>';
}

async function runAIAnalysis() {
  const btn = $('ai-run-analysis-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Analysing…'; }
  try {
    const data = await Auth.apiGet('/ai-predictions/');
    _cachedPredictions = data;
    _renderPredictionKPIs(data.portfolio_insights || {});
    if (btn) { btn.disabled = false; btn.textContent = '🔮 Run Analysis'; }
    // Update chatbot KPIs
    const ins = data.portfolio_insights || {};
    if ($('ai-risk-score'))  $('ai-risk-score').textContent  = ins.avg_risk_score ? `${ins.avg_risk_score}/100` : '—';
    if ($('ai-outperform'))  $('ai-outperform').textContent  = ins.outperformers_count ?? '—';
    if ($('ai-watchlist'))   $('ai-watchlist').textContent   = ins.underperformers_count ?? '—';
    // Refresh insights
    const insEl = $('ai-insights-mini');
    if (insEl && ins.portfolio_momentum) {
      insEl.innerHTML = `
        <div class="v5-insight-card green"><div class="v5-insight-title">&#8593; Portfolio Momentum: ${esc(ins.portfolio_momentum)}</div>Gemini analysis complete — check Predictions tab for full breakdown.</div>
        <div class="v5-insight-card blue"><div class="v5-insight-title">&#128201; Risk Score: ${ins.avg_risk_score || '—'}/100</div>${ins.outperformers_count || 0} outperformers · ${ins.underperformers_count || 0} underperformers detected.</div>
        <div class="v5-insight-card gold"><div class="v5-insight-title">&#128200; Rev Growth CAGR: ${ins.rev_growth_cagr ? ins.rev_growth_cagr.toFixed(1) + '%' : '—'}</div>Sector alpha (Tech): ${ins.sector_alpha_tech_pct ? '+' + ins.sector_alpha_tech_pct.toFixed(1) + '%' : '—'} vs benchmark.</div>`;
    }
    showToast('AI Analysis complete', 'success');
    // If predictions tab already open, re-render it
    if ($('ai-predict') && $('ai-predict').classList.contains('active')) {
      _renderPredictionsContent(data);
    }
    // If AI Insights tab open, refresh it
    if ($('ai-insights') && $('ai-insights').classList.contains('active')) {
      delete _subRendered['analytics-insights'];
      loadAIInsights();
    }
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = '🔮 Run Analysis'; }
    showToast('AI Analysis failed: ' + (e.message || e), 'error');
  }
}

let _cachedPredictions = null;

function _renderPredictionKPIs(ins) {
  // Update both Risk Monitor KPIs and Predictions KPIs
  const set = (id, val) => { const el = $(id); if (el) el.textContent = val; };
  const score = ins.avg_risk_score ? `${ins.avg_risk_score}/100` : '—';
  const cagr  = ins.rev_growth_cagr ? `${Number(ins.rev_growth_cagr).toFixed(1)}%` : '—';
  const alpha = ins.sector_alpha_tech_pct != null ? `+${Number(ins.sector_alpha_tech_pct).toFixed(1)}%` : '—';
  const mom   = ins.portfolio_momentum || '—';

  ['rm-', 'pred-'].forEach(pfx => {
    set(pfx + 'risk-score',      score);
    set(pfx + 'outperformers',   ins.outperformers_count  ?? '—');
    set(pfx + 'underperformers', ins.underperformers_count ?? '—');
    set(pfx + 'rev-cagr',        cagr);
    set(pfx + 'sector-alpha',    alpha);
    set(pfx + 'momentum',        mom);
  });
}

async function loadRiskMonitor() {
  const tbody  = $('risk-tbody');
  const watchEl = $('watch-list');

  // Show computing state
  if (tbody) tbody.innerHTML = '<tr><td colspan="5" class="table-empty" id="risk-computing-msg">Loading risk scores…</td></tr>';

  try {
    let data = await Auth.apiGet('/risk-scores/');
    let arr  = Array.isArray(data) ? data : (data.results || []);

    // If no scores exist, auto-compute for all companies then reload
    if (!arr.length) {
      if (tbody) {
        const msgEl = $('risk-computing-msg');
        if (msgEl) msgEl.textContent = 'Computing risk scores with AI… this takes ~30 seconds first time.';
      }
      try {
        await Auth.apiPost('/risk-scores/compute-all/', {});
      } catch(ce) { /* ignore errors — retry the list */ }
      // Reload list after compute
      data = await Auth.apiGet('/risk-scores/');
      arr  = Array.isArray(data) ? data : (data.results || []);
    }

    if (!tbody) return;

    // Populate Risk Monitor KPIs from actual data
    const scores   = arr.map(r => parseFloat(r.risk_score || 0));
    const avgScore = scores.length ? Math.round(scores.reduce((a,b)=>a+b,0)/scores.length) : 0;
    const outperf  = arr.filter(r => r.irr_pct != null && r.irr_pct > 25).length;
    const underperf= arr.filter(r => r.risk_tier === 'high' || (r.irr_pct != null && r.irr_pct < 5)).length;

    const set = (id, val) => { const el = $(id); if (el) el.textContent = val; };
    set('rm-risk-score',      avgScore ? `${avgScore}/100` : '—');
    set('rm-outperformers',   outperf);
    set('rm-underperformers', underperf);
    // Also sync predictions KPIs if not already populated
    if (!_cachedPredictions) {
      set('pred-risk-score',      avgScore ? `${avgScore}/100` : '—');
      set('pred-outperformers',   outperf);
      set('pred-underperformers', underperf);
    }

    if (!arr.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="table-empty">No portfolio companies found. Import fund data first.</td></tr>';
      return;
    }

    const tierBg = t => ({ high: '#f87171', medium: '#fb923c', low: '#34d399' }[t] || '#94a3b8');
    tbody.innerHTML = arr.slice(0, 40).map(r => {
      const score = r.risk_score != null ? Number(r.risk_score).toFixed(0) : '—';
      const irr   = r.irr_pct   != null ? Number(r.irr_pct).toFixed(1) + '%' : '—';
      const stage = r.stage || '—';
      const barW  = r.risk_score ? Math.min(Math.round(r.risk_score), 100) : 0;
      const bar   = barW ? `<div style="display:inline-block;width:${barW}px;height:4px;background:${tierBg(r.risk_tier)};border-radius:2px;margin-right:6px;vertical-align:middle"></div>` : '';
      return `<tr>
        <td class="td-bold">${esc(r.company_name || '—')}</td>
        <td>${esc(r.sector || '—')}</td>
        <td><span style="font-size:10px;padding:2px 6px;background:var(--card2);border-radius:4px">${esc(stage)}</span></td>
        <td class="td-right">${bar}<strong>${score}</strong></td>
        <td class="td-right" style="color:${(r.irr_pct || 0) >= 0 ? '#34d399' : '#f87171'}">${irr}</td>
      </tr>`;
    }).join('');

    if (watchEl) {
      const highRisk = arr.filter(r => r.risk_tier === 'high' || parseFloat(r.risk_score || 0) > 70).slice(0, 8);
      if (!highRisk.length) {
        watchEl.innerHTML = '<div style="color:var(--text3);font-size:11px;padding:12px">No high-risk companies — portfolio is healthy.</div>';
      } else {
        watchEl.innerHTML = highRisk.map(r => `
          <div class="v5-insight-card red" style="margin-bottom:8px">
            <div class="v5-insight-title">${esc(r.company_name || '—')} &nbsp;<span style="font-size:10px;opacity:0.7">${esc(r.sector || '')}</span></div>
            <div style="display:flex;gap:12px;margin:4px 0;font-size:11px">
              <span>Risk: <strong>${Number(r.risk_score || 0).toFixed(0)}</strong></span>
              ${r.irr_pct != null ? `<span>IRR: <strong>${Number(r.irr_pct).toFixed(1)}%</strong></span>` : ''}
              ${r.stage ? `<span>Stage: <strong>${esc(r.stage)}</strong></span>` : ''}
            </div>
            <div style="font-size:10px;color:var(--text3)">${esc(r.ai_commentary || 'Elevated risk — review recommended')}</div>
          </div>`).join('');
      }
    }
  } catch(e) {
    if (tbody) tbody.innerHTML = `<tr><td colspan="5" class="table-empty">Error loading risk scores: ${esc(e.message || '')}</td></tr>`;
    console.error('Risk Monitor error:', e);
  }
}

/* ── AI Insights ─────────────────────────────────────────────── */
async function loadAIInsights() {
  const heatmapEl  = $('risk-heatmap');
  const analysisEl = $('ai-full-analysis');

  if (heatmapEl)  heatmapEl.innerHTML  = '<div style="color:var(--text3);font-size:11px;padding:12px">Loading portfolio heatmap…</div>';
  if (analysisEl) analysisEl.innerHTML = '<div style="color:var(--text3);font-size:11px;padding:12px">Loading Gemini AI analysis…</div>';

  try {
    const data = await Auth.apiGet('/ai-insights/');
    const heatmap       = data.heatmap       || [];
    const sectorSummary = data.sector_summary || [];
    const fullAnalysis  = data.full_analysis  || '';

    // Render Portfolio Risk Heatmap
    if (heatmapEl) {
      if (!heatmap.length) {
        heatmapEl.innerHTML = '<div style="color:var(--text3);font-size:11px;padding:12px">No portfolio data. Import fund data first.</div>';
      } else {
        const tierColor = t => ({ high: '#f87171', medium: '#fb923c', low: '#34d399' }[t] || '#94a3b8');
        const tierBg    = t => ({ high: 'rgba(248,113,113,0.12)', medium: 'rgba(251,146,60,0.12)', low: 'rgba(52,211,153,0.12)' }[t] || 'var(--card2)');

        // Sector summary row
        const sectorHtml = sectorSummary.slice(0, 6).map(s => `
          <div style="padding:8px;background:var(--card2);border-radius:8px;border:1px solid var(--border);text-align:center">
            <div style="font-size:10px;font-weight:700;color:var(--text)">${esc(s.sector)}</div>
            <div style="font-size:11px;color:#34d399;margin:2px 0">${s.avg_moic != null ? s.avg_moic.toFixed(2) + 'x' : '—'}</div>
            <div style="font-size:9px;color:var(--text3)">${s.company_count} cos · IRR ${s.avg_irr != null ? s.avg_irr.toFixed(1) + '%' : '—'}</div>
          </div>`).join('');

        // Company heatmap grid
        const compHtml = heatmap.slice(0, 24).map(h => `
          <div title="${esc(h.company_name)}" style="padding:6px 8px;border-radius:6px;background:${tierBg(h.risk_tier)};border:1px solid ${tierColor(h.risk_tier)}33;cursor:default">
            <div style="font-size:10px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(h.company_name)}</div>
            <div style="font-size:9px;color:var(--text3)">${esc(h.sector || '')}</div>
            <div style="display:flex;justify-content:space-between;margin-top:3px">
              <span style="font-size:10px;font-weight:700;color:${tierColor(h.risk_tier)}">${Math.round(h.risk_score || 0)}</span>
              <span style="font-size:9px;color:var(--text3)">${h.moic != null ? h.moic.toFixed(1) + 'x' : '—'}</span>
            </div>
          </div>`).join('');

        heatmapEl.innerHTML = `
          <div style="margin-bottom:10px">
            <div style="font-size:10px;font-weight:600;color:var(--text3);margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px">Sector Overview</div>
            <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px">${sectorHtml}</div>
          </div>
          <div>
            <div style="font-size:10px;font-weight:600;color:var(--text3);margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px">Company Risk Map
              <span style="margin-left:8px;font-weight:400">
                <span style="color:#34d399">■</span> Low &nbsp;
                <span style="color:#fb923c">■</span> Medium &nbsp;
                <span style="color:#f87171">■</span> High
              </span>
            </div>
            <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:5px">${compHtml}</div>
          </div>`;
      }
    }

    // Render AI Full Analysis (Gemini markdown)
    if (analysisEl) {
      if (!fullAnalysis) {
        analysisEl.innerHTML = '<div style="color:var(--text3);font-size:11px;padding:12px">No analysis available. Run Analysis or configure Gemini API key.</div>';
      } else {
        // Render markdown using marked.js if available, otherwise pre-format
        let html = fullAnalysis;
        if (typeof marked !== 'undefined') {
          html = marked.parse(fullAnalysis);
        } else {
          html = fullAnalysis
            .replace(/## (.*)/g, '<h3 style="font-size:13px;font-weight:700;margin:12px 0 6px;color:var(--text)">$1</h3>')
            .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
            .replace(/\n/g, '<br>');
        }
        analysisEl.innerHTML = `<div style="font-size:12px;line-height:1.7;color:var(--text2)">${html}</div>`;
      }
    }

  } catch(e) {
    if (heatmapEl)  heatmapEl.innerHTML  = `<div style="color:var(--text3);font-size:11px;padding:12px">Error loading heatmap: ${esc(e.message || 'Check console')}</div>`;
    if (analysisEl) analysisEl.innerHTML = `<div style="color:var(--text3);font-size:11px;padding:12px">Error loading analysis: ${esc(e.message || 'Check console')}</div>`;
    console.error('AI Insights error:', e);
  }
}

async function loadMISReports() {
  try {
    const fundParam = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const data = await Auth.apiGet(`/mis/submission-status/${fundParam}`);
    const arr  = Array.isArray(data) ? data : (data.results || []);
    const tbody = $('mis-status-tbody');
    if (!tbody) return;

    const tick = v => v
      ? '<span style="color:#34d399;font-size:15px">&#10003;</span>'
      : '<span style="color:#f87171;font-size:13px">&#10007;</span>';

    tbody.innerHTML = arr.map((co, idx) => `<tr>
      <td class="td-bold">${esc(co.company_name || '—')}</td>
      <td class="td-center">${tick(co.has_pl)}</td>
      <td class="td-center">${tick(co.has_bs)}</td>
      <td class="td-center">${tick(co.has_cf)}</td>
      <td class="td-center">${tick(co.has_bva)}</td>
      <td style="font-size:10px;color:var(--text3)">${co.last_updated || '—'}</td>
      <td class="td-center"><span class="v5-status ${co.status === 'active' ? 'active' : 'pending'}">${esc(co.status || 'pending')}</span></td>
    </tr>`).join('') || '<tr><td colspan="7" class="table-empty">No companies found for this fund. Import a fund Excel file with P&L / Balance Sheet / Cash Flow / Budget vs Actual sheets.</td></tr>';
  } catch(e) {
    console.error('MIS Submission Status error:', e);
    const tb = $('mis-status-tbody');
    if (tb) tb.innerHTML = '<tr><td colspan="7" class="table-empty">Failed to load MIS data. Check server connection.</td></tr>';
  }
}

async function generateReport(reportType) {
  // Find and disable the button
  const btn = document.querySelector(`.mis-gen-btn[onclick="generateReport('${reportType}')"]`);
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Generating…'; }

  try {
    const payload = { report_type: reportType };
    if (_ctx.fundId) payload.fund_id = _ctx.fundId;
    const report = await Auth.apiPost('/generate-report/', payload);

    // Show report in a modal-style overlay
    _showReportModal(report);
  } catch(e) {
    showToast('Report generation failed: ' + (e.message || e), 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Generate'; }
  }
}

function _showReportModal(report) {
  // Remove existing modal if any
  const existing = document.getElementById('report-modal-overlay');
  if (existing) existing.remove();

  const sectionsHtml = (report.sections || []).map(sec => `
    <div style="margin-bottom:20px">
      <div style="font-size:13px;font-weight:700;color:var(--text);border-bottom:1px solid var(--border);padding-bottom:6px;margin-bottom:10px">${esc(sec.heading)}</div>
      ${(sec.rows || []).map(row => `
        <div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border-soft,rgba(255,255,255,0.05))">
          <span style="font-size:11px;color:var(--text3)">${esc(row.label)}</span>
          <span style="font-size:12px;font-weight:600;color:var(--text)">${esc(row.value)}${row.note ? ` <span style="font-size:10px;color:var(--text3)">(${esc(row.note)})</span>` : ''}</span>
        </div>`).join('')}
    </div>`).join('');

  const highlightsHtml = (report.highlights || []).map(h => `<li style="margin-bottom:4px;font-size:11px">${esc(h)}</li>`).join('');
  const flagsHtml = (report.risk_flags || []).map(f => `<div class="v5-insight-card red" style="margin-bottom:6px;font-size:11px">${esc(f)}</div>`).join('');

  // PDF download button
  const pdfBtnHtml = report.report_id
    ? `<button class="v5-btn v5-btn-ghost" style="font-size:11px;padding:6px 14px;margin-right:8px;background:rgba(5,150,105,0.1);color:#059669;border-color:#059669" onclick="window.open('/api/download-report/${esc(report.report_id)}/','_blank')">&#128229; Download PDF</button>`
    : '';

  const overlay = document.createElement('div');
  overlay.id = 'report-modal-overlay';
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.75);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px';
  overlay.innerHTML = `
    <div style="background:var(--card);border:1px solid var(--border);border-radius:12px;max-width:760px;width:100%;max-height:85vh;overflow-y:auto;padding:28px;position:relative">
      <button onclick="document.getElementById('report-modal-overlay').remove()" style="position:absolute;top:16px;right:16px;background:none;border:none;color:var(--text3);font-size:18px;cursor:pointer">&#10005;</button>
      <div style="font-size:16px;font-weight:700;color:var(--text);margin-bottom:4px">${esc(report.title || 'Report')}</div>
      <div style="font-size:10px;color:var(--text3);margin-bottom:12px">${esc(report.period || '')} &nbsp;·&nbsp; Generated ${new Date(report.generated_at || Date.now()).toLocaleString('en-IN')} &nbsp;·&nbsp; CONFIDENTIAL</div>
      <div style="margin-bottom:16px">${pdfBtnHtml}</div>
      ${report.summary ? `<div class="v5-insight-card blue" style="margin-bottom:16px;font-size:12px">${esc(report.summary)}</div>` : ''}
      ${flagsHtml ? `<div style="margin-bottom:16px">${flagsHtml}</div>` : ''}
      ${sectionsHtml}
      ${highlightsHtml ? `<div style="margin-top:16px"><div style="font-size:12px;font-weight:600;margin-bottom:8px">Key Highlights</div><ul style="padding-left:16px">${highlightsHtml}</ul></div>` : ''}
    </div>`;
  document.body.appendChild(overlay);
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
}

// ── Comprehensive MIS Report ───────────────────────────────────────────
async function generateComprehensiveMIS() {
  const btn = document.getElementById('gen-mis-report-btn');
  if (btn) { btn.disabled = true; btn.innerHTML = '&#9203; Generating Report…'; }

  try {
    if (!_ctx.fundId) {
      showToast('Please select a fund first.', 'error');
      return;
    }

    const result = await Auth.apiPost('/generate-comprehensive-mis/', {
      fund_id: _ctx.fundId
    });

    _showComprehensiveMISModal(result);
  } catch(e) {
    showToast('MIS Report generation failed: ' + (e.message || e), 'error');
    console.error('Comprehensive MIS error:', e);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '&#128196; Generate MIS Report'; }
  }
}

async function _downloadMISPdf(reportId, fileName) {
  try {
    const blob = await Auth.apiGetBlob(`/download-report/${reportId}/`);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = fileName || 'Comprehensive_MIS_Report.pdf';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    showToast('PDF downloaded successfully!', 'success');
  } catch(e) {
    showToast('PDF download failed: ' + (e.message || e), 'error');
  }
}

function _renderMISTable(headers, rows) {
  if (!rows || !rows.length) return '<div style="padding:12px;font-size:11px;color:var(--text3)">No data available.</div>';
  const thHtml = headers.map(h => `<th style="padding:8px 10px;font-size:10px;font-weight:700;color:#fff;background:#003366;text-align:left;border:1px solid #1a4d80">${esc(h)}</th>`).join('');
  const trHtml = rows.map((row, idx) => {
    const bg = idx % 2 === 0 ? 'rgba(255,255,255,0.02)' : 'rgba(99,102,241,0.03)';
    const tds = row.map(cell => `<td style="padding:6px 10px;font-size:10px;color:var(--text);border:1px solid var(--border-soft,rgba(255,255,255,0.06))">${esc(cell)}</td>`).join('');
    return `<tr style="background:${bg}">${tds}</tr>`;
  }).join('');
  return `<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;margin:6px 0"><thead><tr>${thHtml}</tr></thead><tbody>${trHtml}</tbody></table></div>`;
}

function _renderSection(sec) {
  const type = sec.type || 'kv';
  let bodyHtml = '';

  if (type === 'kv_and_text') {
    // Executive summary text + KV rows
    if (sec.text) bodyHtml += `<div style="padding:10px 14px;font-size:11px;color:var(--text);line-height:1.7;border-bottom:1px solid var(--border-soft,rgba(255,255,255,0.06))">${esc(sec.text)}</div>`;
    bodyHtml += (sec.rows || []).map(row => `
      <div style="display:flex;justify-content:space-between;align-items:center;padding:7px 14px;border-bottom:1px solid var(--border-soft,rgba(255,255,255,0.06))">
        <span style="font-size:10px;color:var(--text3)">${esc(row.label)}</span>
        <span style="font-size:11px;font-weight:600;color:var(--text)">${esc(row.value)}</span>
      </div>`).join('');
  } else if (type === 'table') {
    bodyHtml += _renderMISTable(sec.headers || [], sec.table_rows || []);
  } else if (type === 'multi_table') {
    const subs = sec.sub_sections || [];
    if (!subs.length) {
      bodyHtml += '<div style="padding:12px;font-size:11px;color:var(--text3)">No data available.</div>';
    }
    for (const sub of subs) {
      bodyHtml += `<div style="padding:8px 14px 2px;font-size:11px;font-weight:700;color:var(--accent,#6366f1)">${esc(sub.sub_heading)}</div>`;
      bodyHtml += _renderMISTable(sub.headers || [], sub.table_rows || []);
    }
  }

  // Summary line
  if (sec.summary) {
    bodyHtml += `<div style="padding:8px 14px;font-size:10px;font-weight:600;color:var(--accent,#6366f1);border-top:1px solid var(--border-soft,rgba(255,255,255,0.06));background:rgba(99,102,241,0.04)">${esc(sec.summary)}</div>`;
  }

  return `
    <div style="margin-bottom:24px;background:var(--card-inner,rgba(255,255,255,0.03));border:1px solid var(--border);border-radius:10px;overflow:hidden">
      <div style="font-size:13px;font-weight:700;color:var(--text);padding:12px 14px;border-bottom:1px solid var(--border);background:rgba(99,102,241,0.06)">${esc(sec.heading)}</div>
      ${bodyHtml}
    </div>`;
}

function _showComprehensiveMISModal(report) {
  const existing = document.getElementById('comprehensive-mis-overlay');
  if (existing) existing.remove();

  const sections = report.sections || [];
  const sectionsHtml = sections.map(sec => _renderSection(sec)).join('');

  const reportId = report.report_id || '';
  const fileName = `Comprehensive_MIS_Report_${_ctx.fundName || 'Fund'}.pdf`;
  const pdfBtnHtml = reportId
    ? `<button class="v5-btn v5-btn-primary" style="font-size:12px;padding:8px 18px;background:linear-gradient(135deg,#059669,#10b981)" onclick="_downloadMISPdf('${esc(reportId)}','${esc(fileName)}')">&#128229; Download PDF (${report.total_pages || '25+'} pages)</button>`
    : '';

  const overlay = document.createElement('div');
  overlay.id = 'comprehensive-mis-overlay';
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.8);z-index:9999;display:flex;align-items:center;justify-content:center;padding:16px';
  overlay.innerHTML = `
    <div style="background:var(--card);border:1px solid var(--border);border-radius:14px;max-width:1100px;width:100%;max-height:92vh;overflow-y:auto;padding:32px;position:relative">
      <button onclick="document.getElementById('comprehensive-mis-overlay').remove()" style="position:absolute;top:16px;right:16px;background:none;border:none;color:var(--text3);font-size:20px;cursor:pointer;z-index:1">&#10005;</button>

      <div style="display:flex;align-items:center;gap:12px;margin-bottom:6px">
        <div style="font-size:28px">&#128196;</div>
        <div>
          <div style="font-size:18px;font-weight:700;color:var(--text)">${esc(report.title || 'Comprehensive MIS Report')}</div>
          <div style="font-size:10px;color:var(--text3)">Generated ${new Date(report.generated_at || Date.now()).toLocaleString('en-IN')} &nbsp;·&nbsp; ${report.total_pages || '25+'} pages &nbsp;·&nbsp; CONFIDENTIAL</div>
        </div>
      </div>

      <div style="display:flex;gap:10px;margin:16px 0 20px;position:sticky;top:0;z-index:2;background:var(--card);padding:8px 0">
        ${pdfBtnHtml}
        <button class="v5-btn v5-btn-ghost" style="font-size:12px;padding:8px 18px" onclick="document.getElementById('comprehensive-mis-overlay').remove()">Close</button>
      </div>

      <div style="border-top:1px solid var(--border);padding-top:16px">
        ${sectionsHtml}
      </div>

      <div style="margin-top:24px;padding:16px;border-top:1px solid var(--border);border-radius:8px;background:rgba(0,51,102,0.06)">
        <div style="font-size:10px;color:var(--text3);line-height:1.7">
          <strong>DISCLAIMER:</strong> This report has been generated automatically by TrackFundAI based on
          fund data uploaded by the fund manager. All financial figures are as reported in the uploaded MIS files
          and have not been independently audited. Valuations are based on IPEV guidelines. Past performance is
          not indicative of future results. This report is classified as <strong>CONFIDENTIAL</strong> and is
          intended solely for authorised fund managers, Limited Partners (LPs), and their designated advisors.
          All amounts in Indian Rupees (INR), expressed in Crores (Cr) unless otherwise stated.
        </div>
      </div>
    </div>`;

  document.body.appendChild(overlay);
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
}

async function loadAuditLog() {
  try {
    const data  = await Auth.apiGet('/auth/audit-log/');
    const arr   = data.results || data || [];
    const tbody = $('audit-tbody');
    if (!tbody) return;

    const actionColor = a => ({
      create: '#34d399', update: '#60a5fa', delete: '#f87171',
      login: '#a78bfa', logout: '#94a3b8', export: '#fb923c', read: '#e2e8f0',
    }[a] || '#94a3b8');

    const moduleLabel = m => ({
      fund: 'Portfolio', scheme: 'Portfolio', investment: 'Portfolio',
      user: 'Investors', nav: 'Accounting', capital_call: 'Accounting',
      distribution: 'Accounting', compliance: 'Compliance',
      session: 'Auth', portfolio_company: 'Portfolio',
    }[m] || m.charAt(0).toUpperCase() + m.slice(1).replace(/_/g,' '));

    tbody.innerHTML = arr.slice(0, 60).map((a, idx) => {
      const ts = a.timestamp ? new Date(a.timestamp) : null;
      const tsStr = ts ? ts.toLocaleDateString('en-IN') + ' ' + ts.toLocaleTimeString('en-IN', {hour:'2-digit',minute:'2-digit'}) : '—';
      const hash = a.record_hash || '';
      const hashShort = hash ? hash.substring(0, 12) + '…' : '—';
      const mod = moduleLabel(a.resource_type || a.module || 'system');
      return `<tr>
        <td style="font-size:10px;color:var(--text3);text-align:center">${idx + 1}</td>
        <td style="font-size:10px;white-space:nowrap">${tsStr}</td>
        <td><span style="font-size:10px;padding:2px 7px;border-radius:4px;background:${actionColor(a.action)}22;color:${actionColor(a.action)};font-weight:600">${esc((a.action_display || a.action || '—').toUpperCase())}</span></td>
        <td style="font-size:11px">${esc(mod)}</td>
        <td style="font-size:11px">${esc(a.user_name || a.user || '—')}</td>
        <td style="font-size:9px;font-family:monospace;color:var(--text3)" title="${esc(hash)}">${hashShort}</td>
        <td class="td-center"><span class="v5-status active">Active</span></td>
      </tr>`;
    }).join('') || '<tr><td colspan="7" class="table-empty">No audit log entries.</td></tr>';
  } catch(e) {
    const tb = $('audit-tbody');
    if (tb) tb.innerHTML = '<tr><td colspan="7" class="table-empty">No audit logs.</td></tr>';
  }
}

let _revForecastChart = null;

async function loadPredictions() {
  const loadingEl = $('predictions-loading');
  const contentEl = $('predictions-content');

  if (_cachedPredictions) {
    _renderPredictionsContent(_cachedPredictions);
    return;
  }

  // Show loading state
  if (loadingEl) loadingEl.style.display = 'block';
  if (contentEl) contentEl.style.display = 'none';

  // Animate loading bar
  let pct = 0;
  const barEl = $('pred-load-bar');
  const loadInterval = setInterval(() => {
    pct = Math.min(pct + 3, 90);
    if (barEl) barEl.style.width = pct + '%';
  }, 200);

  try {
    const data = await Auth.apiGet('/ai-predictions/');
    _cachedPredictions = data;
    clearInterval(loadInterval);
    if (barEl) barEl.style.width = '100%';
    setTimeout(() => {
      if (loadingEl) loadingEl.style.display = 'none';
      if (contentEl) contentEl.style.display = 'block';
      _renderPredictionsContent(data);
    }, 300);
  } catch(e) {
    clearInterval(loadInterval);
    if (loadingEl) loadingEl.style.display = 'none';
    if (contentEl) contentEl.style.display = 'block';
    console.error('Predictions error:', e);
    // Show error detail in the table so user can diagnose
    const tb = $('exit-prob-tbody');
    if (tb) tb.innerHTML = `<tr><td colspan="5" class="table-empty">Predictions failed (${esc(e.message || 'network error')}). Check that you are logged in and fund data is imported.</td></tr>`;
    // Still show content area with empty state rather than blank page
    const peer = $('peer-bench-tbody');
    if (peer) peer.innerHTML = '<tr><td colspan="7" class="table-empty">—</td></tr>';
  }
}

function _renderPredictionsContent(data) {
  // KPIs
  if (data.portfolio_insights) _renderPredictionKPIs(data.portfolio_insights);

  // Forecast subtitle
  const fc = data.revenue_forecast || {};
  const subEl = $('pred-forecast-sub');
  if (subEl) subEl.textContent = fc.methodology
    ? `${fc.confidence || 'medium'} confidence · CAGR ${fc.growth_cagr_pct != null ? Number(fc.growth_cagr_pct).toFixed(1) + '%' : '—'} · ${fc.methodology}`
    : 'Gemini ML forecast · 6-month horizon';

  // Exit probability table
  const exitTbody = $('exit-prob-tbody');
  const exits = data.exit_probabilities || [];
  if (exitTbody) {
    if (!exits.length) {
      exitTbody.innerHTML = '<tr><td colspan="5" class="table-empty">No portfolio data for predictions.</td></tr>';
    } else {
      exitTbody.innerHTML = exits.slice(0, 30).map(e => {
        const prob = e.exit_prob_12m != null ? Number(e.exit_prob_12m) : 0;
        const barColor = prob >= 50 ? '#34d399' : prob >= 30 ? '#60a5fa' : '#f87171';
        const probBar = `<div style="display:inline-block;width:${prob}px;max-width:80px;height:4px;background:${barColor};border-radius:2px;vertical-align:middle;margin-right:6px"></div>`;
        return `<tr>
          <td class="td-bold">${esc(e.company_name || '—')}</td>
          <td><span style="font-size:10px;padding:2px 6px;background:var(--card2);border-radius:4px">${esc(e.stage || '—')}</span></td>
          <td class="td-right">${e.moic != null ? Number(e.moic).toFixed(2) + 'x' : '—'}</td>
          <td class="td-right">${probBar}<strong>${prob}%</strong></td>
          <td style="font-size:10px;color:var(--text3)">${esc(e.expected_exit_type || '—')}</td>
        </tr>`;
      }).join('');
    }
  }

  // Revenue forecast chart
  if (fc.months && fc.values && fc.months.length) {
    const canvas = $('rev-forecast-chart');
    if (canvas) {
      if (_revForecastChart) { _revForecastChart.destroy(); _revForecastChart = null; }
      _revForecastChart = new Chart(canvas, {
        type: 'bar',
        data: {
          labels: fc.months,
          datasets: [{
            label: 'Revenue Forecast (₹Cr)',
            data: fc.values,
            backgroundColor: 'rgba(96,165,250,0.25)',
            borderColor: '#60a5fa',
            borderWidth: 2,
            borderRadius: 4,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: '#94a3b8', font: { size: 10 } }, grid: { display: false } },
            y: { ticks: { color: '#94a3b8', font: { size: 10 }, callback: v => '₹' + v + 'Cr' }, grid: { color: 'rgba(148,163,184,0.1)' } },
          },
        },
      });
    }
  }

  // Peer benchmarking table
  const peerTbody = $('peer-bench-tbody');
  const peers = data.peer_benchmarking || [];
  if (peerTbody) {
    if (!peers.length) {
      peerTbody.innerHTML = '<tr><td colspan="7" class="table-empty">No benchmarking data.</td></tr>';
    } else {
      peerTbody.innerHTML = peers.map(p => {
        const outIcon = p.outperforming ? '<span style="color:#34d399">&#8593; Outperforming</span>' : '<span style="color:#f87171">&#8595; Below benchmark</span>';
        return `<tr>
          <td class="td-bold">${esc(p.company_name || '—')}</td>
          <td>${esc(p.sector || '—')}</td>
          <td class="td-right" style="color:${p.moic > (p.benchmark_moic || 2) ? '#34d399' : 'var(--text)'}"><strong>${p.moic != null ? Number(p.moic).toFixed(2) + 'x' : '—'}</strong></td>
          <td class="td-right" style="color:var(--text3)">${p.benchmark_moic != null ? Number(p.benchmark_moic).toFixed(2) + 'x' : '—'}</td>
          <td class="td-right" style="color:${p.irr_pct > 0 ? '#34d399' : '#f87171'}">${p.irr_pct != null ? Number(p.irr_pct).toFixed(1) + '%' : '—'}</td>
          <td class="td-right" style="color:var(--text3)">${p.benchmark_irr != null ? Number(p.benchmark_irr).toFixed(1) + '%' : '—'}</td>
          <td class="td-center">${outIcon}</td>
        </tr>`;
      }).join('');
    }
  }
}

/* ── IC Workflow ───────────────────────────────────────────── */
async function loadICWorkflow() {
  // Stage labels for the funnel cards — keys must match DealPipeline.stage choices
  const FUNNEL_STAGES = [
    { key: 'sourced',          label: 'Sourced',         color: 'cyan'   },
    { key: 'initial_screen',   label: 'Screening',        color: 'blue'   },
    { key: 'deep_dive',        label: 'Due Diligence',    color: 'gold'   },
    { key: 'term_sheet',       label: 'Term Sheet',       color: 'orange' },
    { key: 'ic_presentation',  label: 'IC Presentation',  color: 'purple' },
    { key: 'approved',         label: 'Approved',         color: 'green'  },
  ];

  // Friendly display labels for the pipeline table Stage column
  const STAGE_LABEL = {
    sourced:         'Sourced',
    initial_screen:  'Screening',
    deep_dive:       'Due Diligence',
    term_sheet:      'Term Sheet',
    ic_presentation: 'IC Presentation',
    approved:        'Approved',
    rejected:        'Rejected',
    closed:          'Closed',
    passed:          'Passed',
  };

  try {
    const fundQS = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const [sumRes, dealsRes] = await Promise.allSettled([
      Auth.apiGet(`/ic/pipeline/summary/${fundQS}`),
      Auth.apiGet(`/ic/pipeline/${fundQS}`),
    ]);

    // pipeline_summary now returns a flat dict: { sourced: N, initial_screen: N, ... }
    const summary = sumRes.value || {};
    const deals   = dealsRes.value?.results || dealsRes.value || [];

    // ── Funnel KPI cards ──
    const funnelEl = $('ic-funnel');
    if (funnelEl) {
      funnelEl.innerHTML = FUNNEL_STAGES.map(s => {
        // Use summary count (flat dict) first; fall back to counting from deals array
        const count = summary[s.key] != null
          ? summary[s.key]
          : deals.filter(d => d.stage === s.key || (s.key === 'approved' && d.stage === 'closed')).length;
        return `<div class="v5-kpi-card ${s.color}">
          <div class="v5-kpi-label">${s.label}</div>
          <div class="v5-kpi-value">${count}</div>
          <div class="v5-kpi-sub">companies</div>
        </div>`;
      }).join('');
    }

    // ── Pipeline table ──
    const tbody = $('ic-pipeline-tbody');
    if (!tbody) return;

    if (!deals.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="table-empty">No deals in pipeline.</td></tr>';
      return;
    }

    tbody.innerHTML = deals.map(d => {
      const stageLabel = STAGE_LABEL[d.stage] || d.stage || '—';
      const stageClass = d.stage === 'approved' || d.stage === 'closed'
        ? 'v5-status active'
        : d.stage === 'rejected'
          ? 'v5-status overdue'
          : 'v5-status pending';
      const ticket = d.proposed_investment_inr
        ? fmtCr(parseFloat(d.proposed_investment_inr)) + ' Cr'
        : '—';
      const owner = d.sourced_by_name || d.sourced_by || '—';
      const updated = d.updated_at
        ? new Date(d.updated_at).toLocaleDateString('en-IN')
        : '—';
      return `<tr>
        <td class="td-bold">${esc(d.company_name || '—')}</td>
        <td>${esc(d.sector || '—')}</td>
        <td><span class="${stageClass}">${esc(stageLabel)}</span></td>
        <td class="td-right">${ticket}</td>
        <td>${esc(owner)}</td>
        <td>${updated}</td>
      </tr>`;
    }).join('');

  } catch(e) {
    console.error('IC Workflow error:', e);
    const tbody = $('ic-pipeline-tbody');
    if (tbody) tbody.innerHTML = '<tr><td colspan="6" class="table-empty">Error loading pipeline data.</td></tr>';
  }
}

/* ── MIS ───────────────────────────────────────────────────── */
async function loadMIS() {
  try {
    const [bvaRes, consRes] = await Promise.allSettled([
      Auth.apiGet('/mis/bva/'),
      Auth.apiGet('/mis/consolidated/'),
    ]);

    const bva  = bvaRes.value?.results  || bvaRes.value  || [];
    const cons = consRes.value?.results || consRes.value || [];

    const kpiEl = $('mis-kpis');
    if (kpiEl) {
      let totRev=0, totEbit=0, totPat=0;
      cons.forEach(c => {
        totRev  += parseFloat(c.total_revenue || 0);
        totEbit += parseFloat(c.total_ebitda  || 0);
        totPat  += parseFloat(c.total_pat     || 0);
      });
      kpiEl.innerHTML = `
        <div class="v5-kpi-card blue"><div class="v5-kpi-label">Total Revenue</div><div class="v5-kpi-value">${fmtCr(totRev)}</div><div class="v5-kpi-sub">Consolidated (Cr)</div></div>
        <div class="v5-kpi-card green"><div class="v5-kpi-label">Total EBITDA</div><div class="v5-kpi-value">${fmtCr(totEbit)}</div><div class="v5-kpi-sub">Portfolio-wide (Cr)</div></div>
        <div class="v5-kpi-card gold"><div class="v5-kpi-label">Total PAT</div><div class="v5-kpi-value">${fmtCr(totPat)}</div><div class="v5-kpi-sub">After tax (Cr)</div></div>
        <div class="v5-kpi-card red"><div class="v5-kpi-label">Anomaly Alerts</div><div class="v5-kpi-value" id="mis-anomaly-count">—</div><div class="v5-kpi-sub">Active</div></div>
        <div class="v5-kpi-card cyan"><div class="v5-kpi-label">Companies</div><div class="v5-kpi-value">${cons.length}</div><div class="v5-kpi-sub">MIS reporting</div></div>`;

      // Anomaly count — field is 'resolved' (boolean)
      Auth.apiGet('/mis/anomalies/').then(d => {
        const anomEl = $('mis-anomaly-count');
        if (anomEl) {
          const arr = Array.isArray(d) ? d : (d.results || []);
          anomEl.textContent = arr.filter(a => !a.resolved).length;
        }
      }).catch(()=>{});
    }

    // BvA table
    const tbody = $('mis-bva-tbody');
    if (tbody) {
      tbody.innerHTML = bva.slice(0,25).map(r => {
        const v    = parseFloat(r.variance_inr || 0);
        const vPct = r.variance_pct != null ? `${Number(r.variance_pct).toFixed(1)}%` : '—';
        const favClass = r.is_favorable ? 'v5-text-green' : 'v5-text-red';
        return `<tr>
          <td class="td-bold">${esc(r.company_name || '—')}</td>
          <td>${esc(r.line_item_display || r.line_item || '—')}</td>
          <td class="td-right">${fmtCr(r.budget_inr)}</td>
          <td class="td-right">${fmtCr(r.actual_inr)}</td>
          <td>
            <div class="v5-var-cell">
              <div class="v5-var-bar ${v<0?'neg':''}" style="width:${Math.min(Math.abs(r.variance_pct||0),100)}px"></div>
              <span class="v5-var-pct ${v>=0?'pos':'neg'}">${v>=0?'+':''}${vPct}</span>
            </div>
          </td>
          <td class="td-center">${r.is_favorable ? '<span class="v5-status active">&#10003;</span>' : '<span class="v5-status overdue">&#10007;</span>'}</td>
        </tr>`;
      }).join('') || '<tr><td colspan="6" class="table-empty">No BvA data. Import MIS data first.</td></tr>';
    }
  } catch(e) {
    console.error('MIS load error:', e);
  }
}

/* ── AI Chatbot ────────────────────────────────────────────── */

/** Simple markdown→HTML: **bold**, *italic*, bullet points, line breaks */
function mdToHtml(text) {
  if (!text) return '';
  let h = esc(text);
  // Bold
  h = h.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // Italic
  h = h.replace(/\*(.+?)\*/g, '<em>$1</em>');
  // Bullet points (lines starting with • or -)
  h = h.replace(/^[•\-]\s+(.+)$/gm, '<li>$1</li>');
  h = h.replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>');
  // Line breaks
  h = h.replace(/\n/g, '<br>');
  return h;
}

/** Render a Chart.js chart from chatbot chart data */
function renderChatChart(container, chartData) {
  if (!chartData || !chartData.labels || !chartData.datasets) return;
  const wrapper = document.createElement('div');
  wrapper.className = 'v5-chat-chart';
  wrapper.style.cssText = 'margin-top:12px;background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:14px;';
  const canvas = document.createElement('canvas');
  canvas.height = 200;
  wrapper.appendChild(canvas);
  container.appendChild(wrapper);

  const datasets = chartData.datasets.map(ds => ({
    label: ds.label,
    data: ds.data,
    backgroundColor: chartData.type === 'doughnut'
      ? chartData.datasets[0].data.map((_, i) => ['#00d4ff','#7c3aed','#10b981','#f59e0b','#ef4444','#3b82f6','#ec4899','#14b8a6'][i % 8])
      : ds.color + '99',
    borderColor: ds.color,
    borderWidth: chartData.type === 'doughnut' ? 2 : 2,
    fill: chartData.type === 'line',
    tension: 0.3,
    pointRadius: chartData.type === 'line' ? 3 : 0,
  }));

  try {
    new Chart(canvas, {
      type: chartData.type || 'bar',
      data: { labels: chartData.labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: datasets.length > 1 || chartData.type === 'doughnut', labels: { color: '#94a3b8', font: { size: 10 } } },
          title: { display: !!chartData.title, text: chartData.title || '', color: '#e2e8f0', font: { size: 12 } },
        },
        scales: chartData.type === 'doughnut' ? {} : {
          x: { ticks: { color: '#94a3b8', font: { size: 9 }, maxRotation: 45 }, grid: { color: '#1e293b' } },
          y: { ticks: { color: '#94a3b8', font: { size: 9 } }, grid: { color: '#1e293b' } },
        },
      },
    });
  } catch (e) { /* Chart.js not loaded or error */ }
}

let _aiChatLoading = false;
async function sendAIChat(text) {
  const inp  = $('ai-chat-inp');
  const msgs = $('ai-chat-msgs');
  const q    = text || (inp ? inp.value.trim() : '');
  if (!q || !msgs || _aiChatLoading) return;
  if (inp) inp.value = '';
  _aiChatLoading = true;

  msgs.innerHTML += `
    <div class="v5-chat-msg user">
      <div class="v5-chat-avatar-ai" style="background:linear-gradient(135deg,#7c3aed,#2563eb)">U</div>
      <div class="v5-chat-bubble">${esc(q)}</div>
    </div>`;
  msgs.scrollTop = msgs.scrollHeight;

  const typingId = 'ai-typing-' + Date.now();
  msgs.innerHTML += `
    <div class="v5-chat-msg" id="${typingId}">
      <div class="v5-chat-avatar-ai">AI</div>
      <div class="v5-chat-bubble"><div class="v5-typing"><div class="v5-typing-dot"></div><div class="v5-typing-dot"></div><div class="v5-typing-dot"></div></div></div>
    </div>`;
  msgs.scrollTop = msgs.scrollHeight;

  try {
    const token  = Auth.getToken();
    const apiBase = (window.location.port === '8000' || !window.location.port) ? '' : 'http://127.0.0.1:8000';
    const payload = { query: q };
    if (_ctx.fundId) {
      payload.fund_id = _ctx.fundId;
      if (_ctx.fundName) payload.fund_name = _ctx.fundName;
    }
    const res    = await fetch(`${apiBase}/api/chatbot/query/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
      body: JSON.stringify(payload),
    });
    const data  = await res.json();
    const reply = data.response || 'No response from AI.';
    const typingEl = $(typingId);
    if (typingEl) {
      const bubble = typingEl.querySelector('.v5-chat-bubble');
      bubble.innerHTML = mdToHtml(reply);
      // Render chart if present
      if (data.chart) renderChatChart(bubble, data.chart);
      // Render data table if rows present and > 1 row
      if (data.data && data.data.columns && data.data.rows && data.data.rows.length > 1) {
        const tbl = document.createElement('div');
        tbl.className = 'v5-chat-data-table';
        tbl.style.cssText = 'margin-top:10px;max-height:200px;overflow:auto;font-size:11px;';
        const cols = data.data.columns;
        const rows = data.data.rows.slice(0, 15);
        let html = '<table class="v5-table" style="font-size:11px;"><thead><tr>';
        cols.forEach(c => html += `<th>${esc(c)}</th>`);
        html += '</tr></thead><tbody>';
        rows.forEach(r => {
          html += '<tr>';
          r.forEach(v => html += `<td>${esc(String(v ?? '—'))}</td>`);
          html += '</tr>';
        });
        html += '</tbody></table>';
        tbl.innerHTML = html;
        bubble.appendChild(tbl);
      }
    }
  } catch(e) {
    const typingEl = $(typingId);
    if (typingEl) typingEl.querySelector('.v5-chat-bubble').textContent = 'AI service unavailable. Please try again.';
  }
  _aiChatLoading = false;
  msgs.scrollTop = msgs.scrollHeight;
}

/* ── Theme toggle ──────────────────────────────────────────── */
function toggleV5Theme() {
  const isLight = document.documentElement.getAttribute('data-theme') === 'light';
  document.documentElement.setAttribute('data-theme', isLight ? '' : 'light');
  localStorage.setItem('tfai_theme', isLight ? '' : 'light');
  const btn = $('theme-btn');
  if (btn) btn.textContent = isLight ? '☾' : '☀';
}

/* ── Notifications ─────────────────────────────────────────── */
function toggleNotifDrawer() {
  const d = $('notif-drawer');
  if (d) d.style.display = d.style.display === 'none' ? 'block' : 'none';
}

async function loadNotifCount() {
  try {
    const data  = await Auth.apiGet('/notifications/unread-count/');
    const badge = $('notif-badge');
    if (!badge) return;
    const count = data.unread_count || 0;
    badge.textContent = count;
    badge.classList.toggle('zero', !count);
  } catch(e) {}
}

/* ── Init ──────────────────────────────────────────────────── */
(async function init() {
  if (typeof Auth !== 'undefined' && !Auth.isLoggedIn()) {
    window.location.href = 'login.html';
    return;
  }

  if (typeof Auth !== 'undefined') {
    const user = Auth.getUser();
    const badge = $('user-badge');
    if (badge && user) {
      badge.textContent = (user.first_name || user.username || '?') + ' · ' + (user.role || '').replace('_',' ').toUpperCase();
    }
    const logoutBtn = $('btn-logout');
    if (logoutBtn) logoutBtn.onclick = () => Auth.logout();
  }

  // ── CRITICAL: Listen to fund/period context changes from FundSelector ──
  // FundSelector fires 'tfai:context-change' with { fundId, fundName, period, dateStart, dateEnd }
  document.addEventListener('tfai:context-change', async (e) => {
    await onContextChange(e.detail);
  });

  // Loading bar animation
  const loadBar = $('v5-load-bar');
  if (loadBar) {
    let pct = 0;
    const tick = setInterval(() => {
      pct = Math.min(pct + Math.random() * 15, 90);
      loadBar.style.width = pct + '%';
    }, 120);

    // FundSelector.mount() fires an initial tfai:context-change that bootstraps the first load.
    // We also load the fund list and notifications in parallel.
    await Promise.allSettled([loadFundList(), loadNotifCount()]);

    pct = 100; loadBar.style.width = '100%';
    clearInterval(tick);
    setTimeout(() => {
      const ls = $('v5-loading');
      if (ls) ls.classList.add('hidden');
    }, 400);
  }

  // Initialize sidebar sub-tabs for the currently active page
  updateSidebarSubtabs(_currentPage);

  // loadFundList already triggers onContextChange → loadOverview via the select.
  // Fallback: if no overview rendered within 1.5s (e.g. auth error), load it anyway.
  setTimeout(() => {
    if (!_pageRendered['overview']) {
      _pageRendered['overview'] = true;
      loadOverview();
    }
  }, 1500);

/* ══════════════════════════════════════════════════════════════
   EXPORT FUNCTIONS — Full Report PDF, Portfolio CSV, Accounting CSV
══════════════════════════════════════════════════════════════ */

/* ── Helpers ───────────────────────────────────────────────── */
function _csvEsc(val) {
  if (val == null) return '';
  const s = String(val);
  if (s.includes(',') || s.includes('"') || s.includes('\n')) return '"' + s.replace(/"/g, '""') + '"';
  return s;
}
function _csvRow(arr) { return arr.map(_csvEsc).join(',') + '\n'; }
function _downloadCSV(filename, csvStr) {
  const bom = '\uFEFF';
  const blob = new Blob([bom + csvStr], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}
function _numVal(v) { return v == null ? 0 : parseFloat(v) || 0; }
function _numStr(v, dec) { return v == null ? '' : Number(v).toFixed(dec ?? 2); }

/* ══════════════════════════════════════════════════════════════
   HOC-1: FULL REPORT PDF — exportFullReportPDF()
══════════════════════════════════════════════════════════════ */
window.exportFullReportPDF = async function() {
  const btn = $('btn-full-report');
  if (btn) { btn.disabled = true; btn.textContent = 'Generating PDF…'; }

  try {
    const schemeIds = _ctx.schemeIds;
    const qs  = schemeQS(schemeIds);
    const fqs = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';

    // Fetch all data in parallel
    const cosUrl = '/portfolio-companies/' + fqs;
    const [cosRes, invRes, navRes, callsRes, distsRes, exitsRes] = await Promise.allSettled([
      Auth.apiGet(cosUrl),
      getInvestmentsForContext(schemeIds),
      Auth.apiGet('/accounting/nav/' + (qs ? '?' + qs : '')),
      Auth.apiGet('/lp/capital-calls/' + (qs ? '?' + qs : '')),
      Auth.apiGet('/lp/distributions/' + (qs ? '?' + qs : '')),
      Auth.apiGet('/portfolio/exits/' + fqs),
    ]);

    const cos   = cosRes.value?.results  || cosRes.value  || [];
    const invs  = Array.isArray(invRes.value) ? invRes.value : [];
    const navs  = (navRes.value?.results  || navRes.value  || []);
    const calls = (callsRes.value?.results || callsRes.value || []);
    const dists = (distsRes.value?.results || distsRes.value || []);
    const exitsData = exitsRes.value || {};
    const exits = exitsData.exits || [];
    const exitSummary = exitsData.summary || {};

    // Compute KPIs
    const active = cos.filter(c => c.is_active).length;
    const inactive = cos.length - active;
    let totalCost = 0, totalFV = 0;
    invs.forEach(inv => { totalCost += _numVal(inv.total_invested); totalFV += _numVal(inv.latest_valuation); });
    if (!totalFV && navs.length) {
      const latest = {}; navs.forEach(n => { if (!latest[n.scheme] || n.nav_date > latest[n.scheme].nav_date) latest[n.scheme] = n; });
      Object.values(latest).forEach(n => { totalFV += _numVal(n.total_nav); });
    }
    if (!totalCost && calls.length) calls.forEach(c => { totalCost += _numVal(c.total_call_amount); });
    const moic = totalCost > 0 ? totalFV / totalCost : 0;
    let totalDist = 0; dists.forEach(d => { totalDist += _numVal(d.total_net_amount); });
    const dpi  = totalCost > 0 ? totalDist / totalCost : 0;
    const tvpi = totalCost > 0 ? (totalFV + totalDist) / totalCost : 0;

    // IRR
    let irrNum = 0, irrDen = 0;
    invs.forEach(inv => { if (inv.irr_pct != null) { const w = _numVal(inv.total_invested); irrNum += parseFloat(inv.irr_pct) * w; irrDen += w; }});
    let netIrr = irrDen > 0 ? irrNum / irrDen : null;
    if (netIrr === null && _ctx.fundId) {
      try { const misIrr = await Auth.apiGet(`/mis/consolidated/?fund=${_ctx.fundId}&line_item=net_irr`);
        const r = (Array.isArray(misIrr) ? misIrr : (misIrr.results||[])).find(r => r.line_item === 'net_irr');
        if (r?.total_actual_inr != null) netIrr = parseFloat(r.total_actual_inr);
      } catch(e){}
    }

    // Sector data
    const sectorFV = {}, sectorCost = {}, sectorCount = {};
    cos.forEach(c => { const s = c.sector || 'Other'; sectorCount[s] = (sectorCount[s]||0)+1; });
    invs.forEach(inv => { const s = inv.sector || 'Other'; sectorFV[s] = (sectorFV[s]||0)+_numVal(inv.latest_valuation); sectorCost[s] = (sectorCost[s]||0)+_numVal(inv.total_invested); });

    // Top companies by FV
    const costMap = {}, fvMap = {};
    invs.forEach(inv => { const n = inv.company_name || inv.portfolio_company_name || '';
      costMap[n] = (costMap[n]||0) + _numVal(inv.total_invested);
      fvMap[n]   = (fvMap[n]||0)   + _numVal(inv.latest_valuation);
    });
    const topCos = [...cos].sort((a,b) => (fvMap[b.name]||0) - (fvMap[a.name]||0));

    // Stage, Geo, Co-Investor data
    const stageMap = {}; cos.forEach(c => { const s = c.sector || 'Unknown'; stageMap[s] = (stageMap[s]||0)+1; });
    const geoMap = {};   cos.forEach(c => { const g = c.headquarters_city || c.headquarters_country || 'Unknown'; geoMap[g] = (geoMap[g]||0)+1; });

    // NAV trend
    const navByDate = {};
    [...navs].sort((a,b) => (a.nav_date||'') < (b.nav_date||'') ? -1 : 1)
      .forEach(n => { navByDate[n.nav_date] = (navByDate[n.nav_date]||0) + _numVal(n.total_nav); });

    // Build PDF HTML
    const now = new Date().toLocaleDateString('en-IN', {day:'2-digit',month:'short',year:'numeric'});
    const reportHtml = `
    <div style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;color:#1a1a2e;padding:30px;max-width:900px;margin:auto;">

      <!-- Header -->
      <div style="text-align:center;margin-bottom:30px;border-bottom:3px solid #0066cc;padding-bottom:20px;">
        <h1 style="font-size:26px;color:#0066cc;margin:0;">TrackFundAI — Fund Intelligence Report</h1>
        <p style="font-size:14px;color:#555;margin:6px 0 0;">${esc(_ctx.fundName)} &middot; Generated ${now}</p>
      </div>

      <!-- 1. Overview KPIs -->
      <h2 style="font-size:18px;color:#0066cc;border-bottom:1px solid #ddd;padding-bottom:6px;">1. Fund Overview</h2>
      <table style="width:100%;border-collapse:collapse;margin-bottom:20px;font-size:13px;">
        <tr><td style="padding:8px;border:1px solid #ddd;font-weight:600;width:50%;">Portfolio Companies</td><td style="padding:8px;border:1px solid #ddd;">${cos.length} (${active} Active, ${inactive} Inactive)</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;font-weight:600;">Total Fair Value</td><td style="padding:8px;border:1px solid #ddd;">${fmtCr(totalFV)}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;font-weight:600;">Total Cost Deployed</td><td style="padding:8px;border:1px solid #ddd;">${fmtCr(totalCost)}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;font-weight:600;">Portfolio MOIC</td><td style="padding:8px;border:1px solid #ddd;">${fmtX(moic)} (Gross, Unrealized)</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;font-weight:600;">Net IRR</td><td style="padding:8px;border:1px solid #ddd;">${netIrr != null ? netIrr.toFixed(1) + '%' : '—'}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;font-weight:600;">TVPI</td><td style="padding:8px;border:1px solid #ddd;">${fmtX(tvpi)}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;font-weight:600;">DPI</td><td style="padding:8px;border:1px solid #ddd;">${fmtX(dpi)}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;font-weight:600;">Total Distributions</td><td style="padding:8px;border:1px solid #ddd;">${fmtCr(totalDist)}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;font-weight:600;">Deployment</td><td style="padding:8px;border:1px solid #ddd;">${fmtCr(totalCost)}${_ctx.corpusTarget ? ' (' + ((totalCost/_ctx.corpusTarget)*100).toFixed(1) + '% of corpus)' : ''}</td></tr>
      </table>

      <!-- 2. Sector Allocation by Fair Value -->
      <h2 style="font-size:18px;color:#0066cc;border-bottom:1px solid #ddd;padding-bottom:6px;">2. Sector Allocation by Fair Value</h2>
      <table style="width:100%;border-collapse:collapse;margin-bottom:20px;font-size:12px;">
        <tr style="background:#f0f4f8;"><th style="padding:6px 8px;border:1px solid #ddd;text-align:left;">Sector</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:right;">Fair Value (Cr)</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:right;">% of Total</th></tr>
        ${Object.entries(sectorFV).sort(([,a],[,b])=>b-a).map(([s,v]) => `<tr><td style="padding:6px 8px;border:1px solid #ddd;">${esc(s)}</td><td style="padding:6px 8px;border:1px solid #ddd;text-align:right;">${fmtCr(v)}</td><td style="padding:6px 8px;border:1px solid #ddd;text-align:right;">${totalFV > 0 ? (v/totalFV*100).toFixed(1)+'%' : '—'}</td></tr>`).join('')}
      </table>

      <!-- 3. Sector Allocation by Cost -->
      <h2 style="font-size:18px;color:#0066cc;border-bottom:1px solid #ddd;padding-bottom:6px;">3. Sector Allocation by Cost</h2>
      <table style="width:100%;border-collapse:collapse;margin-bottom:20px;font-size:12px;">
        <tr style="background:#f0f4f8;"><th style="padding:6px 8px;border:1px solid #ddd;text-align:left;">Sector</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:right;">Cost (Cr)</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:right;">% of Total</th></tr>
        ${Object.entries(sectorCost).sort(([,a],[,b])=>b-a).map(([s,v]) => `<tr><td style="padding:6px 8px;border:1px solid #ddd;">${esc(s)}</td><td style="padding:6px 8px;border:1px solid #ddd;text-align:right;">${fmtCr(v)}</td><td style="padding:6px 8px;border:1px solid #ddd;text-align:right;">${totalCost > 0 ? (v/totalCost*100).toFixed(1)+'%' : '—'}</td></tr>`).join('')}
      </table>

      <!-- 4. Sector Allocation by Count -->
      <h2 style="font-size:18px;color:#0066cc;border-bottom:1px solid #ddd;padding-bottom:6px;">4. Sector Allocation by Company Count</h2>
      <table style="width:100%;border-collapse:collapse;margin-bottom:20px;font-size:12px;">
        <tr style="background:#f0f4f8;"><th style="padding:6px 8px;border:1px solid #ddd;text-align:left;">Sector</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:right;">Count</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:right;">% of Total</th></tr>
        ${Object.entries(sectorCount).sort(([,a],[,b])=>b-a).map(([s,v]) => `<tr><td style="padding:6px 8px;border:1px solid #ddd;">${esc(s)}</td><td style="padding:6px 8px;border:1px solid #ddd;text-align:right;">${v}</td><td style="padding:6px 8px;border:1px solid #ddd;text-align:right;">${cos.length > 0 ? (v/cos.length*100).toFixed(1)+'%' : '—'}</td></tr>`).join('')}
      </table>

      <!-- 5. Performance Snapshot & NAV Trends -->
      <h2 style="font-size:18px;color:#0066cc;border-bottom:1px solid #ddd;padding-bottom:6px;">5. Performance Snapshot & NAV Trends</h2>
      <table style="width:100%;border-collapse:collapse;margin-bottom:12px;font-size:13px;">
        <tr><td style="padding:8px;border:1px solid #ddd;font-weight:600;width:50%;">MOIC</td><td style="padding:8px;border:1px solid #ddd;">${fmtX(moic)}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;font-weight:600;">TVPI</td><td style="padding:8px;border:1px solid #ddd;">${fmtX(tvpi)}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;font-weight:600;">DPI</td><td style="padding:8px;border:1px solid #ddd;">${fmtX(dpi)}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;font-weight:600;">Net IRR</td><td style="padding:8px;border:1px solid #ddd;">${netIrr != null ? netIrr.toFixed(1)+'%' : '—'}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;font-weight:600;">Active / Total Companies</td><td style="padding:8px;border:1px solid #ddd;">${active} / ${cos.length}</td></tr>
      </table>
      <h3 style="font-size:14px;color:#333;margin:12px 0 6px;">NAV Trend (Recent Periods)</h3>
      <table style="width:100%;border-collapse:collapse;margin-bottom:20px;font-size:12px;">
        <tr style="background:#f0f4f8;"><th style="padding:6px 8px;border:1px solid #ddd;text-align:left;">Period</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:right;">Total NAV (Cr)</th></tr>
        ${Object.entries(navByDate).sort(([a],[b])=>a<b?-1:1).slice(-12).map(([d,v]) => `<tr><td style="padding:6px 8px;border:1px solid #ddd;">${d}</td><td style="padding:6px 8px;border:1px solid #ddd;text-align:right;">${fmtCr(v)}</td></tr>`).join('') || '<tr><td colspan="2" style="padding:6px 8px;border:1px solid #ddd;color:#999;">No NAV data</td></tr>'}
      </table>

      <!-- 6. Top Portfolio Companies by Fair Value -->
      <h2 style="font-size:18px;color:#0066cc;border-bottom:1px solid #ddd;padding-bottom:6px;">6. Top Portfolio Companies by Fair Value</h2>
      <table style="width:100%;border-collapse:collapse;margin-bottom:20px;font-size:12px;">
        <tr style="background:#f0f4f8;"><th style="padding:6px 8px;border:1px solid #ddd;text-align:left;">#</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:left;">Company</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:left;">Sector</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:right;">Cost (Cr)</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:right;">FV (Cr)</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:right;">MOIC</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:center;">Status</th></tr>
        ${topCos.map((c, i) => { const cost = costMap[c.name]||0; const fv = fvMap[c.name]||0; const m = cost > 0 ? (fv/cost).toFixed(2)+'x' : '—';
          return `<tr><td style="padding:6px 8px;border:1px solid #ddd;">${i+1}</td><td style="padding:6px 8px;border:1px solid #ddd;font-weight:600;">${esc(c.name)}</td><td style="padding:6px 8px;border:1px solid #ddd;">${esc(c.sector||'—')}</td><td style="padding:6px 8px;border:1px solid #ddd;text-align:right;">${cost > 0 ? fmtCr(cost) : '—'}</td><td style="padding:6px 8px;border:1px solid #ddd;text-align:right;">${fv > 0 ? fmtCr(fv) : '—'}</td><td style="padding:6px 8px;border:1px solid #ddd;text-align:right;">${m}</td><td style="padding:6px 8px;border:1px solid #ddd;text-align:center;">${c.is_active ? 'Active' : 'Inactive'}</td></tr>`; }).join('')}
      </table>

      <!-- 7. Stage, Geography & Co-Investors -->
      <h2 style="font-size:18px;color:#0066cc;border-bottom:1px solid #ddd;padding-bottom:6px;">7. Stage, Geography & Co-Investors</h2>
      <div style="display:flex;gap:30px;margin-bottom:20px;">
        <div style="flex:1;">
          <h3 style="font-size:14px;color:#333;margin:0 0 8px;">Stage / Sector Mix</h3>
          <table style="width:100%;border-collapse:collapse;font-size:12px;">
            ${Object.entries(stageMap).sort(([,a],[,b])=>b-a).map(([s,c]) => `<tr><td style="padding:4px 8px;border:1px solid #ddd;">${esc(s)}</td><td style="padding:4px 8px;border:1px solid #ddd;text-align:right;">${c}</td></tr>`).join('')}
          </table>
        </div>
        <div style="flex:1;">
          <h3 style="font-size:14px;color:#333;margin:0 0 8px;">Geography</h3>
          <table style="width:100%;border-collapse:collapse;font-size:12px;">
            ${Object.entries(geoMap).sort(([,a],[,b])=>b-a).slice(0,15).map(([g,c]) => `<tr><td style="padding:4px 8px;border:1px solid #ddd;">${esc(g)}</td><td style="padding:4px 8px;border:1px solid #ddd;text-align:right;">${c}</td></tr>`).join('')}
          </table>
        </div>
      </div>

      <!-- 8. Recent Capital Calls -->
      <h2 style="font-size:18px;color:#0066cc;border-bottom:1px solid #ddd;padding-bottom:6px;">8. Recent Capital Calls</h2>
      <table style="width:100%;border-collapse:collapse;margin-bottom:20px;font-size:12px;">
        <tr style="background:#f0f4f8;"><th style="padding:6px 8px;border:1px solid #ddd;text-align:left;">#</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:left;">Scheme / LP</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:left;">Call Date</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:right;">Amount (Cr)</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:center;">Status</th></tr>
        ${calls.slice(0,20).map((c,i) => { const lp = (c.purpose||c.scheme_name||'—').split(' — ')[0];
          return `<tr><td style="padding:6px 8px;border:1px solid #ddd;">${i+1}</td><td style="padding:6px 8px;border:1px solid #ddd;">${esc(lp)}</td><td style="padding:6px 8px;border:1px solid #ddd;">${c.call_date||'—'}</td><td style="padding:6px 8px;border:1px solid #ddd;text-align:right;">${fmtCr(_numVal(c.total_call_amount))}</td><td style="padding:6px 8px;border:1px solid #ddd;text-align:center;">${esc(c.status_display||c.call_status||'—')}</td></tr>`; }).join('') || '<tr><td colspan="5" style="padding:6px 8px;border:1px solid #ddd;color:#999;">No capital calls</td></tr>'}
      </table>

      <!-- 9. Realized Returns (Exits) -->
      <h2 style="font-size:18px;color:#0066cc;border-bottom:1px solid #ddd;padding-bottom:6px;">9. Realized Returns (Exits)</h2>
      ${exits.length ? `
      <table style="width:100%;border-collapse:collapse;margin-bottom:12px;font-size:13px;">
        <tr><td style="padding:6px;border:1px solid #ddd;font-weight:600;">Total Proceeds</td><td style="padding:6px;border:1px solid #ddd;">${exitSummary.total_proceeds ? fmtCr(exitSummary.total_proceeds) : '—'}</td><td style="padding:6px;border:1px solid #ddd;font-weight:600;">Avg Exit MOIC</td><td style="padding:6px;border:1px solid #ddd;">${exitSummary.avg_moic != null ? fmtX(exitSummary.avg_moic) : '—'}</td></tr>
        <tr><td style="padding:6px;border:1px solid #ddd;font-weight:600;">DPI</td><td style="padding:6px;border:1px solid #ddd;">${exitSummary.dpi != null ? exitSummary.dpi.toFixed(2)+'x' : '—'}</td><td style="padding:6px;border:1px solid #ddd;font-weight:600;">Total Exits</td><td style="padding:6px;border:1px solid #ddd;">${exitSummary.total_exits || exits.length}</td></tr>
      </table>
      <table style="width:100%;border-collapse:collapse;margin-bottom:20px;font-size:12px;">
        <tr style="background:#f0f4f8;"><th style="padding:6px 8px;border:1px solid #ddd;text-align:left;">Company</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:left;">Sector</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:left;">Exit Type</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:right;">Cost (Cr)</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:right;">Proceeds (Cr)</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:right;">MOIC</th><th style="padding:6px 8px;border:1px solid #ddd;text-align:right;">IRR%</th><th style="padding:6px 8px;border:1px solid #ddd;">Exit Date</th></tr>
        ${exits.map(e => `<tr><td style="padding:6px 8px;border:1px solid #ddd;">${esc(e.company_name)}</td><td style="padding:6px 8px;border:1px solid #ddd;">${esc(e.sector||'—')}</td><td style="padding:6px 8px;border:1px solid #ddd;">${esc(e.exit_type_display||e.exit_type)}</td><td style="padding:6px 8px;border:1px solid #ddd;text-align:right;">${e.cost ? fmtCr(e.cost) : '—'}</td><td style="padding:6px 8px;border:1px solid #ddd;text-align:right;">${e.proceeds != null ? fmtCr(e.proceeds) : '—'}</td><td style="padding:6px 8px;border:1px solid #ddd;text-align:right;">${e.moic != null ? e.moic.toFixed(2)+'x' : '—'}</td><td style="padding:6px 8px;border:1px solid #ddd;text-align:right;">${(e.net_irr_pct ?? e.irr_pct) != null ? (e.net_irr_pct ?? e.irr_pct).toFixed(1)+'%' : '—'}</td><td style="padding:6px 8px;border:1px solid #ddd;">${e.exit_date||'—'}</td></tr>`).join('')}
      </table>` : '<p style="color:#999;font-size:13px;">No exits recorded for this fund.</p>'}

      <!-- 10. Fund Scorecard -->
      <h2 style="font-size:18px;color:#0066cc;border-bottom:1px solid #ddd;padding-bottom:6px;">10. Fund Scorecard</h2>
      <table style="width:100%;border-collapse:collapse;margin-bottom:20px;font-size:13px;">
        <tr style="background:#f0f4f8;"><th style="padding:8px;border:1px solid #ddd;text-align:left;">Metric</th><th style="padding:8px;border:1px solid #ddd;text-align:right;">Value</th></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;">MOIC</td><td style="padding:8px;border:1px solid #ddd;text-align:right;">${fmtX(moic)}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;">Net IRR</td><td style="padding:8px;border:1px solid #ddd;text-align:right;">${netIrr != null ? netIrr.toFixed(1)+'%' : '—'}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;">TVPI</td><td style="padding:8px;border:1px solid #ddd;text-align:right;">${fmtX(tvpi)}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;">DPI</td><td style="padding:8px;border:1px solid #ddd;text-align:right;">${fmtX(dpi)}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;">Active Companies</td><td style="padding:8px;border:1px solid #ddd;text-align:right;">${active}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;">Total Companies</td><td style="padding:8px;border:1px solid #ddd;text-align:right;">${cos.length}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;">Total Cost Deployed</td><td style="padding:8px;border:1px solid #ddd;text-align:right;">${fmtCr(totalCost)}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;">Total Fair Value</td><td style="padding:8px;border:1px solid #ddd;text-align:right;">${fmtCr(totalFV)}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;">Total Distributions</td><td style="padding:8px;border:1px solid #ddd;text-align:right;">${fmtCr(totalDist)}</td></tr>
        <tr><td style="padding:8px;border:1px solid #ddd;">Avg Ticket Size</td><td style="padding:8px;border:1px solid #ddd;text-align:right;">${cos.length ? fmtCr(totalCost / cos.length) : '—'}</td></tr>
      </table>

      <div style="text-align:center;color:#999;font-size:10px;border-top:1px solid #ddd;padding-top:12px;margin-top:30px;">
        TrackFundAI &middot; Confidential &middot; ${_ctx.fundName} &middot; ${now}
      </div>
    </div>`;

    // Generate PDF with html2pdf
    const container = document.createElement('div');
    container.innerHTML = reportHtml;
    document.body.appendChild(container);

    const fileName = `TrackFundAI_Full_Report_${(_ctx.fundName||'AllFunds').replace(/[^a-zA-Z0-9]/g,'_')}_${new Date().toISOString().slice(0,10)}.pdf`;

    await html2pdf().set({
      margin:       [10, 10, 10, 10],
      filename:     fileName,
      image:        { type: 'jpeg', quality: 0.95 },
      html2canvas:  { scale: 2, useCORS: true, letterRendering: true },
      jsPDF:        { unit: 'mm', format: 'a4', orientation: 'portrait' },
      pagebreak:    { mode: ['avoid-all', 'css', 'legacy'] },
    }).from(container).save();

    document.body.removeChild(container);

  } catch(e) {
    console.error('PDF export error:', e);
    alert('Error generating PDF report. Please try again.');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '&#128202; Full Report'; }
  }
};

/* ══════════════════════════════════════════════════════════════
   HOC-2: PORTFOLIO CSV — exportPortfolioCSV()
══════════════════════════════════════════════════════════════ */
window.exportPortfolioCSV = async function() {
  const btn = $('btn-export-portfolio-csv');
  if (btn) { btn.disabled = true; btn.textContent = 'Exporting…'; }

  try {
    const fqs = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const schemeIds = _ctx.schemeIds;

    // Fetch all data in parallel
    const [cosRes, invCtx, burnRes, exitsRes, kpisRes, saasRes, qqRes, invDetailRes, valRes, kpiTrackRes, exitScenRes] = await Promise.allSettled([
      Auth.apiGet('/portfolio-companies/' + fqs),
      getInvestmentsForContext(schemeIds),
      Auth.apiGet('/portfolio/burn-runway/' + fqs),
      Auth.apiGet('/portfolio/exits/' + fqs),
      Auth.apiGet('/portfolio/kpis/' + fqs),
      Auth.apiGet('/portfolio/saas-metrics/' + fqs),
      Auth.apiGet('/portfolio/quoted-unquoted/' + fqs),
      Auth.apiGet('/portfolio/investments/' + fqs),
      Auth.apiGet('/portfolio/valuations/' + fqs),
      Auth.apiGet('/portfolio/kpi-tracking/' + fqs),
      Auth.apiGet('/portfolio/exit-scenarios/' + fqs),
    ]);

    const cos   = cosRes.value?.results || cosRes.value || [];
    const invs  = Array.isArray(invCtx.value) ? invCtx.value : [];
    const burnData  = burnRes.value || {};
    const exitsData = exitsRes.value || {};
    const kpisData  = kpisRes.value || {};
    const saasData  = saasRes.value || {};
    const qqData    = qqRes.value || {};
    const invDetailData = invDetailRes.value || {};
    const valData   = valRes.value || {};
    const kpiTrackData = kpiTrackRes.value || {};
    const exitScenData = exitScenRes.value || {};

    // Build cost/FV maps
    const costMap = {}, fvMap = {}, stageMap = {}, irrMap = {};
    invs.forEach(inv => {
      const n = inv.company_name || inv.portfolio_company_name || '';
      costMap[n] = (costMap[n]||0) + _numVal(inv.total_invested);
      fvMap[n]   = (fvMap[n]||0)   + _numVal(inv.latest_valuation);
      if (inv.stage && !stageMap[n]) stageMap[n] = inv.stage;
      if (inv.irr_pct != null && irrMap[n] == null) irrMap[n] = parseFloat(inv.irr_pct);
    });

    let totalCost = 0, totalFV = 0;
    invs.forEach(inv => { totalCost += _numVal(inv.total_invested); totalFV += _numVal(inv.latest_valuation); });

    let csv = '';

    // ── Section 1: Overview Summary ──
    csv += _csvRow(['=== PORTFOLIO OVERVIEW ===']);
    csv += _csvRow(['Metric', 'Value']);
    csv += _csvRow(['Fund', _ctx.fundName]);
    csv += _csvRow(['Total Companies', cos.length]);
    csv += _csvRow(['Active Companies', cos.filter(c => c.is_active).length]);
    csv += _csvRow(['Total Cost Deployed (Cr)', _numStr(totalCost)]);
    csv += _csvRow(['Total Fair Value (Cr)', _numStr(totalFV)]);
    csv += _csvRow(['Avg Ticket (Cr)', cos.length ? _numStr(totalCost / cos.length) : '']);
    csv += _csvRow(['Unrealized Gain (MOIC)', totalCost > 0 ? _numStr(totalFV / totalCost) + 'x' : '']);
    csv += '\n';

    // Sector Breakdown
    csv += _csvRow(['--- Sector Breakdown ---']);
    csv += _csvRow(['Sector', 'Company Count', 'FV (Cr)', '% of FV', 'Cost (Cr)']);
    const sectors = {};
    cos.forEach(c => { const s = c.sector || 'Other'; sectors[s] = (sectors[s]||0)+1; });
    const sectorFVLocal = {}, sectorCostLocal = {};
    invs.forEach(inv => { const s = inv.sector || 'Other'; sectorFVLocal[s] = (sectorFVLocal[s]||0)+_numVal(inv.latest_valuation); sectorCostLocal[s] = (sectorCostLocal[s]||0)+_numVal(inv.total_invested); });
    Object.entries(sectors).sort(([,a],[,b])=>b-a).forEach(([s,cnt]) => {
      csv += _csvRow([s, cnt, _numStr(sectorFVLocal[s]||0), totalFV > 0 ? _numStr((sectorFVLocal[s]||0)/totalFV*100,1)+'%' : '', _numStr(sectorCostLocal[s]||0)]);
    });
    csv += '\n';

    // ── Section 2: All Companies ──
    csv += _csvRow(['=== ALL PORTFOLIO COMPANIES ===']);
    csv += _csvRow(['#', 'Company', 'Sector', 'Stage', 'City', 'Cost (Cr)', 'FV (Cr)', 'IRR%', 'MOIC', 'Status']);
    cos.forEach((c, i) => {
      const cost = costMap[c.name] || 0;
      const fv   = fvMap[c.name]   || 0;
      const moic = cost > 0 ? (fv/cost).toFixed(2)+'x' : '';
      csv += _csvRow([i+1, c.name||'', c.sector||'', stageMap[c.name]||'', c.headquarters_city||'',
        _numStr(cost), _numStr(fv), irrMap[c.name] != null ? irrMap[c.name].toFixed(1) : '', moic, c.is_active ? 'Active' : 'Inactive']);
    });
    csv += '\n';

    // ── Section 3: Burn & Runway ──
    const burnCos = burnData.companies || [];
    csv += _csvRow(['=== BURN & RUNWAY ===']);
    csv += _csvRow(['Summary', 'Avg Gross Burn (Cr)', 'Avg Net Burn (Cr)', 'Avg Runway (mo)', 'Total Cash (Cr)']);
    csv += _csvRow(['', _numStr(burnData.avg_gross_burn), _numStr(burnData.avg_net_burn), burnData.avg_runway != null ? burnData.avg_runway.toFixed(1) : '', _numStr(burnData.total_cash)]);
    csv += _csvRow(['#', 'Company', 'Gross Burn (Cr)', 'Net Burn (Cr)', 'Cash Balance (Cr)', 'Runway (mo)', 'Risk']);
    burnCos.forEach((c, i) => {
      const risk = c.runway_months == null ? '' : c.runway_months < 6 ? 'High' : c.runway_months < 12 ? 'Watch' : 'Safe';
      csv += _csvRow([i+1, c.company_name||'', _numStr(c.gross_burn), _numStr(c.net_burn), _numStr(c.cash_balance), c.runway_months != null ? c.runway_months.toFixed(1) : '', risk]);
    });
    csv += '\n';

    // ── Section 4: Exits ──
    const exitsList = exitsData.exits || [];
    const exitSum = exitsData.summary || {};
    csv += _csvRow(['=== EXITS ===']);
    csv += _csvRow(['Summary', 'Total Proceeds (Cr)', 'Avg MOIC', 'Avg IRR%', 'DPI', 'Total Exits']);
    csv += _csvRow(['', _numStr(exitSum.total_proceeds), exitSum.avg_moic != null ? _numStr(exitSum.avg_moic) : '', exitSum.avg_irr != null ? _numStr(exitSum.avg_irr,1) : '', exitSum.dpi != null ? _numStr(exitSum.dpi) : '', exitSum.total_exits||'']);
    csv += _csvRow(['#', 'Company', 'Sector', 'Exit Type', 'Cost (Cr)', 'Proceeds (Cr)', 'MOIC', 'IRR%', 'Exit Date']);
    exitsList.forEach((e, i) => {
      const irr = (e.net_irr_pct ?? e.irr_pct);
      csv += _csvRow([i+1, e.company_name||'', e.sector||'', e.exit_type_display||e.exit_type||'', _numStr(e.cost), e.proceeds != null ? _numStr(e.proceeds) : '', e.moic != null ? _numStr(e.moic) : '', irr != null ? irr.toFixed(1) : '', e.exit_date||'']);
    });
    csv += '\n';

    // ── Section 5: KPIs ──
    const kpis = kpisData.kpis || [];
    csv += _csvRow(['=== KPIs ===']);
    csv += _csvRow(['#', 'Company', 'Metric', 'Value', 'Period', 'Format']);
    kpis.forEach((k, i) => {
      let v = k.value;
      if (v != null) {
        if (k.format === 'currency') v = _numStr(v);
        else if (k.format === 'percent') v = v.toFixed(2) + '%';
        else if (k.format === 'ratio') v = v.toFixed(2) + 'x';
        else v = String(v);
      } else v = '';
      csv += _csvRow([i+1, k.company_name||'', k.kpi_name||'', v, k.period||'', k.format||'']);
    });
    csv += '\n';

    // ── Section 6: SaaS Metrics ──
    const saas = saasData.companies || [];
    csv += _csvRow(['=== SaaS METRICS ===']);
    csv += _csvRow(['#', 'Company', 'Sector', 'MRR (Cr)', 'ARR (Cr)', 'NRR%', 'Churn%', 'CAC', 'LTV', 'LTV/CAC']);
    saas.forEach((c, i) => {
      csv += _csvRow([i+1, c.company_name||'', c.sector||'', _numStr(c.mrr), _numStr(c.arr), c.nrr != null ? c.nrr.toFixed(1) : '', c.churn_rate != null ? c.churn_rate.toFixed(2) : '', c.cac != null ? _numStr(c.cac,0) : '', c.ltv != null ? _numStr(c.ltv,0) : '', c.ltv_cac_ratio != null ? c.ltv_cac_ratio.toFixed(1) : '']);
    });
    csv += '\n';

    // ── Section 7: Quoted & Unquoted ──
    const quoted = qqData.quoted || [];
    const unquoted = qqData.unquoted || [];
    csv += _csvRow(['=== QUOTED (LISTED) COMPANIES ===']);
    csv += _csvRow(['#', 'Company', 'Sector', 'Exchange', 'Cost (Cr)', 'FV (Cr)', 'IPEV Level']);
    quoted.forEach((c, i) => {
      csv += _csvRow([i+1, c.name||'', c.sector||'', c.exchange||'', _numStr(c.cost), _numStr(c.fair_value), c.ipev_level||'']);
    });
    csv += '\n';
    csv += _csvRow(['=== UNQUOTED (PRIVATE) COMPANIES ===']);
    csv += _csvRow(['#', 'Company', 'Sector', 'Cost (Cr)', 'FV (Cr)', 'IPEV Level']);
    unquoted.forEach((c, i) => {
      csv += _csvRow([i+1, c.name||'', c.sector||'', _numStr(c.cost), _numStr(c.fair_value), c.ipev_level||'']);
    });
    csv += '\n';

    // ── Section 8: Investments Detail ──
    const invDetail = invDetailData.investments || [];
    csv += _csvRow(['=== INVESTMENTS DETAIL ===']);
    csv += _csvRow(['#', 'Company', 'Scheme', 'Sector', 'Stage', 'Instrument', 'Invested (Cr)', 'Ownership%', 'IRR%', 'FV (Cr)', 'Status', 'Date']);
    invDetail.forEach((inv, i) => {
      csv += _csvRow([i+1, inv.company_name||'', inv.scheme_name||'', inv.sector||'', inv.stage||'', inv.instrument_type_display||inv.instrument_type||'',
        _numStr(inv.total_invested), inv.ownership_pct != null ? inv.ownership_pct.toFixed(2) : '', inv.irr_pct != null ? inv.irr_pct.toFixed(1) : '',
        inv.latest_valuation != null ? _numStr(inv.latest_valuation) : '', inv.status_display||inv.status||'', inv.investment_date ? inv.investment_date.slice(0,10) : '']);
    });
    csv += '\n';

    // ── Section 9: Valuations ──
    const vals = valData.valuations || [];
    csv += _csvRow(['=== VALUATIONS ===']);
    csv += _csvRow(['#', 'Company', 'Scheme', 'Date', 'Fair Value (Cr)', 'Methodology', 'IPEV Level', 'MOIC', 'Status', 'Submitted By', 'Approved By']);
    vals.forEach((v, i) => {
      csv += _csvRow([i+1, v.company_name||'', v.scheme_name||'', v.valuation_date ? v.valuation_date.slice(0,10) : '', _numStr(v.fair_value),
        v.methodology_display||v.methodology||'', v.ipev_level||'', v.multiple != null ? v.multiple.toFixed(2) : '', v.status||'', v.submitted_by||'', v.approved_by||'']);
    });
    csv += '\n';

    // ── Section 10: KPI Tracking ──
    const kpiTrack = kpiTrackData.kpis || [];
    csv += _csvRow(['=== KPI TRACKING ===']);
    csv += _csvRow(['#', 'Company', 'KPI Metric', 'Period', 'Value', 'Format', 'Status', 'Submitted By', 'Submitted At', 'Reviewed By']);
    kpiTrack.forEach((k, i) => {
      let v = k.value;
      if (v != null) {
        if (k.format === 'currency') v = _numStr(v);
        else if (k.format === 'percent') v = v.toFixed(2) + '%';
        else if (k.format === 'ratio') v = v.toFixed(2) + 'x';
        else v = String(v);
      } else v = '';
      csv += _csvRow([i+1, k.company_name||'', k.kpi_name||'', k.period ? k.period.slice(0,7) : '', v, k.format||'', k.status||'', k.submitted_by||'', k.submitted_at ? k.submitted_at.slice(0,10) : '', k.reviewed_by||'']);
    });
    csv += '\n';

    // ── Section 11: Exit Scenarios ──
    const exitScens = exitScenData.scenarios || [];
    csv += _csvRow(['=== EXIT SCENARIOS ===']);
    csv += _csvRow(['#', 'Company', 'Scheme', 'Exit Type', 'Actual?', 'Exit Date', 'Proceeds (Cr)', 'MOIC', 'IRR%', 'Nature', 'Buyer']);
    exitScens.forEach((e, i) => {
      csv += _csvRow([i+1, e.company_name||'', e.scheme_name||'', e.exit_type_display||e.exit_type||'', e.is_actual ? 'Yes' : 'No',
        e.exit_date ? e.exit_date.slice(0,10) : '', e.proceeds != null ? _numStr(e.proceeds) : '', e.moic != null ? _numStr(e.moic) : '',
        e.irr_pct != null ? e.irr_pct.toFixed(1) : '', e.gain_loss_nature||'', e.buyer_name||'']);
    });

    const fileName = `TrackFundAI_Portfolio_${(_ctx.fundName||'AllFunds').replace(/[^a-zA-Z0-9]/g,'_')}_${new Date().toISOString().slice(0,10)}.csv`;
    _downloadCSV(fileName, csv);

  } catch(e) {
    console.error('Portfolio CSV error:', e);
    alert('Error exporting Portfolio CSV. Please try again.');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '&#128228; Export CSV'; }
  }
};

/* ══════════════════════════════════════════════════════════════
   HOC-3: FUND ACCOUNTING CSV — exportAccountingCSV()
══════════════════════════════════════════════════════════════ */
window.exportAccountingCSV = async function() {
  const btn = $('btn-export-accounting-csv');
  if (btn) { btn.disabled = true; btn.textContent = 'Exporting…'; }

  try {
    const fqs  = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const qs   = schemeQS(_ctx.schemeIds);

    // Fetch all accounting data in parallel
    const [navRes, carryRes, callsRes, distsRes, ledgerRes, feesRes, coaRes] = await Promise.allSettled([
      Auth.apiGet('/accounting/nav/' + (fqs || (qs ? '?' + qs : ''))),
      Auth.apiGet('/accounting/carry/' + fqs),
      Auth.apiGet('/lp/capital-calls/' + (qs ? '?' + qs : fqs)),
      Auth.apiGet('/lp/distributions/' + (qs ? '?' + qs : fqs)),
      Auth.apiGet('/accounting/ledger/' + fqs),
      Auth.apiGet('/accounting/fees/' + fqs),
      Auth.apiGet('/accounting/chart-of-accounts/'),
    ]);

    const navArr   = (navRes.value?.results   || navRes.value   || []).sort((a,b) => (b.nav_date||'') > (a.nav_date||'') ? 1 : -1);
    const carries  = carryRes.value?.results   || carryRes.value   || [];
    const calls    = callsRes.value?.results   || callsRes.value   || [];
    const dists    = distsRes.value?.results    || distsRes.value    || [];
    const ledger   = ledgerRes.value?.results   || ledgerRes.value   || [];
    const fees     = feesRes.value?.results     || feesRes.value     || [];
    const coa      = coaRes.value?.results      || coaRes.value      || [];

    let csv = '';

    // ── Section 1: NAV Summary ──
    csv += _csvRow(['=== NAV SUMMARY ===']);
    const latestByScheme = {};
    navArr.forEach(n => { if (!latestByScheme[n.scheme]) latestByScheme[n.scheme] = n; });
    const latestArr = Object.values(latestByScheme);
    const totalNav = latestArr.reduce((s,n) => s + _numVal(n.total_nav), 0);
    csv += _csvRow(['Metric', 'Value']);
    csv += _csvRow(['Total NAV (Latest)', _numStr(totalNav)]);
    csv += _csvRow(['Avg NAV per Unit', latestArr.length ? _numStr(latestArr.reduce((s,n) => s + _numVal(n.nav_per_unit), 0) / latestArr.length, 4) : '']);
    csv += _csvRow(['Total Unrealized Gains', _numStr(latestArr.reduce((s,n) => s + _numVal(n.unrealized_gains), 0))]);
    csv += _csvRow(['Total Realized Gains', _numStr(latestArr.reduce((s,n) => s + _numVal(n.realized_gains), 0))]);
    csv += _csvRow(['Total Mgmt Fee', _numStr(navArr.reduce((s,n) => s + _numVal(n.management_fee_payable), 0))]);
    csv += '\n';

    // ── Section 2: NAV History ──
    csv += _csvRow(['=== NAV HISTORY ===']);
    csv += _csvRow(['Period', 'Scheme', 'Total NAV', 'NAV per Unit', 'Units Outstanding', 'Investments at FV', 'Cash', 'Receivables', 'Mgmt Fee Payable', 'Other Liabilities', 'Unrealized Gains', 'Realized Gains', 'Reconciled', 'Depository']);
    navArr.forEach(n => {
      csv += _csvRow([n.nav_date||'', n.scheme_name||'', _numStr(n.total_nav), _numStr(n.nav_per_unit, 4), _numStr(n.total_units_outstanding, 4),
        _numStr(n.investments_at_fair_value), _numStr(n.cash_and_equivalents), _numStr(n.receivables), _numStr(n.management_fee_payable),
        _numStr(n.other_liabilities), _numStr(n.unrealized_gains), _numStr(n.realized_gains),
        n.depository_reconciled ? 'Yes' : 'No', n.depository_type||'']);
    });
    csv += '\n';

    // ── Section 3: Waterfall & Carry ──
    csv += _csvRow(['=== CARRY & CLAWBACK ===']);
    csv += _csvRow(['Scheme', 'Calculation Date', 'Status', 'Total Distributions', 'Called Capital', 'Preferred Return', 'Carry Base', 'Gross Carry', 'Net Carry', 'GP Clawback']);
    carries.forEach(c => {
      csv += _csvRow([c.scheme_name||'', c.calculation_date||'', c.status_display||c.calculation_status||'',
        _numStr(c.total_distributions), _numStr(c.total_called_capital), _numStr(c.preferred_return_amount),
        _numStr(c.carry_base), _numStr(c.carry_amount_gross), _numStr(c.carry_amount_net), _numStr(c.gp_clawback_provision)]);
    });
    csv += '\n';

    // ── Section 4: Capital Calls ──
    csv += _csvRow(['=== CAPITAL CALLS ===']);
    csv += _csvRow(['#', 'LP / Scheme', 'Call Date', 'Amount (Cr)', 'Status', 'Purpose']);
    calls.forEach((c, i) => {
      const parts = (c.purpose||c.scheme_name||'').split(' — ');
      csv += _csvRow([i+1, parts[0]||'', c.call_date||'', _numStr(c.total_call_amount), c.status_display||c.call_status||'', parts.length > 1 ? parts.slice(1).join(' — ') : '']);
    });
    csv += '\n';

    // ── Section 5: Distributions ──
    csv += _csvRow(['=== DISTRIBUTIONS ===']);
    csv += _csvRow(['#', 'Scheme / LP', 'Date', 'Amount (Cr)', 'Type', 'Status']);
    dists.forEach((d, i) => {
      csv += _csvRow([i+1, d.scheme_name||'', d.distribution_date||'', _numStr(d.total_net_amount), d.type_display||d.distribution_type||'', d.status_display||d.distribution_status||'']);
    });
    csv += '\n';

    // ── Section 6: Fund P&L ──
    csv += _csvRow(['=== FUND P&L ===']);
    csv += _csvRow(['Period', 'Scheme', 'Unrealized Gains', 'Realized Gains', 'Mgmt Fee', 'Other Liabilities', 'Gross Income', 'Total Expenses', 'Net P&L']);
    const plArr = [...navArr].sort((a,b) => (a.nav_date||'') < (b.nav_date||'') ? -1 : 1);
    plArr.forEach(n => {
      const unrealized = _numVal(n.unrealized_gains);
      const realized   = _numVal(n.realized_gains);
      const mgmtFee    = _numVal(n.management_fee_payable);
      const otherLiab  = _numVal(n.other_liabilities);
      const gross = unrealized + realized;
      const exp   = mgmtFee + otherLiab;
      csv += _csvRow([n.nav_date||'', n.scheme_name||'', _numStr(unrealized), _numStr(realized), _numStr(mgmtFee), _numStr(otherLiab), _numStr(gross), _numStr(exp), _numStr(gross - exp)]);
    });
    csv += '\n';

    // ── Section 7: Fund Ledger ──
    csv += _csvRow(['=== FUND LEDGER ===']);
    csv += _csvRow(['Entry #', 'Date', 'Description', 'Debit Account', 'Credit Account', 'Amount', 'Ref Type', 'Status']);
    ledger.forEach(e => {
      csv += _csvRow([e.journal_entry_number||'', e.entry_date||'', e.description||'', e.debit_account_name||'', e.credit_account_name||'',
        _numStr(e.amount), e.reference_type_display||e.reference_type||'', e.is_reversed ? 'REVERSED' : 'POSTED']);
    });
    csv += '\n';

    // ── Section 8: Management Fees ──
    csv += _csvRow(['=== MANAGEMENT FEES ===']);
    csv += _csvRow(['Scheme', 'Period Start', 'Period End', 'Fee Basis', 'Rate%', 'Fee Amount', 'GST', 'Total (incl GST)', 'Invoice #', 'Status']);
    fees.forEach(f => {
      csv += _csvRow([f.scheme_name||'', f.period_start||'', f.period_end||'', _numStr(f.fee_basis_amount),
        f.fee_rate ? parseFloat(f.fee_rate).toFixed(4) : '', _numStr(f.fee_amount), _numStr(f.gst_amount),
        _numStr(f.total_fee_with_gst), f.invoice_number||'', f.status_display||f.fee_status||'']);
    });
    csv += '\n';

    // ── Section 9: Chart of Accounts ──
    csv += _csvRow(['=== CHART OF ACCOUNTS ===']);
    csv += _csvRow(['Code', 'Account Name', 'Type', 'Parent Account', 'Description', 'Active']);
    coa.forEach(a => {
      csv += _csvRow([a.account_code||'', a.account_name||'', a.account_type_display||a.account_type||'', a.parent_account_name||'', a.description||'', a.is_active ? 'Yes' : 'No']);
    });

    const fileName = `TrackFundAI_Accounting_${(_ctx.fundName||'AllFunds').replace(/[^a-zA-Z0-9]/g,'_')}_${new Date().toISOString().slice(0,10)}.csv`;
    _downloadCSV(fileName, csv);

  } catch(e) {
    console.error('Accounting CSV error:', e);
    alert('Error exporting Accounting CSV. Please try again.');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '&#128228; Export CSV'; }
  }
};

/* ── Shared PDF helpers ────────────────────────────────────── */
const _pdfStyles = `
  <style>
    * { box-sizing: border-box; }
    body { font-family: 'Segoe UI', Helvetica, Arial, sans-serif; color: #1a1a2e; }
    .pdf-header { text-align:center; margin-bottom:28px; border-bottom:3px solid #0066cc; padding-bottom:18px; }
    .pdf-header h1 { font-size:24px; color:#0066cc; margin:0; }
    .pdf-header p  { font-size:13px; color:#555; margin:5px 0 0; }
    .pdf-section { margin-bottom:22px; page-break-inside:avoid; }
    .pdf-section h2 { font-size:16px; color:#0066cc; border-bottom:1px solid #d0d5dd; padding-bottom:5px; margin:0 0 10px; }
    .pdf-section h3 { font-size:13px; color:#333; margin:10px 0 6px; }
    .pdf-kpi-grid { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:14px; }
    .pdf-kpi { flex:1 1 140px; border:1px solid #d0d5dd; border-radius:6px; padding:10px 12px; background:#f8fafc; }
    .pdf-kpi .label { font-size:10px; color:#666; text-transform:uppercase; letter-spacing:.03em; }
    .pdf-kpi .value { font-size:18px; font-weight:700; color:#1a1a2e; margin-top:2px; }
    .pdf-kpi .sub   { font-size:10px; color:#888; margin-top:1px; }
    table { width:100%; border-collapse:collapse; font-size:11px; margin-bottom:8px; }
    th { background:#edf0f5; color:#1a1a2e; font-weight:700; text-align:left; padding:5px 7px; border:1px solid #d0d5dd; font-size:10px; }
    td { padding:4px 7px; border:1px solid #d0d5dd; color:#333; }
    .r { text-align:right; } .c { text-align:center; } .b { font-weight:600; }
    .pdf-footer { text-align:center; color:#999; font-size:9px; border-top:1px solid #d0d5dd; padding-top:10px; margin-top:24px; }
    .pdf-empty { color:#999; font-size:11px; font-style:italic; padding:8px 0; }
  </style>`;

function _pdfKpi(label, value, sub) {
  return `<div class="pdf-kpi"><div class="label">${label}</div><div class="value">${value}</div>${sub ? `<div class="sub">${sub}</div>` : ''}</div>`;
}

async function _generatePDF(htmlContent, fileName) {
  const container = document.createElement('div');
  container.innerHTML = htmlContent;
  document.body.appendChild(container);
  try {
    await html2pdf().set({
      margin:       [8, 8, 8, 8],
      filename:     fileName,
      image:        { type: 'jpeg', quality: 0.92 },
      html2canvas:  { scale: 2, useCORS: true, letterRendering: true },
      jsPDF:        { unit: 'mm', format: 'a4', orientation: 'portrait' },
      pagebreak:    { mode: ['css', 'legacy'], avoid: ['.pdf-kpi', 'tr'] },
    }).from(container).save();
  } finally {
    document.body.removeChild(container);
  }
}

/* ══════════════════════════════════════════════════════════════
   PORTFOLIO PDF — exportPortfolioPDF()
══════════════════════════════════════════════════════════════ */
window.exportPortfolioPDF = async function() {
  const btn = $('btn-export-portfolio-pdf');
  if (btn) { btn.disabled = true; btn.textContent = 'Generating PDF…'; }

  try {
    const fqs = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const schemeIds = _ctx.schemeIds;

    const [cosRes, invCtx, burnRes, exitsRes, kpisRes, saasRes, qqRes, invDetailRes, valRes, kpiTrackRes, exitScenRes, holdingRes] = await Promise.allSettled([
      Auth.apiGet('/portfolio-companies/' + fqs),
      getInvestmentsForContext(schemeIds),
      Auth.apiGet('/portfolio/burn-runway/' + fqs),
      Auth.apiGet('/portfolio/exits/' + fqs),
      Auth.apiGet('/portfolio/kpis/' + fqs),
      Auth.apiGet('/portfolio/saas-metrics/' + fqs),
      Auth.apiGet('/portfolio/quoted-unquoted/' + fqs),
      Auth.apiGet('/portfolio/investments/' + fqs),
      Auth.apiGet('/portfolio/valuations/' + fqs),
      Auth.apiGet('/portfolio/kpi-tracking/' + fqs),
      Auth.apiGet('/portfolio/exit-scenarios/' + fqs),
      Auth.apiGet('/portfolio/avg-holding/' + fqs),
    ]);

    const cos      = cosRes.value?.results || cosRes.value || [];
    const invs     = Array.isArray(invCtx.value) ? invCtx.value : [];
    const burnData = burnRes.value || {};
    const exitsData= exitsRes.value || {};
    const kpisData = kpisRes.value || {};
    const saasData = saasRes.value || {};
    const qqData   = qqRes.value || {};
    const invDetail= (invDetailRes.value || {}).investments || [];
    const vals     = (valRes.value || {}).valuations || [];
    const kpiTrack = (kpiTrackRes.value || {}).kpis || [];
    const exitScens= (exitScenRes.value || {}).scenarios || [];
    const holdData = holdingRes.value || {};

    // Compute summary maps
    const costMap = {}, fvMap2 = {}, stageMap2 = {}, irrMap2 = {};
    invs.forEach(inv => {
      const n = inv.company_name || inv.portfolio_company_name || '';
      costMap[n]  = (costMap[n]||0)  + _numVal(inv.total_invested);
      fvMap2[n]   = (fvMap2[n]||0)   + _numVal(inv.latest_valuation);
      if (inv.stage && !stageMap2[n]) stageMap2[n] = inv.stage;
      if (inv.irr_pct != null && irrMap2[n] == null) irrMap2[n] = parseFloat(inv.irr_pct);
    });
    let totalCost = 0, totalFV = 0;
    invs.forEach(inv => { totalCost += _numVal(inv.total_invested); totalFV += _numVal(inv.latest_valuation); });
    const active = cos.filter(c => c.is_active).length;
    const moicVal = totalCost > 0 ? totalFV / totalCost : 0;
    const avgHold = holdData.avg_holding_years;

    // Sectors
    const sectors = {};
    cos.forEach(c => { const s = c.sector || 'Other'; sectors[s] = (sectors[s]||0)+1; });
    const sectorFVm = {}, sectorCostm = {};
    invs.forEach(inv => { const s = inv.sector || 'Other'; sectorFVm[s] = (sectorFVm[s]||0)+_numVal(inv.latest_valuation); sectorCostm[s] = (sectorCostm[s]||0)+_numVal(inv.total_invested); });

    const now = new Date().toLocaleDateString('en-IN', {day:'2-digit',month:'short',year:'numeric'});

    let html = `<div style="max-width:900px;margin:auto;padding:20px;">${_pdfStyles}`;

    // Header
    html += `<div class="pdf-header"><h1>Portfolio Report</h1><p>${esc(_ctx.fundName)} &middot; Generated ${now}</p></div>`;

    // ── 1. Overview ──
    html += `<div class="pdf-section"><h2>1. Portfolio Overview</h2>
      <div class="pdf-kpi-grid">
        ${_pdfKpi('Total Companies', cos.length, `${active} Active · ${cos.length - active} Inactive`)}
        ${_pdfKpi('Total Cost Deployed', fmtCr(totalCost), `${cos.length} companies`)}
        ${_pdfKpi('Total Fair Value', fmtCr(totalFV), `vs Cost ${fmtCr(totalCost)}`)}
        ${_pdfKpi('Avg Ticket', cos.length ? fmtCr(totalCost / cos.length) : '—', 'per company')}
        ${_pdfKpi('Unrealized Gain (MOIC)', fmtX(moicVal), 'vs cost')}
        ${_pdfKpi('Avg Holding', avgHold != null ? avgHold.toFixed(1) + ' yrs' : '—', 'fully diluted')}
      </div>
      <h3>Sector Breakdown</h3>
      <table><tr><th>Sector</th><th class="r">Companies</th><th class="r">FV (Cr)</th><th class="r">% of FV</th><th class="r">Cost (Cr)</th></tr>
      ${Object.entries(sectors).sort(([,a],[,b])=>b-a).map(([s,c]) => `<tr><td class="b">${esc(s)}</td><td class="r">${c}</td><td class="r">${_numStr(sectorFVm[s]||0)}</td><td class="r">${totalFV > 0 ? ((sectorFVm[s]||0)/totalFV*100).toFixed(1)+'%' : '—'}</td><td class="r">${_numStr(sectorCostm[s]||0)}</td></tr>`).join('')}
      </table>
    </div>`;

    // ── 2. All Companies ──
    html += `<div class="pdf-section" style="page-break-before:always;"><h2>2. All Portfolio Companies (${cos.length})</h2>
      <table><tr><th>#</th><th>Company</th><th>Sector</th><th>Stage</th><th>City</th><th class="r">Cost (Cr)</th><th class="r">FV (Cr)</th><th class="r">IRR%</th><th class="r">MOIC</th><th class="c">Status</th></tr>
      ${cos.map((c, i) => { const cost = costMap[c.name]||0; const fv = fvMap2[c.name]||0; const m = cost > 0 ? (fv/cost).toFixed(2)+'x' : '—';
        return `<tr><td>${i+1}</td><td class="b">${esc(c.name||'—')}</td><td>${esc(c.sector||'—')}</td><td>${esc(stageMap2[c.name]||'—')}</td><td>${esc(c.headquarters_city||'—')}</td><td class="r">${cost > 0 ? _numStr(cost) : '—'}</td><td class="r">${fv > 0 ? _numStr(fv) : '—'}</td><td class="r">${irrMap2[c.name] != null ? irrMap2[c.name].toFixed(1) : '—'}</td><td class="r">${m}</td><td class="c">${c.is_active ? 'Active' : 'Inactive'}</td></tr>`; }).join('')}
      </table></div>`;

    // ── 3. Burn & Runway ──
    const burnCos = burnData.companies || [];
    html += `<div class="pdf-section" style="page-break-before:always;"><h2>3. Burn & Runway (${burnCos.length} companies)</h2>
      <div class="pdf-kpi-grid">
        ${_pdfKpi('Avg Gross Burn', burnData.avg_gross_burn != null ? fmtCr(burnData.avg_gross_burn) : '—', '/Month')}
        ${_pdfKpi('Avg Net Burn', burnData.avg_net_burn != null ? fmtCr(burnData.avg_net_burn) : '—', '/Month')}
        ${_pdfKpi('Avg Runway', burnData.avg_runway != null ? burnData.avg_runway.toFixed(1) + ' mo' : '—', 'Portfolio avg')}
        ${_pdfKpi('Total Cash', burnData.total_cash != null ? fmtCr(burnData.total_cash) : '—', 'All companies')}
      </div>`;
    if (burnCos.length) {
      html += `<table><tr><th>#</th><th>Company</th><th class="r">Gross Burn (Cr)</th><th class="r">Net Burn (Cr)</th><th class="r">Cash (Cr)</th><th class="r">Runway (mo)</th><th class="c">Risk</th></tr>
      ${burnCos.map((c, i) => { const rmo = c.runway_months; const risk = rmo == null ? '—' : rmo < 6 ? 'High' : rmo < 12 ? 'Watch' : 'Safe';
        return `<tr><td>${i+1}</td><td class="b">${esc(c.company_name)}</td><td class="r">${c.gross_burn != null ? _numStr(c.gross_burn) : '—'}</td><td class="r">${c.net_burn != null ? _numStr(c.net_burn) : '—'}</td><td class="r">${c.cash_balance != null ? _numStr(c.cash_balance) : '—'}</td><td class="r">${rmo != null ? rmo.toFixed(1) : '—'}</td><td class="c">${risk}</td></tr>`; }).join('')}
      </table>`;
    } else { html += '<p class="pdf-empty">No burn/runway data.</p>'; }
    html += '</div>';

    // ── 4. Exits ──
    const exitsList = exitsData.exits || [];
    const exitSum = exitsData.summary || {};
    html += `<div class="pdf-section" style="page-break-before:always;"><h2>4. Exits (${exitsList.length} exits)</h2>
      <div class="pdf-kpi-grid">
        ${_pdfKpi('Total Proceeds', exitSum.total_proceeds ? fmtCr(exitSum.total_proceeds) : '—', 'exits')}
        ${_pdfKpi('Avg Exit MOIC', exitSum.avg_moic != null ? fmtX(exitSum.avg_moic) : '—', 'Gross realized')}
        ${_pdfKpi('Avg IRR', (exitSum.avg_net_irr ?? exitSum.avg_irr) != null ? (exitSum.avg_net_irr ?? exitSum.avg_irr).toFixed(1)+'%' : '—', 'On exits')}
        ${_pdfKpi('DPI', exitSum.dpi != null ? exitSum.dpi.toFixed(2)+'x' : '—', 'Distributed / Paid-in')}
        ${_pdfKpi('Total Exits', exitSum.total_exits ?? exitsList.length, 'Completed')}
      </div>`;
    if (exitsList.length) {
      html += `<table><tr><th>#</th><th>Company</th><th>Sector</th><th>Exit Type</th><th class="r">Cost (Cr)</th><th class="r">Proceeds (Cr)</th><th class="r">MOIC</th><th class="r">IRR%</th><th>Exit Date</th></tr>
      ${exitsList.map((e, i) => { const irr = e.net_irr_pct ?? e.irr_pct;
        return `<tr><td>${i+1}</td><td class="b">${esc(e.company_name)}</td><td>${esc(e.sector||'—')}</td><td>${esc(e.exit_type_display||e.exit_type)}</td><td class="r">${e.cost ? _numStr(e.cost) : '—'}</td><td class="r">${e.proceeds != null ? _numStr(e.proceeds) : '—'}</td><td class="r">${e.moic != null ? e.moic.toFixed(2)+'x' : '—'}</td><td class="r">${irr != null ? irr.toFixed(1) : '—'}</td><td>${e.exit_date||'—'}</td></tr>`; }).join('')}
      </table>`;
    } else { html += '<p class="pdf-empty">No exits recorded.</p>'; }
    html += '</div>';

    // ── 5. KPIs ──
    const kpis = kpisData.kpis || [];
    html += `<div class="pdf-section" style="page-break-before:always;"><h2>5. Portfolio KPIs (${kpis.length} records)</h2>`;
    if (kpis.length) {
      html += `<table><tr><th>#</th><th>Company</th><th>Metric</th><th class="r">Value</th><th>Period</th><th>Format</th></tr>
      ${kpis.map((k, i) => {
        let v = '—'; if (k.value != null) { if (k.format === 'currency') v = fmtCr(k.value); else if (k.format === 'percent') v = k.value.toFixed(2)+'%'; else if (k.format === 'ratio') v = k.value.toFixed(2)+'x'; else v = Number(k.value).toLocaleString('en-IN'); }
        return `<tr><td>${i+1}</td><td class="b">${esc(k.company_name||'—')}</td><td>${esc(k.kpi_name||'—')}</td><td class="r">${v}</td><td>${k.period||'—'}</td><td>${esc(k.format||'—')}</td></tr>`; }).join('')}
      </table>`;
    } else { html += '<p class="pdf-empty">No KPI data.</p>'; }
    html += '</div>';

    // ── 6. SaaS Metrics ──
    const saas = saasData.companies || [];
    html += `<div class="pdf-section" style="page-break-before:always;"><h2>6. SaaS Metrics (${saas.length} companies)</h2>`;
    if (saas.length) {
      let totalMRR = 0, totalARR = 0; saas.forEach(c => { if (c.mrr) totalMRR += c.mrr; if (c.arr) totalARR += c.arr; });
      html += `<div class="pdf-kpi-grid">
        ${_pdfKpi('Portfolio MRR', totalMRR ? fmtCr(totalMRR) : '—', 'Monthly Recurring Rev')}
        ${_pdfKpi('Portfolio ARR', totalARR ? fmtCr(totalARR) : '—', 'Annual Run Rate')}
      </div>
      <table><tr><th>#</th><th>Company</th><th>Sector</th><th class="r">MRR (Cr)</th><th class="r">ARR (Cr)</th><th class="r">NRR%</th><th class="r">Churn%</th><th class="r">CAC</th><th class="r">LTV</th><th class="r">LTV/CAC</th></tr>
      ${saas.map((c, i) => `<tr><td>${i+1}</td><td class="b">${esc(c.company_name||'—')}</td><td>${esc(c.sector||'—')}</td><td class="r">${c.mrr != null ? _numStr(c.mrr) : '—'}</td><td class="r">${c.arr != null ? _numStr(c.arr) : '—'}</td><td class="r">${c.nrr != null ? c.nrr.toFixed(1)+'%' : '—'}</td><td class="r">${c.churn_rate != null ? c.churn_rate.toFixed(2)+'%' : '—'}</td><td class="r">${c.cac != null ? '₹'+Number(c.cac).toLocaleString('en-IN',{maximumFractionDigits:0}) : '—'}</td><td class="r">${c.ltv != null ? '₹'+Number(c.ltv).toLocaleString('en-IN',{maximumFractionDigits:0}) : '—'}</td><td class="r">${c.ltv_cac_ratio != null ? c.ltv_cac_ratio.toFixed(1)+'x' : '—'}</td></tr>`).join('')}
      </table>`;
    } else { html += '<p class="pdf-empty">No SaaS metrics data.</p>'; }
    html += '</div>';

    // ── 7. Quoted & Unquoted ──
    const quoted = qqData.quoted || [];
    const unquoted = qqData.unquoted || [];
    const qqSummary = qqData.summary || {};
    html += `<div class="pdf-section" style="page-break-before:always;"><h2>7. Quoted & Unquoted Companies</h2>
      <div class="pdf-kpi-grid">
        ${_pdfKpi('Quoted (Listed)', qqSummary.quoted_count ?? quoted.length, qqSummary.quoted_cost ? 'Cost: '+fmtCr(qqSummary.quoted_cost) : '')}
        ${_pdfKpi('Unquoted (Private)', qqSummary.unquoted_count ?? unquoted.length, qqSummary.unquoted_cost ? 'Cost: '+fmtCr(qqSummary.unquoted_cost) : '')}
      </div>`;
    if (quoted.length) {
      html += `<h3>Quoted (Listed) — ${quoted.length} companies</h3>
      <table><tr><th>#</th><th>Company</th><th>Sector</th><th>Exchange</th><th class="r">Cost (Cr)</th><th class="r">FV (Cr)</th><th>IPEV</th></tr>
      ${quoted.map((c, i) => `<tr><td>${i+1}</td><td class="b">${esc(c.name)}</td><td>${esc(c.sector||'—')}</td><td>${esc(c.exchange||'—')}</td><td class="r">${_numStr(c.cost)}</td><td class="r">${_numStr(c.fair_value)}</td><td>${c.ipev_level||'—'}</td></tr>`).join('')}
      </table>`;
    }
    if (unquoted.length) {
      html += `<h3>Unquoted (Private) — ${unquoted.length} companies</h3>
      <table><tr><th>#</th><th>Company</th><th>Sector</th><th class="r">Cost (Cr)</th><th class="r">FV (Cr)</th><th>IPEV</th></tr>
      ${unquoted.map((c, i) => `<tr><td>${i+1}</td><td class="b">${esc(c.name)}</td><td>${esc(c.sector||'—')}</td><td class="r">${_numStr(c.cost)}</td><td class="r">${_numStr(c.fair_value)}</td><td>${c.ipev_level||'—'}</td></tr>`).join('')}
      </table>`;
    }
    if (!quoted.length && !unquoted.length) html += '<p class="pdf-empty">No quoted/unquoted data.</p>';
    html += '</div>';

    // ── 8. Investments Detail ──
    html += `<div class="pdf-section" style="page-break-before:always;"><h2>8. Investments Detail (${invDetail.length} investments)</h2>`;
    if (invDetail.length) {
      html += `<table><tr><th>#</th><th>Company</th><th>Scheme</th><th>Sector</th><th>Stage</th><th>Instrument</th><th class="r">Invested (Cr)</th><th class="r">Own%</th><th class="r">IRR%</th><th class="r">FV (Cr)</th><th class="c">Status</th><th>Date</th></tr>
      ${invDetail.map((inv, i) => `<tr><td>${i+1}</td><td class="b">${esc(inv.company_name)}</td><td style="font-size:10px;">${esc(inv.scheme_name)}</td><td>${esc(inv.sector||'—')}</td><td>${esc(inv.stage||'—')}</td><td style="font-size:10px;">${esc(inv.instrument_type_display||inv.instrument_type||'—')}</td><td class="r">${inv.total_invested ? _numStr(inv.total_invested) : '—'}</td><td class="r">${inv.ownership_pct != null ? inv.ownership_pct.toFixed(2) : '—'}</td><td class="r">${inv.irr_pct != null ? inv.irr_pct.toFixed(1) : '—'}</td><td class="r">${inv.latest_valuation != null ? _numStr(inv.latest_valuation) : '—'}</td><td class="c">${esc(inv.status_display||inv.status||'—')}</td><td style="font-size:10px;">${inv.investment_date ? inv.investment_date.slice(0,10) : '—'}</td></tr>`).join('')}
      </table>`;
    } else { html += '<p class="pdf-empty">No investment data.</p>'; }
    html += '</div>';

    // ── 9. Valuations ──
    html += `<div class="pdf-section" style="page-break-before:always;"><h2>9. Valuations (${vals.length} records)</h2>`;
    if (vals.length) {
      const ipevLabel = { 1: 'L1 Quoted', 2: 'L2 Observable', 3: 'L3 Unobservable' };
      html += `<table><tr><th>#</th><th>Company</th><th>Scheme</th><th>Date</th><th class="r">Fair Value (Cr)</th><th>Methodology</th><th class="c">IPEV</th><th class="r">MOIC</th><th class="c">Status</th><th>Submitted</th><th>Approved</th></tr>
      ${vals.map((v, i) => `<tr><td>${i+1}</td><td class="b">${esc(v.company_name)}</td><td style="font-size:10px;">${esc(v.scheme_name)}</td><td style="font-size:10px;">${v.valuation_date ? v.valuation_date.slice(0,10) : '—'}</td><td class="r">${_numStr(v.fair_value)}</td><td style="font-size:10px;">${esc(v.methodology_display||v.methodology||'—')}</td><td class="c" style="font-size:10px;">${v.ipev_level ? esc(ipevLabel[v.ipev_level]||'L'+v.ipev_level) : '—'}</td><td class="r">${v.multiple != null ? v.multiple.toFixed(2)+'x' : '—'}</td><td class="c">${esc(v.status||'—')}</td><td style="font-size:10px;">${esc(v.submitted_by||'—')}</td><td style="font-size:10px;">${esc(v.approved_by||'—')}</td></tr>`).join('')}
      </table>`;
    } else { html += '<p class="pdf-empty">No valuation data.</p>'; }
    html += '</div>';

    // ── 10. KPI Tracking ──
    html += `<div class="pdf-section" style="page-break-before:always;"><h2>10. KPI Tracking (${kpiTrack.length} submissions)</h2>`;
    if (kpiTrack.length) {
      html += `<table><tr><th>#</th><th>Company</th><th>KPI Metric</th><th>Period</th><th class="r">Value</th><th>Format</th><th class="c">Status</th><th>Submitted By</th><th>Date</th><th>Reviewed By</th></tr>
      ${kpiTrack.map((k, i) => {
        let v = '—'; if (k.value != null) { if (k.format === 'currency') v = fmtCr(k.value); else if (k.format === 'percent') v = k.value.toFixed(2)+'%'; else if (k.format === 'ratio') v = k.value.toFixed(2)+'x'; else v = String(k.value); }
        return `<tr><td>${i+1}</td><td class="b">${esc(k.company_name)}</td><td>${esc(k.kpi_name)}</td><td style="font-size:10px;">${k.period ? k.period.slice(0,7) : '—'}</td><td class="r">${v}</td><td style="font-size:10px;">${esc(k.format||'—')}</td><td class="c">${esc(k.status||'—')}</td><td style="font-size:10px;">${esc(k.submitted_by||'—')}</td><td style="font-size:10px;">${k.submitted_at ? k.submitted_at.slice(0,10) : '—'}</td><td style="font-size:10px;">${esc(k.reviewed_by||'—')}</td></tr>`; }).join('')}
      </table>`;
    } else { html += '<p class="pdf-empty">No KPI tracking data.</p>'; }
    html += '</div>';

    // ── 11. Exit Scenarios ──
    html += `<div class="pdf-section" style="page-break-before:always;"><h2>11. Exit Scenarios (${exitScens.length} records)</h2>`;
    if (exitScens.length) {
      const actual = exitScens.filter(r => r.is_actual).length;
      html += `<div class="pdf-kpi-grid">
        ${_pdfKpi('Total Scenarios', exitScens.length, 'Across portfolio')}
        ${_pdfKpi('Actual Exits', actual, 'Completed exits')}
        ${_pdfKpi('Modelled', exitScens.length - actual, 'IPO / M&A / Secondary')}
      </div>
      <table><tr><th>#</th><th>Company</th><th>Scheme</th><th>Exit Type</th><th class="c">Actual?</th><th>Exit Date</th><th class="r">Proceeds (Cr)</th><th class="r">MOIC</th><th class="r">IRR%</th><th>Nature</th><th>Buyer</th></tr>
      ${exitScens.map((e, i) => `<tr><td>${i+1}</td><td class="b">${esc(e.company_name)}</td><td style="font-size:10px;">${esc(e.scheme_name)}</td><td>${esc(e.exit_type_display||e.exit_type)}</td><td class="c">${e.is_actual ? 'Yes' : 'No'}</td><td style="font-size:10px;">${e.exit_date ? e.exit_date.slice(0,10) : '—'}</td><td class="r">${e.proceeds != null ? _numStr(e.proceeds) : '—'}</td><td class="r">${e.moic != null ? e.moic.toFixed(2)+'x' : '—'}</td><td class="r">${e.irr_pct != null ? e.irr_pct.toFixed(1) : '—'}</td><td style="font-size:10px;">${esc(e.gain_loss_nature||'—')}</td><td style="font-size:10px;">${esc(e.buyer_name||'—')}</td></tr>`).join('')}
      </table>`;
    } else { html += '<p class="pdf-empty">No exit scenarios.</p>'; }
    html += '</div>';

    // Footer
    html += `<div class="pdf-footer">TrackFundAI &middot; Confidential &middot; ${esc(_ctx.fundName)} &middot; Portfolio Report &middot; ${now}</div></div>`;

    const fileName = `TrackFundAI_Portfolio_${(_ctx.fundName||'AllFunds').replace(/[^a-zA-Z0-9]/g,'_')}_${new Date().toISOString().slice(0,10)}.pdf`;
    await _generatePDF(html, fileName);

  } catch(e) {
    console.error('Portfolio PDF error:', e);
    alert('Error generating Portfolio PDF. Please try again.');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '&#128196; Export PDF'; }
  }
};

/* ══════════════════════════════════════════════════════════════
   FUND ACCOUNTING PDF — exportAccountingPDF()
══════════════════════════════════════════════════════════════ */
window.exportAccountingPDF = async function() {
  const btn = $('btn-export-accounting-pdf');
  if (btn) { btn.disabled = true; btn.textContent = 'Generating PDF…'; }

  try {
    const fqs = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const qs  = schemeQS(_ctx.schemeIds);

    const [navRes, carryRes, callsRes, distsRes, ledgerRes, feesRes, coaRes] = await Promise.allSettled([
      Auth.apiGet('/accounting/nav/' + (fqs || (qs ? '?' + qs : ''))),
      Auth.apiGet('/accounting/carry/' + fqs),
      Auth.apiGet('/lp/capital-calls/' + (qs ? '?' + qs : fqs)),
      Auth.apiGet('/lp/distributions/' + (qs ? '?' + qs : fqs)),
      Auth.apiGet('/accounting/ledger/' + fqs),
      Auth.apiGet('/accounting/fees/' + fqs),
      Auth.apiGet('/accounting/chart-of-accounts/'),
    ]);

    const navArr  = (navRes.value?.results  || navRes.value  || []).sort((a,b) => (b.nav_date||'') > (a.nav_date||'') ? 1 : -1);
    const carries = carryRes.value?.results  || carryRes.value  || [];
    const calls   = callsRes.value?.results  || callsRes.value  || [];
    const dists   = distsRes.value?.results   || distsRes.value   || [];
    const ledger  = ledgerRes.value?.results  || ledgerRes.value  || [];
    const fees    = feesRes.value?.results    || feesRes.value    || [];
    const coa     = coaRes.value?.results     || coaRes.value     || [];

    // NAV aggregates
    const latestByScheme = {};
    navArr.forEach(n => { if (!latestByScheme[n.scheme]) latestByScheme[n.scheme] = n; });
    const latestArr = Object.values(latestByScheme);
    const totalNav = latestArr.reduce((s,n) => s + _numVal(n.total_nav), 0);
    const avgNavPerUnit = latestArr.length ? latestArr.reduce((s,n) => s + _numVal(n.nav_per_unit), 0) / latestArr.length : 0;
    const totalUnrealized = latestArr.reduce((s,n) => s + _numVal(n.unrealized_gains), 0);
    const totalRealized   = latestArr.reduce((s,n) => s + _numVal(n.realized_gains), 0);
    const totalMgmtFee    = navArr.reduce((s,n) => s + _numVal(n.management_fee_payable), 0);

    // Carry aggregates
    let totalCalled = 0, totalDistC = 0, prefReturn = 0, carryGross = 0, carryNet = 0, clawback = 0;
    carries.forEach(c => {
      totalCalled += _numVal(c.total_called_capital);
      totalDistC  += _numVal(c.total_distributions);
      prefReturn  += _numVal(c.preferred_return_amount);
      carryGross  += _numVal(c.carry_amount_gross);
      carryNet    += _numVal(c.carry_amount_net);
      clawback    += _numVal(c.gp_clawback_provision);
    });

    const now = new Date().toLocaleDateString('en-IN', {day:'2-digit',month:'short',year:'numeric'});

    let html = `<div style="max-width:900px;margin:auto;padding:20px;">${_pdfStyles}`;

    // Header
    html += `<div class="pdf-header"><h1>Fund Accounting Report</h1><p>${esc(_ctx.fundName)} &middot; Generated ${now}</p></div>`;

    // ── 1. NAV Summary ──
    html += `<div class="pdf-section"><h2>1. NAV Summary</h2>
      <div class="pdf-kpi-grid">
        ${_pdfKpi('Total NAV', fmtCr(totalNav), 'As of latest period')}
        ${_pdfKpi('NAV / Unit', avgNavPerUnit > 0 ? '₹'+avgNavPerUnit.toFixed(4) : '—', latestArr.length+' schemes')}
        ${_pdfKpi('Mgmt Fee YTD', totalMgmtFee > 0 ? fmtCr(totalMgmtFee) : '—', '2.0% p.a. on corpus')}
        ${_pdfKpi('Unrealized Gains', totalUnrealized > 0 ? fmtCr(totalUnrealized) : '—', 'Portfolio revaluation')}
        ${_pdfKpi('Realized Gains', totalRealized > 0 ? fmtCr(totalRealized) : '—', 'From exits')}
      </div></div>`;

    // ── 2. NAV History ──
    html += `<div class="pdf-section"><h2>2. NAV History (${navArr.length} records)</h2>`;
    if (navArr.length) {
      html += `<table><tr><th>Period</th><th>Scheme</th><th class="r">Total NAV</th><th class="r">NAV/Unit</th><th class="r">Units</th><th class="r">Investments FV</th><th class="r">Cash</th><th class="r">Receivables</th><th class="r">Mgmt Fee</th><th class="r">Unrealized</th><th class="r">Realized</th><th class="c">Recon</th></tr>
      ${navArr.map(n => `<tr><td style="font-size:10px;">${n.nav_date||'—'}</td><td style="font-size:10px;">${esc(n.scheme_name||'—')}</td><td class="r">${_numStr(n.total_nav)}</td><td class="r">${n.nav_per_unit ? parseFloat(n.nav_per_unit).toFixed(4) : '—'}</td><td class="r">${n.total_units_outstanding ? _numStr(n.total_units_outstanding,2) : '—'}</td><td class="r">${_numStr(n.investments_at_fair_value)}</td><td class="r">${_numStr(n.cash_and_equivalents)}</td><td class="r">${_numStr(n.receivables)}</td><td class="r">${_numStr(n.management_fee_payable)}</td><td class="r">${_numStr(n.unrealized_gains)}</td><td class="r">${_numStr(n.realized_gains)}</td><td class="c" style="font-size:10px;">${n.depository_reconciled ? '✓' : '✗'}</td></tr>`).join('')}
      </table>`;
    } else { html += '<p class="pdf-empty">No NAV records.</p>'; }
    html += '</div>';

    // ── 3. Waterfall & Carry ──
    html += `<div class="pdf-section" style="page-break-before:always;"><h2>3. Carry & Clawback Analysis</h2>`;
    if (carries.length) {
      html += `<div class="pdf-kpi-grid">
        ${_pdfKpi('Carry Base', fmtCr(carries.reduce((s,c)=>s+_numVal(c.carry_base),0)), 'Profit above hurdle')}
        ${_pdfKpi('GP Carry (Gross)', fmtCr(carryGross), 'Before clawback')}
        ${_pdfKpi('GP Carry (Net)', fmtCr(carryNet), 'After clawback')}
        ${_pdfKpi('Clawback Provision', fmtCr(clawback), 'Excess carry to LPs')}
      </div>
      <table><tr><th>Scheme</th><th>Date</th><th class="c">Status</th><th class="r">Distributions</th><th class="r">Called Capital</th><th class="r">Pref Return</th><th class="r">Carry Base</th><th class="r">Gross Carry</th><th class="r">Net Carry</th><th class="r">Clawback</th></tr>
      ${carries.map(c => `<tr><td class="b">${esc(c.scheme_name||'—')}</td><td style="font-size:10px;">${c.calculation_date||'—'}</td><td class="c">${esc(c.status_display||c.calculation_status||'—')}</td><td class="r">${_numStr(c.total_distributions)}</td><td class="r">${_numStr(c.total_called_capital)}</td><td class="r">${_numStr(c.preferred_return_amount)}</td><td class="r">${_numStr(c.carry_base)}</td><td class="r">${_numStr(c.carry_amount_gross)}</td><td class="r">${_numStr(c.carry_amount_net)}</td><td class="r">${_numStr(c.gp_clawback_provision)}</td></tr>`).join('')}
      </table>`;
    } else { html += '<p class="pdf-empty">No carry data. Run waterfall engine to compute.</p>'; }
    html += '</div>';

    // ── 4. Capital Calls ──
    html += `<div class="pdf-section" style="page-break-before:always;"><h2>4. Capital Calls (${calls.length} records)</h2>`;
    if (calls.length) {
      const totalCallAmt = calls.reduce((s,c) => s + _numVal(c.total_call_amount), 0);
      html += `<div class="pdf-kpi-grid">${_pdfKpi('Total Called', fmtCr(totalCallAmt), calls.length+' calls')}</div>
      <table><tr><th>#</th><th>LP / Scheme</th><th>Call Date</th><th class="r">Amount (Cr)</th><th class="c">Status</th><th>Purpose</th></tr>
      ${calls.map((c, i) => { const parts = (c.purpose||c.scheme_name||'').split(' — ');
        return `<tr><td>${i+1}</td><td class="b">${esc(parts[0])}</td><td style="font-size:10px;">${c.call_date||'—'}</td><td class="r">${_numStr(c.total_call_amount)}</td><td class="c">${esc(c.status_display||c.call_status||'—')}</td><td style="font-size:10px;">${esc(parts.length > 1 ? parts.slice(1).join(' — ') : '—')}</td></tr>`; }).join('')}
      </table>`;
    } else { html += '<p class="pdf-empty">No capital calls.</p>'; }
    html += '</div>';

    // ── 5. Distributions ──
    html += `<div class="pdf-section"><h2>5. Distributions (${dists.length} records)</h2>`;
    if (dists.length) {
      const totalDistAmt = dists.reduce((s,d) => s + _numVal(d.total_net_amount), 0);
      html += `<div class="pdf-kpi-grid">${_pdfKpi('Total Distributed', fmtCr(totalDistAmt), dists.length+' distributions')}</div>
      <table><tr><th>#</th><th>Scheme / LP</th><th>Date</th><th class="r">Amount (Cr)</th><th>Type</th><th class="c">Status</th></tr>
      ${dists.map((d, i) => `<tr><td>${i+1}</td><td class="b">${esc(d.scheme_name||'—')}</td><td style="font-size:10px;">${d.distribution_date||'—'}</td><td class="r">${_numStr(d.total_net_amount)}</td><td>${esc(d.type_display||d.distribution_type||'—')}</td><td class="c">${esc(d.status_display||d.distribution_status||'—')}</td></tr>`).join('')}
      </table>`;
    } else { html += '<p class="pdf-empty">No distributions.</p>'; }
    html += '</div>';

    // ── 6. Fund P&L ──
    const plArr = [...navArr].sort((a,b) => (a.nav_date||'') < (b.nav_date||'') ? -1 : 1);
    html += `<div class="pdf-section" style="page-break-before:always;"><h2>6. Fund P&L (${plArr.length} periods)</h2>`;
    if (plArr.length) {
      html += `<table><tr><th>Period</th><th>Scheme</th><th class="r">Unrealized Gains</th><th class="r">Realized Gains</th><th class="r">Mgmt Fee</th><th class="r">Other Liabilities</th><th class="r">Gross Income</th><th class="r">Total Expenses</th><th class="r">Net P&L</th></tr>
      ${plArr.map(n => {
        const ur = _numVal(n.unrealized_gains); const rl = _numVal(n.realized_gains);
        const mf = _numVal(n.management_fee_payable); const ol = _numVal(n.other_liabilities);
        const gross = ur + rl; const exp = mf + ol; const net = gross - exp;
        return `<tr><td style="font-size:10px;">${n.nav_date||'—'}</td><td style="font-size:10px;">${esc(n.scheme_name||'—')}</td><td class="r">${ur ? _numStr(ur) : '—'}</td><td class="r">${rl ? _numStr(rl) : '—'}</td><td class="r">${mf ? _numStr(mf) : '—'}</td><td class="r">${ol ? _numStr(ol) : '—'}</td><td class="r">${_numStr(gross)}</td><td class="r">${_numStr(exp)}</td><td class="r b" style="color:${net >= 0 ? '#16a34a' : '#dc2626'}">${_numStr(net)}</td></tr>`; }).join('')}
      </table>`;
    } else { html += '<p class="pdf-empty">No P&L data.</p>'; }
    html += '</div>';

    // ── 7. Fund Ledger ──
    html += `<div class="pdf-section" style="page-break-before:always;"><h2>7. Fund Ledger (${ledger.length} entries)</h2>`;
    if (ledger.length) {
      html += `<table><tr><th>Entry #</th><th>Date</th><th>Description</th><th>Debit Account</th><th>Credit Account</th><th class="r">Amount (₹)</th><th>Ref Type</th><th class="c">Status</th></tr>
      ${ledger.map(e => `<tr ${e.is_reversed ? 'style="color:#999;text-decoration:line-through;"' : ''}>
        <td style="font-size:10px;">${esc(e.journal_entry_number||'—')}</td><td style="font-size:10px;">${e.entry_date||'—'}</td><td style="font-size:10px;max-width:150px;">${esc(e.description||'—')}</td><td style="font-size:10px;">${esc(e.debit_account_name||'—')}</td><td style="font-size:10px;">${esc(e.credit_account_name||'—')}</td><td class="r">${_numStr(e.amount)}</td><td style="font-size:10px;">${esc(e.reference_type_display||e.reference_type||'—')}</td><td class="c">${e.is_reversed ? 'REVERSED' : 'POSTED'}</td></tr>`).join('')}
      </table>`;
    } else { html += '<p class="pdf-empty">No journal entries.</p>'; }
    html += '</div>';

    // ── 8. Management Fees ──
    html += `<div class="pdf-section" style="page-break-before:always;"><h2>8. Management Fee Schedule (${fees.length} periods)</h2>`;
    if (fees.length) {
      html += `<table><tr><th>Scheme</th><th>Period</th><th class="r">Fee Basis</th><th class="r">Rate</th><th class="r">Fee Amount</th><th class="r">GST</th><th class="r">Total (incl GST)</th><th>Invoice #</th><th class="c">Status</th></tr>
      ${fees.map(f => `<tr><td class="b" style="font-size:11px;">${esc(f.scheme_name||'—')}</td><td style="font-size:10px;">${f.period_start||'—'} → ${f.period_end||'—'}</td><td class="r">${_numStr(f.fee_basis_amount)}</td><td class="r">${f.fee_rate ? parseFloat(f.fee_rate).toFixed(4)+'%' : '—'}</td><td class="r b">${_numStr(f.fee_amount)}</td><td class="r">${_numStr(f.gst_amount)}</td><td class="r b">${_numStr(f.total_fee_with_gst)}</td><td style="font-size:10px;">${esc(f.invoice_number||'—')}</td><td class="c">${esc(f.status_display||f.fee_status||'—')}</td></tr>`).join('')}
      </table>`;
    } else { html += '<p class="pdf-empty">No fee periods.</p>'; }
    html += '</div>';

    // ── 9. Chart of Accounts ──
    html += `<div class="pdf-section" style="page-break-before:always;"><h2>9. Chart of Accounts (${coa.length} accounts)</h2>`;
    if (coa.length) {
      html += `<table><tr><th>Code</th><th>Account Name</th><th>Type</th><th>Parent Account</th><th>Description</th><th class="c">Active</th></tr>
      ${coa.map(a => `<tr><td style="font-family:monospace;font-size:10px;">${esc(a.account_code)}</td><td class="b">${esc(a.account_name)}</td><td>${esc(a.account_type_display||a.account_type)}</td><td style="font-size:10px;">${esc(a.parent_account_name||'—')}</td><td style="font-size:10px;max-width:180px;">${esc(a.description||'—')}</td><td class="c">${a.is_active ? '✓' : '✗'}</td></tr>`).join('')}
      </table>`;
    } else { html += '<p class="pdf-empty">No accounts.</p>'; }
    html += '</div>';

    // Footer
    html += `<div class="pdf-footer">TrackFundAI &middot; Confidential &middot; ${esc(_ctx.fundName)} &middot; Fund Accounting Report &middot; ${now}</div></div>`;

    const fileName = `TrackFundAI_Accounting_${(_ctx.fundName||'AllFunds').replace(/[^a-zA-Z0-9]/g,'_')}_${new Date().toISOString().slice(0,10)}.pdf`;
    await _generatePDF(html, fileName);

  } catch(e) {
    console.error('Accounting PDF error:', e);
    alert('Error generating Accounting PDF. Please try again.');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '&#128196; Export PDF'; }
  }
};

/* ══════════════════════════════════════════════════════════════
   VALUATIONS PDF — exportValuationReportPDF()
══════════════════════════════════════════════════════════════ */
window.exportValuationReportPDF = async function() {
  const btn = document.getElementById('btn-val-report');
  if (btn) { btn.disabled = true; btn.innerHTML = '&#9203; Generating…'; }

  try {
    const schemeIds = _ctx.schemeIds;
    const fqs = _ctx.fundId ? `?fund=${_ctx.fundId}` : '';
    const now = new Date().toLocaleDateString('en-IN', { day:'2-digit', month:'short', year:'numeric' });

    // Fetch data in parallel: investments (for summary + bridge) and valuations API (for methodology/IPEV)
    const [invs, valData] = await Promise.allSettled([
      getInvestmentsForContext(schemeIds),
      Auth.apiGet('/portfolio/valuations/' + fqs),
    ]);

    const investments = invs.status === 'fulfilled' ? (invs.value || []) : [];
    const valuations  = valData.status === 'fulfilled' ? (valData.value.valuations || []) : [];

    let html = `<div style="max-width:780px;margin:0 auto;padding:18px;font-family:'Segoe UI',Helvetica,Arial,sans-serif;color:#1a1a2e">${_pdfStyles}`;

    // ── Header ──
    html += `<div class="pdf-header">
      <h1>Valuation Report</h1>
      <p>${esc(_ctx.fundName || 'All Funds')} &middot; Generated ${now}</p>
      <p style="font-size:11px;color:#888">IPEV Guidelines &middot; IBBI Registered Valuers</p>
    </div>`;

    // ═══════════ SECTION 1: SUMMARY ═══════════
    html += `<div class="pdf-section"><h2>1. Valuation Summary &mdash; All Portfolio Companies</h2>`;

    // Summary KPIs
    let totalCost = 0, totalFV = 0, companyCount = 0;
    const sorted = [...investments].sort((a, b) =>
      parseFloat(b.latest_valuation || 0) - parseFloat(a.latest_valuation || 0)
    );
    for (const inv of sorted) {
      const cost = parseFloat(inv.total_invested || 0);
      const fv   = parseFloat(inv.latest_valuation || 0);
      if (cost > 0 || fv > 0) companyCount++;
      totalCost += cost;
      totalFV   += fv;
    }
    const totalGain = totalFV - totalCost;
    const totalMOIC = totalCost > 0 ? (totalFV / totalCost).toFixed(2) + 'x' : '—';

    html += `<div class="pdf-kpi-grid">
      ${_pdfKpi('Total Companies', companyCount)}
      ${_pdfKpi('Total Cost', fmtCr(totalCost))}
      ${_pdfKpi('Total Fair Value', fmtCr(totalFV))}
      ${_pdfKpi('Unrealized Gain', (totalGain >= 0 ? '+' : '') + fmtCr(totalGain))}
      ${_pdfKpi('Portfolio MOIC', totalMOIC)}
    </div>`;

    // Summary table
    html += `<table><thead><tr>
      <th>#</th><th>Company</th><th>Method</th><th class="c">Level</th>
      <th class="r">Cost (Cr)</th><th class="r">FV (Cr)</th><th class="r">MOIC</th><th>Valuation Date</th>
    </tr></thead><tbody>`;

    if (sorted.length) {
      sorted.forEach((inv, i) => {
        const cost = parseFloat(inv.total_invested || 0);
        const fv   = parseFloat(inv.latest_valuation || 0);
        const moic = cost > 0 ? (fv / cost).toFixed(2) + 'x' : '—';
        const method = inv.instrument_type_display || inv.instrument_type || '—';
        // Try to find matching valuation record for IPEV level
        const compName = (inv.company_name || inv.portfolio_company_name || '').toLowerCase();
        const matchVal = valuations.find(v => (v.company_name || '').toLowerCase() === compName);
        const level = matchVal && matchVal.ipev_level ? 'L' + matchVal.ipev_level : '—';
        html += `<tr>
          <td>${i + 1}</td>
          <td class="b">${esc(inv.company_name || inv.portfolio_company_name || '—')}</td>
          <td>${esc(method)}</td>
          <td class="c">${level}</td>
          <td class="r">${cost > 0 ? fmtCr(cost) : '—'}</td>
          <td class="r">${fv > 0 ? fmtCr(fv) : '—'}</td>
          <td class="r">${moic}</td>
          <td>${inv.investment_date || '—'}</td>
        </tr>`;
      });
    } else {
      html += `<tr><td colspan="8" style="text-align:center;color:#999">No valuation records available</td></tr>`;
    }
    html += `</tbody></table></div>`;

    // ═══════════ SECTION 2: METHODOLOGY ═══════════
    html += `<div class="pdf-section" style="page-break-before:always"><h2>2. Methodology Analysis</h2>`;

    // 2a — Methodology Mix (from valuations API if available, else from rendered data)
    html += `<h3>2.1 Methodology Mix</h3>`;
    const methodCounts = {};
    let methodTotal = 0;
    if (valuations.length) {
      for (const v of valuations) {
        const m = v.methodology_display || v.methodology || 'Unknown';
        methodCounts[m] = (methodCounts[m] || 0) + 1;
        methodTotal++;
      }
    }

    if (methodTotal > 0) {
      const methodEntries = Object.entries(methodCounts).sort((a, b) => b[1] - a[1]);
      html += `<table><thead><tr><th>Methodology</th><th class="r">Count</th><th class="r">% of Portfolio</th></tr></thead><tbody>`;
      for (const [method, count] of methodEntries) {
        const pct = (count / methodTotal * 100).toFixed(1);
        html += `<tr><td>${esc(method)}</td><td class="r">${count}</td><td class="r">${pct}%</td></tr>`;
      }
      html += `<tr style="font-weight:700;background:#f0f4f8"><td>Total</td><td class="r">${methodTotal}</td><td class="r">100.0%</td></tr>`;
      html += `</tbody></table>`;
    } else {
      // Fallback to static methodology data (same as dashboard renders)
      const methods = [
        { name: 'Revenue Multiple', pct: 38 },
        { name: 'EBITDA Multiple',  pct: 28 },
        { name: 'DCF',              pct: 18 },
        { name: 'P/BV Multiple',    pct: 10 },
        { name: 'Cost Method',      pct: 6 },
      ];
      html += `<table><thead><tr><th>Methodology</th><th class="r">% of Portfolio</th></tr></thead><tbody>`;
      for (const m of methods) {
        html += `<tr><td>${m.name}</td><td class="r">${m.pct}%</td></tr>`;
      }
      html += `</tbody></table>`;
    }

    // 2b — IPEV Level Classification
    html += `<h3>2.2 IPEV Level Classification</h3>`;
    const ipevLabels = { 1: 'Level 1 — Quoted (Exchange Listed)', 2: 'Level 2 — Observable Inputs (Peer Multiples)', 3: 'Level 3 — Unobservable Inputs (DCF / Last Round)' };
    const ipevCounts = { 1: 0, 2: 0, 3: 0 };
    let ipevTotal = 0;
    let ipevFV = { 1: 0, 2: 0, 3: 0 };

    if (valuations.length) {
      for (const v of valuations) {
        const lvl = v.ipev_level;
        if (lvl && ipevCounts.hasOwnProperty(lvl)) {
          ipevCounts[lvl]++;
          ipevFV[lvl] += parseFloat(v.fair_value || 0);
          ipevTotal++;
        }
      }
    }

    if (ipevTotal > 0) {
      html += `<table><thead><tr><th>IPEV Level</th><th>Description</th><th class="r">Count</th><th class="r">% Mix</th><th class="r">Fair Value (Cr)</th></tr></thead><tbody>`;
      for (const lvl of [1, 2, 3]) {
        const pct = ipevTotal > 0 ? (ipevCounts[lvl] / ipevTotal * 100).toFixed(1) : '0.0';
        html += `<tr>
          <td class="b">L${lvl}</td>
          <td>${ipevLabels[lvl]}</td>
          <td class="r">${ipevCounts[lvl]}</td>
          <td class="r">${pct}%</td>
          <td class="r">${fmtCr(ipevFV[lvl])}</td>
        </tr>`;
      }
      html += `<tr style="font-weight:700;background:#f0f4f8">
        <td colspan="2">Total</td>
        <td class="r">${ipevTotal}</td>
        <td class="r">100.0%</td>
        <td class="r">${fmtCr(ipevFV[1] + ipevFV[2] + ipevFV[3])}</td>
      </tr>`;
      html += `</tbody></table>`;
    } else {
      html += `<p class="pdf-empty">No IPEV classification data available.</p>`;
    }

    // Detailed valuations table (from API)
    if (valuations.length) {
      html += `<h3>2.3 Detailed Valuation Records</h3>`;
      html += `<table><thead><tr>
        <th>#</th><th>Company</th><th>Scheme</th><th>Date</th>
        <th class="r">Fair Value (Cr)</th><th>Methodology</th><th class="c">IPEV</th>
        <th class="r">MOIC</th><th class="c">Status</th>
      </tr></thead><tbody>`;
      valuations.forEach((v, i) => {
        const ipev = v.ipev_level ? 'L' + v.ipev_level : '—';
        html += `<tr>
          <td>${i + 1}</td>
          <td class="b">${esc(v.company_name || '—')}</td>
          <td>${esc(v.scheme_name || '—')}</td>
          <td>${v.valuation_date ? v.valuation_date.slice(0, 10) : '—'}</td>
          <td class="r">${fmtCr(v.fair_value)}</td>
          <td>${esc(v.methodology_display || v.methodology || '—')}</td>
          <td class="c">${ipev}</td>
          <td class="r">${v.multiple != null ? v.multiple.toFixed(2) + 'x' : '—'}</td>
          <td class="c">${esc(v.status || '—')}</td>
        </tr>`;
      });
      html += `</tbody></table>`;
    }

    html += `</div>`;

    // ═══════════ SECTION 3: VALUE BRIDGE ═══════════
    html += `<div class="pdf-section" style="page-break-before:always"><h2>3. Value Bridge &mdash; Cost to Fair Value by Sector</h2>`;

    // Aggregate by sector
    let bridgeTotalCost = 0, bridgeTotalFV = 0;
    const bySector = {};
    for (const inv of investments) {
      const cost = parseFloat(inv.total_invested || 0);
      const fv   = parseFloat(inv.latest_valuation || inv.current_value || 0);
      if (!cost && !fv) continue;
      bridgeTotalCost += cost;
      bridgeTotalFV   += fv;
      const sec = inv.sector || 'Other';
      if (!bySector[sec]) bySector[sec] = { cost: 0, fv: 0 };
      bySector[sec].cost += cost;
      bySector[sec].fv   += fv;
    }
    const bridgeGain = bridgeTotalFV - bridgeTotalCost;
    const bridgeGainPct = bridgeTotalCost > 0 ? (bridgeGain / bridgeTotalCost * 100).toFixed(1) + '%' : '—';
    const bridgeMOIC = bridgeTotalCost > 0 ? (bridgeTotalFV / bridgeTotalCost).toFixed(2) + 'x' : '—';

    // Value bridge KPIs
    html += `<div class="pdf-kpi-grid">
      ${_pdfKpi('Total Cost', fmtCr(bridgeTotalCost))}
      ${_pdfKpi('Fair Value', fmtCr(bridgeTotalFV))}
      ${_pdfKpi('Unrealized Gain', (bridgeGain >= 0 ? '+' : '') + fmtCr(bridgeGain), bridgeGainPct)}
      ${_pdfKpi('Portfolio MOIC', bridgeMOIC)}
      ${_pdfKpi('Sectors Covered', Object.keys(bySector).length)}
    </div>`;

    // Visual value bridge summary (text-based waterfall)
    html += `<div style="background:#f8fafc;border:1px solid #d0d5dd;border-radius:8px;padding:14px;margin-bottom:14px">
      <div style="font-size:12px;font-weight:700;color:#333;margin-bottom:8px">Value Bridge Waterfall (₹ Cr)</div>
      <table style="border:none">
        <tr>
          <td style="border:none;width:100px;font-size:11px;color:#666">Cost Basis</td>
          <td style="border:none"><div style="background:#2563eb;height:20px;border-radius:3px;width:${bridgeTotalCost > 0 ? Math.min((bridgeTotalCost / Math.max(bridgeTotalCost, bridgeTotalFV) * 100), 100).toFixed(0) : 0}%;min-width:2px"></div></td>
          <td style="border:none;width:100px;text-align:right;font-size:12px;font-weight:600">${fmtCr(bridgeTotalCost)}</td>
        </tr>
        <tr>
          <td style="border:none;width:100px;font-size:11px;color:#666">${bridgeGain >= 0 ? 'Gain' : 'Loss'}</td>
          <td style="border:none"><div style="background:${bridgeGain >= 0 ? '#34d399' : '#f87171'};height:20px;border-radius:3px;width:${Math.min(Math.abs(bridgeGain) / Math.max(bridgeTotalCost, bridgeTotalFV) * 100, 100).toFixed(0)}%;min-width:2px"></div></td>
          <td style="border:none;width:100px;text-align:right;font-size:12px;font-weight:600;color:${bridgeGain >= 0 ? '#16a34a' : '#dc2626'}">${bridgeGain >= 0 ? '+' : ''}${fmtCr(bridgeGain)}</td>
        </tr>
        <tr>
          <td style="border:none;width:100px;font-size:11px;color:#666">Fair Value</td>
          <td style="border:none"><div style="background:#10b981;height:20px;border-radius:3px;width:${bridgeTotalFV > 0 ? Math.min((bridgeTotalFV / Math.max(bridgeTotalCost, bridgeTotalFV) * 100), 100).toFixed(0) : 0}%;min-width:2px"></div></td>
          <td style="border:none;width:100px;text-align:right;font-size:12px;font-weight:600">${fmtCr(bridgeTotalFV)}</td>
        </tr>
      </table>
    </div>`;

    // Sector breakdown table
    const sectorEntries = Object.entries(bySector).sort((a, b) => b[1].fv - a[1].fv);

    html += `<table><thead><tr>
      <th>#</th><th>Sector</th><th class="r">Cost (Cr)</th><th class="r">FV (Cr)</th>
      <th class="r">Gain (Cr)</th><th class="r">Gain %</th><th class="r">MOIC</th><th class="r">FV Weight</th>
    </tr></thead><tbody>`;

    if (sectorEntries.length) {
      sectorEntries.forEach(([sec, d], i) => {
        const g = d.fv - d.cost;
        const gPct = d.cost > 0 ? (g / d.cost * 100).toFixed(1) + '%' : '—';
        const m = d.cost > 0 ? (d.fv / d.cost).toFixed(2) + 'x' : '—';
        const wt = bridgeTotalFV > 0 ? (d.fv / bridgeTotalFV * 100).toFixed(1) + '%' : '—';
        const gColor = g >= 0 ? '#16a34a' : '#dc2626';
        html += `<tr>
          <td>${i + 1}</td>
          <td class="b">${esc(sec)}</td>
          <td class="r">${fmtCr(d.cost)}</td>
          <td class="r">${fmtCr(d.fv)}</td>
          <td class="r" style="color:${gColor}">${g >= 0 ? '+' : ''}${fmtCr(g)}</td>
          <td class="r" style="color:${gColor}">${gPct}</td>
          <td class="r">${m}</td>
          <td class="r">${wt}</td>
        </tr>`;
      });
      // Totals row
      html += `<tr style="font-weight:700;background:#f0f4f8">
        <td></td><td>Total</td>
        <td class="r">${fmtCr(bridgeTotalCost)}</td>
        <td class="r">${fmtCr(bridgeTotalFV)}</td>
        <td class="r" style="color:${bridgeGain >= 0 ? '#16a34a' : '#dc2626'}">${bridgeGain >= 0 ? '+' : ''}${fmtCr(bridgeGain)}</td>
        <td class="r">${bridgeGainPct}</td>
        <td class="r">${bridgeMOIC}</td>
        <td class="r">100.0%</td>
      </tr>`;
    } else {
      html += `<tr><td colspan="8" style="text-align:center;color:#999">No sector data available</td></tr>`;
    }
    html += `</tbody></table></div>`;

    // Footer
    html += `<div class="pdf-footer">TrackFundAI &middot; Confidential &middot; ${esc(_ctx.fundName || 'All Funds')} &middot; Valuation Report &middot; ${now}</div></div>`;

    const fileName = `TrackFundAI_Valuations_${(_ctx.fundName || 'AllFunds').replace(/[^a-zA-Z0-9]/g, '_')}_${new Date().toISOString().slice(0, 10)}.pdf`;
    await _generatePDF(html, fileName);

  } catch(e) {
    console.error('Valuation Report PDF error:', e);
    alert('Error generating Valuation Report. Please try again.');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '&#128202; Generate Report'; }
  }
};

})();
