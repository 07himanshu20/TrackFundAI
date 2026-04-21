/* ============================================================
   deepdive.js
   Rich single-company dashboard (only shown at level=company).
   Window API: window.DeepDive.render(node)
============================================================ */

(() => {
  const charts = {};

  function destroyAll() {
    Object.keys(charts).forEach(k => {
      if (charts[k]) { charts[k].destroy(); charts[k] = null; }
    });
  }

  function render(node) {
    destroyAll();
    document.getElementById('deepdive-title').textContent = node.name;
    const sub = node.description
      ? `${node.description} · Native ${node.currency}`
      : `Native currency: ${node.currency}`;
    document.getElementById('deepdive-subtitle').textContent = sub;

    const fin = node.financials || {};
    renderKPIs(fin.summary || {}, node.currency);
    renderMonthlyPL(fin.monthly_pl || []);
    renderCostStructure(fin.cost_structure || {}, fin.summary || {});
    renderCashFlow(fin.cash_flow || []);
    renderCashTrend(fin.cash_flow || []);
    renderSegments(fin.sales_by_segment || []);
    renderGeo(fin.sales_by_geo || []);
  }

  function renderKPIs(s, ccy) {
    const grid = document.getElementById('deepdive-kpis');
    grid.innerHTML = '';
    const kpis = [
      ['Revenue', s.revenue ?? s.ytd_revenue, 'USD'],
      ['Gross Profit', s.gross_profit ?? s.ytd_gross_profit, 'USD'],
      ['EBITDA', s.ebitda ?? s.ytd_ebitda, 'USD'],
      ['GP %', s.gp_pct, 'percent'],
      ['EBITDA %', s.ebitda_pct, 'percent'],
      ['Period', s.period || '—', 'text'],
    ];
    kpis.forEach(([label, v, fmt]) => {
      const card = document.createElement('div');
      card.className = 'kpi-card';
      let valueHtml;
      if (fmt === 'text') valueHtml = v;
      else if (fmt === 'percent') valueHtml = window.Portfolio.formatPct(v);
      else valueHtml = window.Portfolio.formatUSD(v);
      card.innerHTML = `
        <div class="kpi-label">${label}</div>
        <div class="kpi-value mono">${valueHtml}</div>
        <div class="kpi-sub">${fmt === 'text' ? '' : 'USD-normalised'}</div>
      `;
      grid.appendChild(card);
    });
  }

  function renderMonthlyPL(rows) {
    const ctx = document.getElementById('dd-chart-pl').getContext('2d');
    const labels = rows.map(r => r.period);
    charts.pl = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [
          {label: 'Revenue', data: rows.map(r => r.revenue), backgroundColor: '#00d4ff99', borderColor: '#00d4ff', borderWidth: 1, borderRadius: 2, order: 2},
          {type: 'line', label: 'EBITDA', data: rows.map(r => r.ebitda), borderColor: '#00ff9d', backgroundColor: '#00ff9d33', tension: 0.3, yAxisID: 'y', order: 1},
        ],
      },
      options: commonOpts('USD'),
    });
  }

  function renderCostStructure(cs, s) {
    const ctx = document.getElementById('dd-chart-cost').getContext('2d');
    const rev = s.revenue ?? s.ytd_revenue;
    const cogs = s.cogs ?? (rev && cs.cogs_pct ? rev * cs.cogs_pct / 100 : null);
    const opex = s.opex ?? (rev && cs.opex_pct ? rev * cs.opex_pct / 100 : null);
    const ebitda = s.ebitda ?? (rev && cs.ebitda_pct ? rev * cs.ebitda_pct / 100 : null);

    const data = [cogs, opex, Math.max(0, ebitda || 0)].map(v => v || 0);
    charts.cost = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['COGS', 'OPEX', 'EBITDA'],
        datasets: [{
          data,
          backgroundColor: ['#ff445599','#ffb80099','#00ff9d99'],
          borderColor: ['#ff4455','#ffb800','#00ff9d'],
          borderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {labels: {color: '#8888aa'}},
          tooltip: {callbacks: {label: c => `${c.label}: ${window.Portfolio.formatUSD(c.raw)}`}},
        },
      },
    });
  }

  function renderCashFlow(rows) {
    const ctx = document.getElementById('dd-chart-cf').getContext('2d');
    if (!rows.length) {
      ctx.canvas.parentElement.querySelector('.chart-title').textContent = 'Cash Flow — no data';
      return;
    }
    const labels = rows.map(r => r.period);
    charts.cf = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [
          {label: 'Operating CF', data: rows.map(r => r.operating_cf), backgroundColor: '#00d4ff99'},
          {label: 'Investing CF', data: rows.map(r => r.investing_cf), backgroundColor: '#a855f799'},
          {label: 'Financing CF', data: rows.map(r => r.financing_cf), backgroundColor: '#ffb80099'},
        ],
      },
      options: Object.assign(commonOpts('USD'), {
        scales: Object.assign({}, commonOpts('USD').scales, {x: Object.assign({}, commonOpts('USD').scales.x, {stacked: true}), y: Object.assign({}, commonOpts('USD').scales.y, {stacked: true})}),
      }),
    });
  }

  function renderCashTrend(rows) {
    const ctx = document.getElementById('dd-chart-cash').getContext('2d');
    if (!rows.length) return;
    charts.cash = new Chart(ctx, {
      type: 'line',
      data: {
        labels: rows.map(r => r.period),
        datasets: [{
          label: 'Closing Cash',
          data: rows.map(r => r.closing_cash),
          borderColor: '#00ff9d',
          backgroundColor: '#00ff9d22',
          tension: 0.35,
          fill: true,
        }],
      },
      options: commonOpts('USD'),
    });
  }

  function renderSegments(rows) {
    const ctx = document.getElementById('dd-chart-seg').getContext('2d');
    if (!rows.length) return;
    charts.seg = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: rows.map(r => r.label),
        datasets: [
          {label: 'Revenue', data: rows.map(r => r.revenue), backgroundColor: '#00d4ff99', borderColor: '#00d4ff', borderWidth: 1},
          {label: 'Gross Margin', data: rows.map(r => r.gross_margin), backgroundColor: '#00ff9d99', borderColor: '#00ff9d', borderWidth: 1},
        ],
      },
      options: commonOpts('USD'),
    });
  }

  function renderGeo(rows) {
    const ctx = document.getElementById('dd-chart-geo').getContext('2d');
    if (!rows.length) {
      ctx.canvas.parentElement.querySelector('.chart-title').textContent = 'Geography — no data';
      return;
    }
    charts.geo = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: rows.map(r => r.label),
        datasets: [{
          data: rows.map(r => r.revenue),
          backgroundColor: ['#00d4ff99','#00ff9d99','#ffb80099','#a855f799','#ff445599','#06ffd699','#8888aa99'],
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: {labels: {color: '#8888aa'}},
          tooltip: {callbacks: {label: c => `${c.label}: ${window.Portfolio.formatUSD(c.raw)}`}},
        },
      },
    });
  }

  function commonOpts(yFmt) {
    return {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: {labels: {color: '#8888aa'}},
        tooltip: {
          backgroundColor: '#0b0b20',
          callbacks: {label: c => `${c.dataset.label}: ${window.Portfolio.formatNum(c.raw, yFmt)}`},
        },
      },
      scales: {
        x: {ticks: {color: '#8888aa'}, grid: {color: 'rgba(255,255,255,0.03)'}},
        y: {
          ticks: {color: '#8888aa', callback: v => window.Portfolio.formatNum(v, yFmt)},
          grid: {color: 'rgba(255,255,255,0.05)'},
        },
      },
    };
  }

  window.DeepDive = {render};
})();
