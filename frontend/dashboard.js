/**
 * dashboard.js
 * Fetches data from Django REST API and renders:
 *   - KPI cards
 *   - All Chart.js charts (revenue trend, EBITDA, GP%, cash flow, segments)
 *   - P&L table
 *   - Working capital chips
 *   - Segment cards
 */

const Dashboard = (() => {

  /* ── API base URL ────────────────────────────────────── */
  const API_BASE = (window.APP_CONFIG && window.APP_CONFIG.API_BASE) || '/api';

  /* ── Chart instances (kept for destroy-on-refresh) ───── */
  const charts = {};

  /* ── Cached data (avoid redundant re-renders) ────────── */
  let _cachedLoadedAt = null;

  /* ── Colour palette for charts ───────────────────────── */
  const PALETTE = {
    blue:   '#00d4ff',
    green:  '#00ff9d',
    amber:  '#ffb800',
    red:    '#ff4455',
    violet: '#a855f7',
    cyan:   '#06ffd6',
    muted:  'rgba(136,136,170,0.4)',
  };

  const SEGMENT_COLORS = {
    'HID':            PALETTE.blue,
    'LabFriend':      PALETTE.green,
    'Project/NGS':    PALETTE.cyan,
    'Sci.Lab':        PALETTE.violet,
    'Sci.Lab-Qiagen': PALETTE.amber,
    'Service':        PALETTE.red,
    'Total':          PALETTE.muted,
  };

  /* ── Chart.js global defaults ────────────────────────── */
  function _setChartDefaults() {
    Chart.defaults.color = '#8888aa';
    Chart.defaults.borderColor = 'rgba(255,255,255,0.06)';
    Chart.defaults.font.family = "'Space Grotesk', sans-serif";
    Chart.defaults.plugins.legend.labels.color = '#8888aa';
    Chart.defaults.plugins.tooltip.backgroundColor = 'rgba(10,10,30,0.92)';
    Chart.defaults.plugins.tooltip.borderColor = 'rgba(0,212,255,0.25)';
    Chart.defaults.plugins.tooltip.borderWidth = 1;
    Chart.defaults.plugins.tooltip.padding = 12;
    Chart.defaults.plugins.tooltip.titleColor = '#f0f0ff';
    Chart.defaults.plugins.tooltip.bodyColor = '#8888aa';
    // Disable animations globally for performance
    Chart.defaults.animation = false;
    Chart.defaults.transitions.active.animation.duration = 0;
    // Cap render DPR — cuts retina rendering cost in half (huge on M-series Macs)
    Chart.defaults.devicePixelRatio = 1;
    // Default bar sizing — prevents razor-thin bars when only a few categories
    Chart.defaults.datasets.bar.categoryPercentage = 0.7;
    Chart.defaults.datasets.bar.barPercentage = 0.85;
    Chart.defaults.datasets.bar.borderRadius = 4;
  }

  /* ─── Helpers ─────────────────────────────────────────── */
  function fmt(val, prefix = 'MYR ') {
    if (val === null || val === undefined) return '—';
    const abs = Math.abs(val);
    if (abs >= 1_000_000) return prefix + (val / 1_000_000).toFixed(2) + 'M';
    if (abs >= 1_000)     return prefix + (val / 1_000).toFixed(1) + 'k';
    return prefix + val.toFixed(0);
  }

  // Cash flow values are already in MYR '000 — format as-is with k suffix
  function fmtCF(val, prefix = '') {
    if (val === null || val === undefined) return '—';
    const abs = Math.abs(val);
    if (abs >= 1_000) return prefix + (val / 1_000).toFixed(2) + 'M';
    return prefix + val.toFixed(1) + 'k';
  }

  function fmtDays(val) {
    if (val === null || val === undefined) return '—';
    return parseFloat(val).toFixed(1);
  }

  // "Jan 2024" -> "Jan '24" — shorter axis labels
  function _shortPeriod(period) {
    if (!period) return '';
    const parts = period.split(' ');
    if (parts.length !== 2) return period;
    const yr = parts[1].slice(-2);
    return `${parts[0]} '${yr}`;
  }

  function deltaClass(val, budget) {
    if (val === null || budget === null) return '';
    return val >= budget ? 'positive' : 'negative';
  }

  function deltaStr(val, budget, suffix = '') {
    if (val === null || budget === null) return '';
    const diff = val - budget;
    const sign = diff >= 0 ? '+' : '';
    return sign + fmt(diff) + (suffix ? ' ' + suffix : '');
  }

  function _destroyChart(id) {
    if (charts[id]) { charts[id].destroy(); delete charts[id]; }
  }

  /* ═══════════════════════════════════════════════════════
     FETCH & RENDER
  ═══════════════════════════════════════════════════════ */
  async function fetchAll() {
    // Always hide the banner first
    document.getElementById('no-data-banner')?.classList.add('hidden');

    try {
      // Check status — if no data loaded yet show banner, don't spam 503s
      const statusRes = await fetch(`${API_BASE}/status/`).catch(() => null);
      if (!statusRes || !statusRes.ok) {
        _showNoDataState();
        return;
      }
      const statusData = await statusRes.json();
      if (!statusData.data_loaded) {
        _showNoDataState();
        return;
      }

      // Skip full re-render if the same file is already loaded
      if (_cachedLoadedAt && _cachedLoadedAt === statusData.loaded_at) {
        return;
      }
      _cachedLoadedAt = statusData.loaded_at;

      const [summaryRes, monthlyRes, cfRes, wcRes, segRes] = await Promise.allSettled([
        fetch(`${API_BASE}/summary/`),
        fetch(`${API_BASE}/monthly-pl/`),
        fetch(`${API_BASE}/cash-flow/`),
        fetch(`${API_BASE}/working-capital/`),
        fetch(`${API_BASE}/sales-segments/`),
      ]);

      // If summary failed
      if (summaryRes.status === 'rejected' || !summaryRes.value.ok) {
        console.warn('Summary fetch failed');
        _showNoDataState();
        return;
      }

      const summary  = await summaryRes.value.json();
      const monthly  = monthlyRes.status === 'fulfilled' && monthlyRes.value.ok
        ? (await monthlyRes.value.json()).monthly_pl || [] : [];
      const cf       = cfRes.status === 'fulfilled' && cfRes.value.ok
        ? (await cfRes.value.json()).cash_flow || [] : [];
      const wc       = wcRes.status === 'fulfilled' && wcRes.value.ok
        ? (await wcRes.value.json()).working_capital || {} : {};
      const segments = segRes.status === 'fulfilled' && segRes.value.ok
        ? (await segRes.value.json()).sales_segments || {} : {};

      _renderHeroStats(summary);
      _renderKPIs(summary);
      _renderPLTable(monthly);
      _renderWorkingCapital(wc, cf);
      _renderSegments(segments);

      // Charts — staggered across 4 frames to avoid blocking the main thread
      setTimeout(() => {
        _renderRevenueTrendChart(monthly);
        _renderEBITDATrendChart(monthly);
        _renderCostDonut(summary);
      }, 0);
      setTimeout(() => {
        _renderGPTrendChart(monthly);
        _renderYTDBarChart(summary);
      }, 50);
      setTimeout(() => {
        _renderCashFlowWaterfall(cf);
        _renderCashTrendChart(cf);
      }, 100);
      setTimeout(() => {
        _renderDSOTrendChart(wc);
        _renderNWCTrendChart(wc);
        _renderSegmentBarChart(segments);
        _renderSegmentDonut(segments);
      }, 150);

    } catch (err) {
      console.error('Dashboard fetch error:', err);
      _showNoDataState();
    }
  }

  function _showNoDataState() {
    document.getElementById('no-data-banner')?.classList.remove('hidden');
  }

  /* ═══════════════════════════════════════════════════════
     HERO STATS
  ═══════════════════════════════════════════════════════ */
  function _renderHeroStats(data) {
    const el = id => document.getElementById(id);

    el('stat-month').textContent = data.report_month || 'May 2025';
    el('stat-ytd-rev').textContent = fmt(data.ytd_revenue_2025);

    const yoy = data.yoy_revenue_growth_pct;
    if (yoy !== null && yoy !== undefined) {
      const sign = yoy >= 0 ? '+' : '';
      el('stat-yoy').textContent = sign + yoy.toFixed(1) + '%';
      el('stat-yoy').style.color = yoy >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
    }
  }

  /* ═══════════════════════════════════════════════════════
     KPI CARDS
  ═══════════════════════════════════════════════════════ */
  function _renderKPIs(data) {
    const s = data.summary_pl || {};

    const rev   = s.revenue;
    const gp    = s.gross_profit;
    const gpPct = s.gp_pct;
    const opex  = s.opex;
    const ebitda = s.ebitda;
    const normEbitda = s.normalized_ebitda;

    // Revenue
    _setKPI('kpi-rev-val',   fmt(rev?.actual_month));
    _setKPI('kpi-rev-delta', _renderDelta(rev?.actual_month, rev?.budget_month));
    const revBar = document.getElementById('kpi-rev-bar');
    if (revBar && rev?.actual_month && rev?.budget_month) {
      revBar.style.width = Math.min(100, (rev.actual_month / rev.budget_month) * 100) + '%';
    }

    // Gross Profit
    _setKPI('kpi-gp-val',   fmt(gp?.actual_month));
    _setKPI('kpi-gp-delta', _renderDelta(gp?.actual_month, gp?.budget_month));
    if (gpPct?.actual_month !== undefined) {
      document.getElementById('kpi-gp-pct').textContent = `GP%: ${gpPct.actual_month?.toFixed(1)}%`;
    }

    // EBITDA
    _setKPI('kpi-ebitda-val',   fmt(ebitda?.actual_month));
    _setKPI('kpi-ebitda-delta', _renderDelta(ebitda?.actual_month, ebitda?.budget_month));
    if (normEbitda?.actual_month !== undefined) {
      document.getElementById('kpi-norm-ebitda').textContent = `Normalised: ${fmt(normEbitda.actual_month)}`;
    }

    // OPEX
    _setKPI('kpi-opex-val',   fmt(opex?.actual_month));
    _setKPI('kpi-opex-delta', _renderDelta(opex?.actual_month, opex?.budget_month, true));

    // YTD Revenue
    _setKPI('kpi-ytdrev-val', fmt(data.ytd_revenue_2025));
    const ytdDelta = document.getElementById('kpi-ytdrev-delta');
    if (ytdDelta && data.yoy_revenue_growth_pct !== null) {
      const yoy = data.yoy_revenue_growth_pct;
      const sign = yoy >= 0 ? '▲' : '▼';
      ytdDelta.textContent = `${sign} ${Math.abs(yoy).toFixed(1)}% YoY`;
      ytdDelta.className = 'kpi-delta ' + (yoy >= 0 ? 'positive' : 'negative');
    }

    // YTD EBITDA
    _setKPI('kpi-ytdebitda-val', fmt(ebitda?.actual_ytd));
    _setKPI('kpi-ytdebitda-delta', _renderDelta(ebitda?.actual_ytd, ebitda?.budget_ytd));
  }

  function _setKPI(id, html) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
  }

  function _renderDelta(actual, budget, invertGood = false) {
    if (actual === null || actual === undefined || budget === null || budget === undefined) return '';
    const diff = actual - budget;
    const pct  = budget !== 0 ? ((diff / Math.abs(budget)) * 100).toFixed(1) : 0;
    const sign  = diff >= 0 ? '▲' : '▼';
    const isGood = invertGood ? diff <= 0 : diff >= 0;
    const cls   = isGood ? 'positive' : 'negative';
    return `<span class="kpi-delta ${cls}">${sign} ${Math.abs(pct)}% vs Budget</span>`;
  }

  /* ═══════════════════════════════════════════════════════
     P&L TABLE
  ═══════════════════════════════════════════════════════ */
  function _renderPLTable(monthly) {
    const tbody = document.getElementById('pl-tbody');
    if (!tbody) return;

    const yearFilter = document.getElementById('pl-year-filter');
    const selectedYear = yearFilter ? parseInt(yearFilter.value) || 0 : 0;

    const filtered = selectedYear === 0
      ? monthly
      : monthly.filter(m => m.year === selectedYear);

    if (!filtered.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="table-empty">No data available for the selected period.</td></tr>';
      return;
    }

    tbody.innerHTML = filtered.map(m => {
      const gpClass    = m.gross_profit >= 0 ? '' : 'negative';
      const ebitdaClass = m.ebitda >= 0 ? '' : 'negative';
      return `
        <tr>
          <td>${m.period}</td>
          <td class="num">${fmt(m.revenue)}</td>
          <td class="num">${fmt(m.cogs)}</td>
          <td class="num ${gpClass}">${fmt(m.gross_profit)}</td>
          <td class="num">${m.gp_pct?.toFixed(1) ?? '—'}%</td>
          <td class="num">${fmt(m.opex)}</td>
          <td class="num ${ebitdaClass}">${fmt(m.ebitda)}</td>
          <td class="num ${m.normalized_ebitda >= 0 ? '' : 'negative'}">${fmt(m.normalized_ebitda)}</td>
        </tr>`;
    }).join('');

    // Year filter listener
    if (yearFilter && !yearFilter._listenerAdded) {
      yearFilter._listenerAdded = true;
      yearFilter.addEventListener('change', () => _renderPLTable(monthly));
    }

    // Export CSV button
    const exportBtn = document.getElementById('btn-export-pl');
    if (exportBtn && !exportBtn._listenerAdded) {
      exportBtn._listenerAdded = true;
      exportBtn.addEventListener('click', () => _exportCSV(filtered));
    }
  }

  function _exportCSV(data) {
    const headers = ['Period','Revenue','COGS','Gross Profit','GP%','OPEX','EBITDA','Norm EBITDA'];
    const rows = data.map(m => [
      m.period, m.revenue, m.cogs, m.gross_profit,
      m.gp_pct, m.opex, m.ebitda, m.normalized_ebitda,
    ]);
    const csv = [headers, ...rows].map(r => r.join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'analisa_pl.csv';
    a.click();
  }

  /* ═══════════════════════════════════════════════════════
     WORKING CAPITAL CHIPS
  ═══════════════════════════════════════════════════════ */
  function _renderWorkingCapital(wc, cf) {
    // Latest DSO/DIO/DPO from the DSO sheet
    const dsoData = wc.dso_dio_dpo || [];
    const latest  = dsoData[dsoData.length - 1] || {};

    _setText('wc-dso', fmtDays(latest.dso));
    _setText('wc-dio', fmtDays(latest.dio));
    _setText('wc-dpo', fmtDays(latest.dpo));
    _setText('wc-ccc', fmtDays(latest.ccc));
    _setText('wc-nwc', fmt(latest.nwc));

    // Latest cash from cash flow (values are in MYR '000)
    const latestCF = cf[cf.length - 1];
    _setText('wc-cash', fmtCF(latestCF?.closing_cash ?? null));
  }

  function _setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  /* ═══════════════════════════════════════════════════════
     SEGMENT CARDS
  ═══════════════════════════════════════════════════════ */
  function _renderSegments(segments) {
    const grid = document.getElementById('segment-grid');
    if (!grid) return;

    const ytd = segments.ytd_comparison || {};
    const entries = Object.entries(ytd).filter(([k]) => k !== 'Total' && k !== 'check');

    if (!entries.length) {
      grid.innerHTML = '<p class="table-empty">Segment data not available.</p>';
      return;
    }

    // Find max for bar scaling
    const maxVal = Math.max(...entries.map(([, v]) => v.ytd_2025 || 0));

    grid.innerHTML = entries.map(([seg, data]) => {
      const val2025 = data.ytd_2025 || 0;
      const val2024 = data.ytd_2024 || 0;
      const ratio   = data.yoy_ratio;
      const color   = SEGMENT_COLORS[seg] || PALETTE.blue;
      const barPct  = maxVal > 0 ? (val2025 / maxVal * 100) : 0;

      let changeHtml = '';
      if (ratio !== null && ratio !== undefined) {
        const pct = ((ratio - 1) * 100).toFixed(1);
        const cls = pct >= 0 ? 'up' : 'down';
        const arrow = pct >= 0 ? '▲' : '▼';
        changeHtml = `<span class="segment-change ${cls}">${arrow} ${Math.abs(pct)}% YoY</span>`;
      }

      return `
        <div class="segment-card">
          <div class="segment-name">${seg}</div>
          <div class="segment-value">${fmt(val2025)}</div>
          ${changeHtml}
          <div class="segment-bar-wrap">
            <div class="segment-bar" style="width:${barPct}%;background:${color}"></div>
          </div>
        </div>`;
    }).join('');
  }

  /* ═══════════════════════════════════════════════════════
     CHARTS
  ═══════════════════════════════════════════════════════ */

  /* Revenue Trend ──────────────────────────────────────── */
  function _renderRevenueTrendChart(monthly) {
    const ctx = document.getElementById('chart-revenue-trend');
    if (!ctx) return;
    _destroyChart('revenue-trend');

    // Sort by year then month; exclude rows where both revenue and GP are zero (future placeholders)
    const sorted = [...monthly]
      .sort((a, b) => a.year !== b.year ? a.year - b.year : a.month_num - b.month_num)
      .filter(m => m.revenue !== 0 || m.gross_profit !== 0);

    charts['revenue-trend'] = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: sorted.map(m => _shortPeriod(m.period)),
        datasets: [
          {
            label: 'Revenue',
            data: sorted.map(m => m.revenue ?? 0),
            backgroundColor: 'rgba(0,212,255,0.55)',
            borderColor: PALETTE.blue,
            borderWidth: 1,
            borderRadius: 4,
            order: 2,
          },
          {
            label: 'Gross Profit',
            type: 'line',
            data: sorted.map(m => m.gross_profit ?? null),
            borderColor: PALETTE.green,
            backgroundColor: 'transparent',
            tension: 0.35,
            pointRadius: 3,
            pointBackgroundColor: PALETTE.green,
            borderWidth: 2.5,
            spanGaps: true,
            order: 1,
          },
        ],
      },
      options: _mixedOptions('MYR'),
    });
  }

  /* EBITDA Trend ───────────────────────────────────────── */
  function _renderEBITDATrendChart(monthly) {
    const ctx = document.getElementById('chart-ebitda-trend');
    if (!ctx) return;
    _destroyChart('ebitda-trend');

    const sorted = [...monthly]
      .sort((a, b) => a.year !== b.year ? a.year - b.year : a.month_num - b.month_num)
      .filter(m => m.revenue !== 0 || m.ebitda !== 0);

    charts['ebitda-trend'] = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: sorted.map(m => _shortPeriod(m.period)),
        datasets: [
          {
            label: 'EBITDA',
            data: sorted.map(m => m.ebitda ?? 0),
            backgroundColor: sorted.map(m => (m.ebitda || 0) >= 0 ? 'rgba(6,255,214,0.55)' : 'rgba(255,68,85,0.55)'),
            borderColor:     sorted.map(m => (m.ebitda || 0) >= 0 ? PALETTE.cyan : PALETTE.red),
            borderWidth: 1,
            borderRadius: 4,
          },
        ],
      },
      options: _barOptions('MYR'),
    });
  }

  /* Cost Donut ─────────────────────────────────────────── */
  function _renderCostDonut(data) {
    const ctx = document.getElementById('chart-cost-donut');
    if (!ctx) return;
    _destroyChart('cost-donut');

    const s = data.summary_pl || {};
    const cogs = s.cogs?.actual_month || 0;
    const opex = s.opex?.actual_month || 0;
    const gp   = s.gross_profit?.actual_month || 0;

    charts['cost-donut'] = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['COGS', 'OPEX', 'Gross Profit'],
        datasets: [{
          data: [Math.abs(cogs), Math.abs(opex), Math.abs(gp)],
          backgroundColor: [PALETTE.red, PALETTE.amber, PALETTE.green],
          borderColor: 'rgba(0,0,0,0.4)',
          borderWidth: 2,
          hoverOffset: 8,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '65%',
        plugins: {
          legend: { position: 'bottom', labels: { padding: 16, usePointStyle: true } },
          tooltip: { callbacks: { label: ctx => `${ctx.label}: ${fmt(ctx.raw)}` } },
        },
      },
    });
  }

  /* GP% Trend ──────────────────────────────────────────── */
  function _renderGPTrendChart(monthly) {
    const ctx = document.getElementById('chart-gp-trend');
    if (!ctx) return;
    _destroyChart('gp-trend');

    const sorted = [...monthly]
      .sort((a, b) => a.year !== b.year ? a.year - b.year : a.month_num - b.month_num)
      .filter(m => m.revenue !== 0);

    charts['gp-trend'] = new Chart(ctx, {
      type: 'line',
      data: {
        labels: sorted.map(m => _shortPeriod(m.period)),
        datasets: [{
          label: 'GP%',
          data: sorted.map(m => m.gp_pct),
          borderColor: PALETTE.green,
          backgroundColor: 'rgba(0,255,157,0.1)',
          fill: true,
          tension: 0.4,
          pointRadius: 4,
          pointBackgroundColor: PALETTE.green,
          borderWidth: 2.5,
          spanGaps: true,
        }],
      },
      options: {
        ..._lineOptions('%'),
        scales: {
          x: { grid: { color: 'rgba(255,255,255,0.04)' }, ticks: { maxRotation: 45, maxTicksLimit: 12 } },
          y: {
            grid: { color: 'rgba(255,255,255,0.05)' },
            min: 0,
            max: 60,
            ticks: { callback: v => v + '%', maxTicksLimit: 6 },
          },
        },
      },
    });
  }

  /* YTD Comparison Bar ─────────────────────────────────── */
  function _renderYTDBarChart(data) {
    const ctx = document.getElementById('chart-ytd-bar');
    if (!ctx) return;
    _destroyChart('ytd-bar');

    // Use month-level summary data from the summary endpoint
    const s = data.summary_pl || {};
    const labels = ['Revenue', 'Gross Profit', 'EBITDA', 'Norm. EBITDA'];
    const actuals = [
      s.revenue?.actual_month,
      s.gross_profit?.actual_month,
      s.ebitda?.actual_month,
      s.normalized_ebitda?.actual_month,
    ];
    const budgets = [
      s.revenue?.budget_month,
      s.gross_profit?.budget_month,
      s.ebitda?.budget_month,
      s.normalized_ebitda?.budget_month,
    ];

    charts['ytd-bar'] = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [
          {
            label: 'Actual (Month)',
            data: actuals,
            backgroundColor: [PALETTE.blue, PALETTE.green, PALETTE.cyan, PALETTE.violet].map(c => c + '55'),
            borderColor: [PALETTE.blue, PALETTE.green, PALETTE.cyan, PALETTE.violet],
            borderWidth: 2,
            borderRadius: 6,
          },
          {
            label: 'Budget (Month)',
            data: budgets,
            backgroundColor: 'rgba(255,255,255,0.06)',
            borderColor: 'rgba(255,255,255,0.2)',
            borderWidth: 2,
            borderRadius: 6,
          },
        ],
      },
      options: _barOptions('MYR'),
    });
  }

  /* Cash Flow Waterfall ────────────────────────────────── */
  function _renderCashFlowWaterfall(cf) {
    const ctx = document.getElementById('chart-cf-waterfall');
    if (!ctx) return;
    _destroyChart('cf-waterfall');

    // Only show 2024+ to avoid pre-2024 outliers; keep last 12 clean points
    const clean = cf.filter(m => {
      const yr = parseInt(m.period.split(' ')[1]);
      return yr >= 2024 && m.net_cash_ops !== null && m.net_cash_ops !== undefined;
    }).slice(-12);

    charts['cf-waterfall'] = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: clean.map(m => _shortPeriod(m.period)),
        datasets: [
          {
            label: 'Net Cash (Ops)',
            data: clean.map(m => m.net_cash_ops ?? 0),
            backgroundColor: clean.map(m => (m.net_cash_ops || 0) >= 0 ? 'rgba(0,255,157,0.55)' : 'rgba(255,68,85,0.55)'),
            borderColor:     clean.map(m => (m.net_cash_ops || 0) >= 0 ? PALETTE.green : PALETTE.red),
            borderWidth: 1,
            borderRadius: 4,
          },
        ],
      },
      options: _cfBarOptions(),
    });
  }

  /* Closing Cash Trend ─────────────────────────────────── */
  function _renderCashTrendChart(cf) {
    const ctx = document.getElementById('chart-cash-trend');
    if (!ctx) return;
    _destroyChart('cash-trend');

    // Only show 2024 onwards — earlier data has inconsistent opening balances
    const recent = cf.filter(m => {
      const yr = parseInt(m.period.split(' ')[1]);
      return yr >= 2024 && m.closing_cash !== null && m.closing_cash !== undefined && m.closing_cash !== 0;
    });

    charts['cash-trend'] = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: recent.map(m => _shortPeriod(m.period)),
        datasets: [{
          label: 'Closing Cash',
          data: recent.map(m => m.closing_cash ?? 0),
          backgroundColor: 'rgba(0,212,255,0.55)',
          borderColor: PALETTE.blue,
          borderWidth: 1,
          borderRadius: 4,
        }],
      },
      options: _cfBarOptions(),
    });
  }

  /* DSO / DIO / DPO Trend ──────────────────────────────── */
  function _renderDSOTrendChart(wc) {
    const ctx = document.getElementById('chart-dso-trend');
    if (!ctx) return;
    _destroyChart('dso-trend');

    const nwcTrend = wc.nwc_trend || [];
    const recent   = nwcTrend.slice(-18);

    charts['dso-trend'] = new Chart(ctx, {
      type: 'line',
      data: {
        labels: recent.map(m => _shortPeriod(m.period)),
        datasets: [
          {
            label: 'DSO (days)',
            data: recent.map(m => m.dso),
            borderColor: PALETTE.blue,
            backgroundColor: 'transparent',
            tension: 0.4, pointRadius: 3, borderWidth: 2,
          },
          {
            label: 'DIO (days)',
            data: recent.map(m => m.dsi),
            borderColor: PALETTE.amber,
            backgroundColor: 'transparent',
            tension: 0.4, pointRadius: 3, borderWidth: 2,
          },
          {
            label: 'DPO (days)',
            data: recent.map(m => m.dpo),
            borderColor: PALETTE.violet,
            backgroundColor: 'transparent',
            tension: 0.4, pointRadius: 3, borderWidth: 2,
          },
        ],
      },
      options: _lineOptions('days'),
    });
  }

  /* NWC Trend ──────────────────────────────────────────── */
  function _renderNWCTrendChart(wc) {
    const ctx = document.getElementById('chart-nwc-trend');
    if (!ctx) return;
    _destroyChart('nwc-trend');

    const nwcTrend = (wc.nwc_trend || []).filter(m => m.nwc !== null && m.nwc !== undefined && m.nwc !== 0);

    charts['nwc-trend'] = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: nwcTrend.map(m => _shortPeriod(m.period)),
        datasets: [{
          label: 'Net Working Capital',
          data: nwcTrend.map(m => m.nwc ?? 0),
          backgroundColor: 'rgba(6,255,214,0.55)',
          borderColor: PALETTE.cyan,
          borderWidth: 1,
          borderRadius: 4,
        }],
      },
      options: _barOptions('MYR'),
    });
  }

  /* Segment Bar ────────────────────────────────────────── */
  function _renderSegmentBarChart(segments) {
    const ctx = document.getElementById('chart-segment-bar');
    if (!ctx) return;
    _destroyChart('segment-bar');

    const ytd = segments.ytd_comparison || {};
    // Drop Total/check AND any segment with zero activity in both years
    // (avoids 2 razor-thin spikes from a sparse dataset)
    const entries = Object.entries(ytd)
      .filter(([k, v]) => k !== 'Total' && k !== 'check' && ((v.ytd_2025 || 0) > 0 || (v.ytd_2024 || 0) > 0))
      .sort(([, a], [, b]) => (b.ytd_2025 || 0) - (a.ytd_2025 || 0));

    charts['segment-bar'] = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: entries.map(([k]) => k),
        datasets: [
          {
            label: 'YTD 2025',
            data: entries.map(([, v]) => v.ytd_2025 || 0),
            backgroundColor: entries.map(([k]) => (SEGMENT_COLORS[k] || PALETTE.blue) + '99'),
            borderColor: entries.map(([k]) => SEGMENT_COLORS[k] || PALETTE.blue),
            borderWidth: 1.5, borderRadius: 4,
          },
          {
            label: 'YTD 2024',
            data: entries.map(([, v]) => v.ytd_2024 || 0),
            backgroundColor: 'rgba(255,255,255,0.1)',
            borderColor: 'rgba(255,255,255,0.3)',
            borderWidth: 1.5, borderRadius: 4,
          },
        ],
      },
      options: _barOptions('MYR'),
    });
  }

  /* Segment Donut ──────────────────────────────────────── */
  function _renderSegmentDonut(segments) {
    const ctx = document.getElementById('chart-segment-donut');
    if (!ctx) return;
    _destroyChart('segment-donut');

    const ytd = segments.ytd_comparison || {};
    const entries = Object.entries(ytd).filter(([k]) => k !== 'Total' && k !== 'check' && (ytd[k]?.ytd_2025 || 0) > 0);

    charts['segment-donut'] = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: entries.map(([k]) => k),
        datasets: [{
          data: entries.map(([, v]) => v.ytd_2025 || 0),
          backgroundColor: entries.map(([k]) => (SEGMENT_COLORS[k] || PALETTE.blue) + 'bb'),
          borderColor: entries.map(([k]) => SEGMENT_COLORS[k] || PALETTE.blue),
          borderWidth: 2,
          hoverOffset: 10,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '60%',
        plugins: {
          legend: { position: 'right', labels: { padding: 14, usePointStyle: true } },
          tooltip: { callbacks: { label: ctx => `${ctx.label}: ${fmt(ctx.raw)}` } },
        },
      },
    });
  }

  /* ── Chart option presets ────────────────────────────── */
  // Shared axis scale factories
  function _xAxisScale(maxTicks = 12) {
    return {
      grid: { color: 'rgba(255,255,255,0.04)', drawTicks: false },
      ticks: {
        maxRotation: 0,
        autoSkip: true,
        autoSkipPadding: 12,
        maxTicksLimit: maxTicks,
        font: { size: 11 },
        color: '#8a8aae',
      },
    };
  }
  function _yAxisScale(yLabel) {
    return {
      grid: { color: 'rgba(255,255,255,0.05)', drawTicks: false },
      beginAtZero: true,          // always include zero so negatives/positives sit on common baseline
      ticks: {
        callback: v => yLabel === '%' ? v + '%'
                      : yLabel === 'days' ? v + 'd'
                      : yLabel === "MYR '000" ? fmtCF(v)
                      : fmt(v, ''),
        maxTicksLimit: 6,
        font: { size: 11 },
        color: '#8a8aae',
        padding: 6,
      },
    };
  }

  function _baseOptions(yLabel) {
    const isCF = yLabel === "MYR '000";
    const fmtFn = yLabel === '%'     ? v => (v ?? 0).toFixed(1) + '%'
                : yLabel === 'days'  ? v => (v ?? 0).toFixed(1) + ' days'
                : isCF               ? v => fmtCF(v)
                :                      v => fmt(v);
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      layout: { padding: { top: 4, right: 8, bottom: 0, left: 0 } },
      plugins: {
        legend: { position: 'top', align: 'end', labels: { padding: 14, usePointStyle: true, boxWidth: 8, font: { size: 11 } } },
        tooltip: {
          callbacks: { label: ctx => ` ${ctx.dataset.label}: ${fmtFn(ctx.raw)}` },
        },
      },
      scales: {
        x: _xAxisScale(yLabel === 'days' ? 12 : 12),
        y: _yAxisScale(yLabel),
      },
    };
  }

  function _barOptions(yLabel)   { return _baseOptions(yLabel); }
  function _lineOptions(yLabel)  { return _baseOptions(yLabel); }
  // Revenue-style: bars + overlay line, MYR axis
  function _mixedOptions(yLabel) { return _baseOptions(yLabel); }
  // Cash-flow bar: MYR '000 axis with fmtCF
  function _cfBarOptions()       { return _baseOptions("MYR '000"); }

  /* ═══════════════════════════════════════════════════════
     PUBLIC
  ═══════════════════════════════════════════════════════ */
  function init() {
    _setChartDefaults();
  }

  return { init, fetchAll, API_BASE };

})();
