/* ============================================================
   portfolio.js
   Core state manager + routing for the hierarchical dashboard.
   Exposes window.Portfolio with:
     - init()
     - navigate(nodeId|null)    // null = portfolio root
     - state                     // current node + children
     - formatUSD(v), formatPct(v)
============================================================ */

(() => {
  // In dev, the frontend is served on a static port (5500/5501/8765/etc.)
  // while Django serves the API on :8000. In production behind a reverse
  // proxy both share the same origin, so only :8000 itself (or no port)
  // should use the relative /api path.
  const _port = window.location.port;
  const _sameOriginAsApi = (_port === '8000' || _port === '' || _port === '80' || _port === '443');
  const API_BASE = _sameOriginAsApi ? '/api' : 'http://127.0.0.1:8000/api';

  const state = {
    meta: null,        // {schema_version, fx_rates, fx_as_of, period_range, base_currency}
    currentId: null,   // null when at portfolio root
    currentNode: null, // fetched node or root-synthetic
    children: [],      // direct children
    ancestors: [],     // breadcrumb
  };

  // ── Formatting helpers ────────────────────────────────────
  const formatUSD = (v) => {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    const abs = Math.abs(v);
    const sign = v < 0 ? '-' : '';
    if (abs >= 1e9)  return `${sign}$${(abs / 1e9).toFixed(2)}B`;
    if (abs >= 1e6)  return `${sign}$${(abs / 1e6).toFixed(2)}M`;
    if (abs >= 1e3)  return `${sign}$${(abs / 1e3).toFixed(1)}K`;
    return `${sign}$${abs.toFixed(0)}`;
  };
  const formatPct = (v) => (v === null || v === undefined) ? '—' : `${Number(v).toFixed(1)}%`;
  const formatNum = (v, fmt) => {
    if (fmt === 'percent') return formatPct(v);
    if (fmt === 'USD')     return formatUSD(v);
    if (v === null || v === undefined) return '—';
    return Number(v).toLocaleString();
  };

  // ── Fetch wrappers ────────────────────────────────────────
  async function apiGet(path) {
    const r = await fetch(`${API_BASE}${path}`);
    if (!r.ok) throw new Error(`API ${path} → ${r.status}`);
    return r.json();
  }
  async function apiPost(path, body) {
    const r = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`API ${path} → ${r.status}`);
    return r.json();
  }

  // ── Navigation ────────────────────────────────────────────
  async function navigate(nodeId) {
    state.currentId = nodeId;

    if (!nodeId) {
      // Portfolio root — use the funds list as "children"
      state.currentNode = {
        id: null,
        name: 'TrackFundAI Portfolio',
        level: 'portfolio',
        is_real: true,
        description: `${state.meta.period_range.start} → ${state.meta.period_range.end} · Base currency ${state.meta.base_currency}`,
        financials: null,
      };
      state.children = state.meta.funds;
      state.ancestors = [];
    } else {
      const data = await apiGet(`/portfolio/node/${encodeURIComponent(nodeId)}/`);
      state.currentNode = data;
      state.children = data.children || [];
      state.ancestors = data.ancestors || [];
    }

    // Update URL hash so refresh preserves location
    if (nodeId) {
      history.replaceState(null, '', `#${nodeId}`);
    } else {
      history.replaceState(null, '', window.location.pathname);
    }

    render();
  }

  // ── Render ────────────────────────────────────────────────
  function render() {
    renderBreadcrumb();
    renderHero();

    const isLeaf = state.currentNode.level === 'company';
    const hasChildren = state.children.length > 0;

    const compareSec = document.getElementById('section-compare');
    const childrenSec = document.getElementById('section-children');
    const deepdiveSec = document.getElementById('section-deepdive');

    // Comparison panel: only show when we have 2+ children to compare
    if (hasChildren && state.children.length >= 2) {
      compareSec.classList.remove('hidden');
      if (window.ComparePanel) window.ComparePanel.render(state.children, state.currentNode);
    } else {
      compareSec.classList.add('hidden');
    }

    // Children grid: show unless leaf
    if (hasChildren) {
      childrenSec.classList.remove('hidden');
      renderChildrenGrid();
    } else {
      childrenSec.classList.add('hidden');
    }

    // Deep-dive: only at leaf
    if (isLeaf) {
      deepdiveSec.classList.remove('hidden');
      if (window.DeepDive) window.DeepDive.render(state.currentNode);
    } else {
      deepdiveSec.classList.add('hidden');
    }

    // Chatbot scope
    if (window.Chatbot) window.Chatbot.setScope(state.currentNode);

    // Scroll to top
    window.scrollTo({top: 0, behavior: 'smooth'});
  }

  function renderBreadcrumb() {
    const el = document.getElementById('breadcrumb');
    el.innerHTML = '';

    const addCrumb = (label, id, isCurrent) => {
      const span = document.createElement('span');
      span.className = 'crumb' + (isCurrent ? ' current' : '');
      span.textContent = label;
      if (!isCurrent) span.onclick = () => navigate(id);
      el.appendChild(span);
    };
    const addSep = () => {
      const s = document.createElement('span');
      s.className = 'crumb-sep';
      s.textContent = '›';
      el.appendChild(s);
    };

    addCrumb('Portfolio', null, !state.currentId);

    (state.ancestors || []).forEach((a, idx) => {
      addSep();
      const isLast = idx === state.ancestors.length - 1;
      addCrumb(a.name, a.id, isLast);
    });
  }

  function renderHero() {
    const node = state.currentNode;
    const eyebrow = document.getElementById('hero-eyebrow');
    const titleMain = document.getElementById('hero-title-main');
    const titleSub = document.getElementById('hero-title-sub');
    const desc = document.getElementById('hero-description');
    const stats = document.getElementById('hero-stats');

    eyebrow.textContent = `LEVEL · ${(node.level || 'portfolio').toUpperCase()}`;

    // Split name into two halves for stylisation
    const parts = node.name.split(' ');
    if (parts.length >= 2) {
      titleMain.textContent = parts[0];
      titleSub.textContent  = parts.slice(1).join(' ');
    } else {
      titleMain.textContent = node.name;
      titleSub.textContent  = '';
    }

    desc.textContent = node.description || '';

    // Hero stats
    stats.innerHTML = '';
    const s = (node.financials && node.financials.summary) || {};
    const chips = [];
    if (node.level === 'portfolio') {
      const totalRev = state.meta.funds.reduce((a,f) => a + (f.financials?.summary?.revenue || 0), 0);
      const totalEbitda = state.meta.funds.reduce((a,f) => a + (f.financials?.summary?.ebitda || 0), 0);
      chips.push(['Funds', state.meta.funds.length]);
      chips.push(['Revenue (USD)', formatUSD(totalRev)]);
      chips.push(['EBITDA (USD)', formatUSD(totalEbitda)]);
      chips.push(['As of', state.meta.fx_as_of]);
    } else {
      if (s.revenue !== undefined)       chips.push(['Revenue', formatUSD(s.revenue)]);
      if (s.gross_profit !== undefined)  chips.push(['Gross Profit', formatUSD(s.gross_profit)]);
      if (s.ebitda !== undefined)        chips.push(['EBITDA', formatUSD(s.ebitda)]);
      if (s.gp_pct !== undefined)        chips.push(['GP %', formatPct(s.gp_pct)]);
      if (s.ebitda_pct !== undefined)    chips.push(['EBITDA %', formatPct(s.ebitda_pct)]);
      chips.push(['Currency', node.currency || 'USD']);
    }
    chips.forEach(([label, value]) => {
      const div = document.createElement('div');
      div.className = 'hero-stat';
      div.innerHTML = `<span class="hero-stat-label">${label}</span><span class="hero-stat-value mono">${value}</span>`;
      stats.appendChild(div);
    });
  }

  function renderChildrenGrid() {
    const grid = document.getElementById('children-grid');
    const childrenTitle = document.getElementById('children-title');
    const childrenTag   = document.getElementById('children-tag');
    const childrenSub   = document.getElementById('children-subtitle');

    const level = state.currentNode.level;
    const childLabelMap = {
      portfolio: ['Funds', 'Click a fund to drill into its sectors'],
      fund:      ['Sectors', 'Click a sector to see segments'],
      sector:    ['Segments', 'Click a segment to see portfolio companies'],
      segment:   ['Portfolio Companies', 'Click a company for a full deep-dive'],
    };
    const [label, sub] = childLabelMap[level] || ['Children', 'Click to drill in'];
    childrenTag.textContent = label;
    childrenTitle.textContent = label;
    childrenSub.textContent = sub;

    grid.innerHTML = '';

    state.children.forEach(c => {
      const card = document.createElement('div');
      card.className = 'child-card';
      const s = (c.financials && c.financials.summary) || {};
      const isReal = !!c.is_real;

      const rev = s.revenue ?? s.ytd_revenue;
      const ebitda = s.ebitda ?? s.ytd_ebitda;

      card.innerHTML = `
        <div class="child-head">
          <div>
            <div class="child-name">${escapeHtml(c.name)}</div>
            <div class="child-level">${(c.level || '').toUpperCase()}</div>
          </div>
          <span class="child-badge ${isReal ? 'real' : 'mock'}">${isReal ? 'REAL' : 'MOCK'}</span>
        </div>
        <div class="child-metrics">
          <div class="cm">
            <span class="cm-label">Revenue</span>
            <span class="cm-value">${formatUSD(rev)}</span>
          </div>
          <div class="cm">
            <span class="cm-label">EBITDA</span>
            <span class="cm-value ${ebitda >= 0 ? 'positive' : 'negative'}">${formatUSD(ebitda)}</span>
          </div>
          <div class="cm">
            <span class="cm-label">GP %</span>
            <span class="cm-value">${formatPct(s.gp_pct)}</span>
          </div>
          <div class="cm">
            <span class="cm-label">${c.level === 'company' ? 'Currency' : 'Children'}</span>
            <span class="cm-value">${c.level === 'company' ? (c.currency || '—') : (c.child_count ?? '—')}</span>
          </div>
        </div>
        <div class="child-chevron">›</div>
      `;
      card.onclick = () => navigate(c.id);
      grid.appendChild(card);
    });
  }

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, m => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[m]));
  }

  // ── Init ──────────────────────────────────────────────────
  async function init() {
    try {
      const meta = await apiGet('/portfolio/');
      state.meta = meta;
      document.getElementById('fx-badge').textContent = `FX · ${meta.fx_as_of}`;

      // Attach home button
      document.getElementById('btn-home').onclick = () => navigate(null);

      // Hash routing — only treat hashes that look like node ids (start with "fund_")
      // as route targets. Anything else (e.g. legacy "#section-segments" anchors) is
      // ignored and we land on the portfolio root.
      const isNodeHash = (h) => typeof h === 'string' && h.startsWith('fund_');
      const readHash = () => {
        const raw = window.location.hash ? window.location.hash.slice(1) : '';
        return isNodeHash(raw) ? raw : null;
      };
      await navigate(readHash());

      window.addEventListener('hashchange', () => {
        const id = readHash();
        if (id !== state.currentId) navigate(id);
      });

      // Hide loading
      const ls = document.getElementById('loading-screen');
      if (ls) ls.style.opacity = '0';
      setTimeout(() => ls && (ls.style.display = 'none'), 400);
    } catch (e) {
      console.error('Portfolio init failed:', e);
      alert('Failed to load portfolio: ' + e.message);
    }
  }

  window.Portfolio = {
    init, navigate, state, apiGet, apiPost,
    formatUSD, formatPct, formatNum,
  };
})();
