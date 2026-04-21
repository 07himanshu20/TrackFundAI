/* ============================================================
   compare.js
   Renders the comparison panel (chart + table + mode tabs).
   Window API: window.ComparePanel.render(children, parentNode)
============================================================ */

(() => {
  let chartInstance = null;
  let currentMode = 'actual';
  let currentMetric = 'revenue';
  let currentChildren = [];
  let currentParent = null;

  // KPI-table state
  let kpiSubmode = 'latest';    // 'latest' | 'as_of' | 'range'
  let kpiAsOf = '';             // 'YYYY-MM'
  let kpiRangeFrom = '';
  let kpiRangeTo = '';
  let kpiTranspose = false;

  // Table state (for sorting)
  let lastTable = null;         // {columns, rows}
  let sortColIdx = null;
  let sortDir = 'asc';          // 'asc' | 'desc'

  const METRIC_OPTIONS = {
    actual: [
      {v: 'revenue',       t: 'Revenue'},
      {v: 'gross_profit',  t: 'Gross Profit'},
      {v: 'ebitda',        t: 'EBITDA'},
      {v: 'opex',          t: 'OPEX'},
      {v: 'gp_pct',        t: 'GP %'},
      {v: 'ebitda_pct',    t: 'EBITDA %'},
      {v: 'ytd_revenue',   t: 'YTD Revenue'},
      {v: 'ytd_ebitda',    t: 'YTD EBITDA'},
    ],
    sales_margin: [
      {v: 'composite', t: 'Revenue + GP + EBITDA (fixed)'},
    ],
    variance: [
      {v: 'revenue', t: 'Revenue'},
      {v: 'ebitda',  t: 'EBITDA'},
    ],
    kpi_table: [],   // no metric picker in kpi_table mode
  };

  function init() {
    // Top-level mode tabs
    document.querySelectorAll('.mode-tab').forEach(tab => {
      tab.onclick = () => {
        document.querySelectorAll('.mode-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        currentMode = tab.dataset.mode;
        updateModeVisibility();
        rebuildMetricDropdown();
        sortColIdx = null;
        refresh();
      };
    });

    // Metric dropdown
    const metricSel = document.getElementById('metric-select');
    if (metricSel) metricSel.onchange = (e) => {
      currentMetric = e.target.value;
      refresh();
    };

    // KPI sub-mode tabs (latest / as_of / range)
    document.querySelectorAll('.kpi-submode-tab').forEach(tab => {
      tab.onclick = () => {
        document.querySelectorAll('.kpi-submode-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        kpiSubmode = tab.dataset.submode;
        updateKpiInputsVisibility();
        // For 'latest' we refresh immediately; for the others we wait until
        // the user provides a month (as_of) or clicks Apply (range).
        if (kpiSubmode === 'latest') {
          sortColIdx = null;
          refresh();
        } else if (kpiSubmode === 'as_of' && kpiAsOf) {
          sortColIdx = null;
          refresh();
        }
      };
    });

    // As-of month change -> refresh immediately
    const asOfInput = document.getElementById('kpi-as-of');
    if (asOfInput) asOfInput.onchange = (e) => {
      kpiAsOf = e.target.value || '';
      if (kpiSubmode === 'as_of') {
        sortColIdx = null;
        refresh();
      }
    };

    // Range inputs — commit on Apply button (so we don't fire mid-typing)
    const rangeFromInput = document.getElementById('kpi-range-from');
    const rangeToInput = document.getElementById('kpi-range-to');
    const applyBtn = document.getElementById('btn-apply-range');
    if (rangeFromInput) rangeFromInput.onchange = (e) => { kpiRangeFrom = e.target.value || ''; };
    if (rangeToInput)   rangeToInput.onchange   = (e) => { kpiRangeTo   = e.target.value || ''; };
    if (applyBtn) applyBtn.onclick = () => {
      if (kpiSubmode === 'range' && kpiRangeFrom && kpiRangeTo) {
        sortColIdx = null;
        refresh();
      }
    };

    // Transpose toggle
    const transposeEl = document.getElementById('kpi-transpose');
    if (transposeEl) transposeEl.onchange = (e) => {
      kpiTranspose = e.target.checked;
      if (lastTable) drawTable(lastTable);
    };

    // CSV export
    const exp = document.getElementById('btn-export-compare');
    if (exp) exp.onclick = exportCSV;
  }

  function updateModeVisibility() {
    const isKpi = currentMode === 'kpi_table';
    const dateCtrls = document.getElementById('kpi-date-controls');
    const metricPicker = document.getElementById('metric-picker');
    if (dateCtrls)   dateCtrls.style.display   = isKpi ? 'flex' : 'none';
    if (metricPicker) metricPicker.style.visibility = isKpi ? 'hidden' : 'visible';
  }

  function updateKpiInputsVisibility() {
    const asofBox  = document.getElementById('kpi-date-inputs-asof');
    const rangeBox = document.getElementById('kpi-date-inputs-range');
    if (asofBox)  asofBox.style.display  = (kpiSubmode === 'as_of') ? 'flex' : 'none';
    if (rangeBox) rangeBox.style.display = (kpiSubmode === 'range') ? 'flex' : 'none';
  }

  function rebuildMetricDropdown() {
    const sel = document.getElementById('metric-select');
    const picker = document.getElementById('metric-picker');
    const opts = METRIC_OPTIONS[currentMode] || [];
    sel.innerHTML = '';
    opts.forEach(o => {
      const el = document.createElement('option');
      el.value = o.v;
      el.textContent = o.t;
      sel.appendChild(el);
    });
    if (currentMode === 'kpi_table') {
      picker.style.visibility = 'hidden';
    } else if (opts.length <= 1) {
      picker.style.visibility = 'hidden';
    } else {
      picker.style.visibility = 'visible';
    }
    if (!opts.find(o => o.v === currentMetric)) {
      currentMetric = opts[0]?.v || 'revenue';
    }
    sel.value = currentMetric;
  }

  function render(children, parent) {
    currentChildren = children;
    currentParent = parent;

    // Update titles
    const tag = document.getElementById('compare-tag');
    const title = document.getElementById('compare-title');
    const sub = document.getElementById('compare-subtitle');
    const childLabel = {
      portfolio: 'funds', fund: 'sectors', sector: 'segments', segment: 'companies',
    }[parent.level] || 'children';
    tag.textContent = `Compare ${childLabel}`;
    title.textContent = `${childLabel[0].toUpperCase() + childLabel.slice(1)} Comparison`;
    sub.textContent = `${children.length} ${childLabel} side-by-side — switch mode & metric below.`;

    updateModeVisibility();
    updateKpiInputsVisibility();
    rebuildMetricDropdown();
    sortColIdx = null;
    refresh();
  }

  async function refresh() {
    if (!currentChildren || currentChildren.length < 1) return;
    const ids = currentChildren.map(c => c.id).join(',');
    const params = new URLSearchParams({ids, mode: currentMode, metric: currentMetric});

    if (currentMode === 'kpi_table') {
      if (kpiSubmode === 'as_of' && kpiAsOf)        params.set('as_of', kpiAsOf);
      if (kpiSubmode === 'range' && kpiRangeFrom && kpiRangeTo) {
        params.set('range_from', kpiRangeFrom);
        params.set('range_to', kpiRangeTo);
      }
    }

    const url = `/portfolio/compare/?${params.toString()}`;
    try {
      const payload = await window.Portfolio.apiGet(url);
      drawChart(payload.chart);
      lastTable = payload.table;
      drawTable(lastTable);
      document.getElementById('compare-chart-title').textContent = payload.chart.title;
      document.getElementById('compare-table-title').textContent =
        payload.sub_label ? `${payload.mode_label} — ${payload.sub_label}` : payload.mode_label;
    } catch (e) {
      console.error('compare refresh failed', e);
    }
  }

  function drawChart(chart) {
    const ctx = document.getElementById('compare-chart').getContext('2d');
    if (chartInstance) chartInstance.destroy();

    const palette = ['#00d4ff','#00ff9d','#ffb800','#a855f7','#ff4455','#06ffd6'];
    const datasets = chart.datasets.map((ds, i) => ({
      label: ds.label,
      data: ds.data,
      backgroundColor: chart.type === 'bar' ? palette[i % palette.length] + '99' : palette[i % palette.length] + '33',
      borderColor: palette[i % palette.length],
      borderWidth: 2,
      borderRadius: 4,
      tension: 0.3,
    }));

    const yFmt = chart.yFormat;
    chartInstance = new Chart(ctx, {
      type: chart.type === 'line' ? 'line' : 'bar',
      data: {labels: chart.labels, datasets},
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {labels: {color: '#8888aa', font: {family: 'Space Grotesk', size: 12}}},
          tooltip: {
            backgroundColor: '#0b0b20',
            borderColor: '#00d4ff33',
            borderWidth: 1,
            callbacks: {
              label: (ctx) => `${ctx.dataset.label}: ${window.Portfolio.formatNum(ctx.raw, yFmt)}`,
            },
          },
        },
        scales: {
          x: {
            ticks: {color: '#8888aa', font: {size: 11}},
            grid: {color: 'rgba(255,255,255,0.03)'},
          },
          y: {
            ticks: {
              color: '#8888aa',
              font: {size: 11},
              callback: (v) => window.Portfolio.formatNum(v, yFmt),
            },
            grid: {color: 'rgba(255,255,255,0.05)'},
          },
        },
      },
    });
  }

  // Format a single cell based on the column name. Returns {text, cls}.
  function formatCell(colName, v) {
    const n = (colName || '').toLowerCase();
    if (v === null || v === undefined || v === '') return {text: '—', cls: ''};
    if (typeof v !== 'number') return {text: String(v), cls: ''};
    const isPct = n.includes('%') || n.includes('percent') || n.includes('yoy') || n.includes('var');
    const text = window.Portfolio.formatNum(v, isPct ? 'percent' : 'USD');
    let cls = '';
    if (n.includes('variance') || n.includes('yoy') || n.includes('var')) {
      cls = v >= 0 ? 'pos' : 'neg';
    }
    return {text, cls};
  }

  function sortedRows(columns, rows) {
    if (sortColIdx === null) return rows;
    const idx = sortColIdx;
    const copy = rows.slice();
    copy.sort((a, b) => {
      const va = a[idx], vb = b[idx];
      const aN = (va === null || va === undefined || va === '') ? null :
                 (typeof va === 'number' ? va : Number(va));
      const bN = (vb === null || vb === undefined || vb === '') ? null :
                 (typeof vb === 'number' ? vb : Number(vb));
      // Push nulls to bottom regardless of direction
      if (aN === null && bN === null) return 0;
      if (aN === null) return 1;
      if (bN === null) return -1;
      if (!isNaN(aN) && !isNaN(bN)) {
        return sortDir === 'asc' ? aN - bN : bN - aN;
      }
      // String compare
      return sortDir === 'asc'
        ? String(va).localeCompare(String(vb))
        : String(vb).localeCompare(String(va));
    });
    return copy;
  }

  function drawTable(table) {
    const thead = document.getElementById('compare-thead');
    const tbody = document.getElementById('compare-tbody');
    thead.innerHTML = '';
    tbody.innerHTML = '';
    if (!table || !table.columns) return;

    const columns = table.columns;
    const rows = sortedRows(columns, table.rows || []);

    if (kpiTranspose && currentMode === 'kpi_table') {
      renderTransposed(columns, rows);
      return;
    }

    // Header
    columns.forEach((c, i) => {
      const th = document.createElement('th');
      const label = document.createElement('span');
      label.textContent = c;
      th.appendChild(label);
      if (i > 0) th.className = 'num sortable';
      else th.className = 'sortable';
      const arrow = document.createElement('span');
      arrow.className = 'sort-arrow';
      arrow.textContent = '▼';
      th.appendChild(arrow);
      if (i === sortColIdx) th.classList.add(sortDir === 'asc' ? 'sort-asc' : 'sort-desc');
      th.onclick = () => {
        if (sortColIdx === i) {
          sortDir = sortDir === 'asc' ? 'desc' : 'asc';
        } else {
          sortColIdx = i;
          sortDir = (i === 0) ? 'asc' : 'desc';
        }
        drawTable(table);
      };
      thead.appendChild(th);
    });

    // Body
    rows.forEach(r => {
      const tr = document.createElement('tr');
      r.forEach((v, idx) => {
        const td = document.createElement('td');
        if (idx === 0) {
          td.textContent = v ?? '—';
        } else {
          const {text, cls} = formatCell(columns[idx], v);
          td.className = 'num ' + cls;
          td.textContent = text;
        }
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
  }

  // Transposed rendering: columns become row-headers, each entity becomes a column.
  function renderTransposed(columns, rows) {
    const thead = document.getElementById('compare-thead');
    const tbody = document.getElementById('compare-tbody');

    // New header: first col = 'KPI', then one col per entity (row[0])
    const entityNames = rows.map(r => r[0] ?? '—');
    const newHeader = ['KPI', ...entityNames];
    newHeader.forEach((c, i) => {
      const th = document.createElement('th');
      th.textContent = c;
      if (i > 0) th.className = 'num';
      thead.appendChild(th);
    });

    // One row per KPI column (skipping col 0 which was 'Entity')
    for (let c = 1; c < columns.length; c++) {
      const tr = document.createElement('tr');
      const head = document.createElement('td');
      head.textContent = columns[c];
      tr.appendChild(head);
      rows.forEach(r => {
        const td = document.createElement('td');
        const {text, cls} = formatCell(columns[c], r[c]);
        td.className = 'num ' + cls;
        td.textContent = text;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    }
  }

  function exportCSV() {
    if (!lastTable) return;
    const cols = lastTable.columns || [];
    const rows = sortedRows(cols, lastTable.rows || []);
    const esc = (v) => {
      if (v === null || v === undefined) return '';
      const s = String(v);
      return /[,"\n]/.test(s) ? `"${s.replace(/"/g,'""')}"` : s;
    };
    const out = [cols.map(esc).join(',')];
    rows.forEach(r => out.push(r.map(esc).join(',')));
    const csv = out.join('\n');
    const blob = new Blob([csv], {type: 'text/csv'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    const suffix = currentMode === 'kpi_table'
      ? `kpi_${kpiSubmode}${kpiAsOf ? '_' + kpiAsOf : ''}${kpiRangeFrom ? '_' + kpiRangeFrom + '_' + kpiRangeTo : ''}`
      : `${currentMode}_${currentMetric}`;
    a.download = `compare_${suffix}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  // Initialise listeners once DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.ComparePanel = {render};
})();
