"""
Phase 3 Priority Matrix — Python encoding of docs/priority_matrix.md.

Universal across all AIF workbook formats. Each field declares:
  principles : list of P1..P7 codes that drive its ranking
  sources    : ordered list of (path, description) tuples — first non-null wins
  tolerance  : (pct, abs) tuple used to detect cross-source disagreements
  reason     : human-readable why-this-ranking string
  compute    : optional callable(merged) → value, used when source path '__compute__'

`path` semantics:
    ('block_name', 'field_name')                  → merged[block_name][field_name]
    ('block_name', '__sum__', 'field_name')       → sum over all dicts in merged[block_name]
    ('block_name', '__latest_by__', date_key, value_key)
                                                  → value_key of the row with MAX(date_key)
    ('__compute__', formula_name)                 → COMPUTED[formula_name](merged)
"""

from decimal import Decimal
from typing import Callable

# ── Universal principles ─────────────────────────────────────────────────────
PRINCIPLES = {
    'P1': 'Dedicated source > Summary source',
    'P2': 'Time-series row > Snapshot cell',
    'P3': 'Row sum > Aggregate cell',
    'P4': 'Audited > Stated > Projected',
    'P5': 'Identifier match > Label match',
    'P6': 'Latest period > Historical period',
    'P7': 'Component identity > Summary identity',
}


# ── Path resolver ────────────────────────────────────────────────────────────

def resolve_path(merged: dict, path: tuple):
    """Walk `path` through `merged` JSON. Returns value or None."""
    if not path:
        return None
    if path[0] == '__compute__':
        fn = COMPUTED.get(path[1])
        return fn(merged) if fn else None
    cur = merged
    i = 0
    while i < len(path):
        step = path[i]
        if not isinstance(cur, (dict, list)):
            return None
        if step == '__sum__':
            field = path[i + 1]
            total = Decimal('0')
            any_seen = False
            for item in cur if isinstance(cur, list) else []:
                if isinstance(item, dict) and item.get(field) not in (None, ''):
                    try:
                        total += Decimal(str(item[field]))
                        any_seen = True
                    except Exception:
                        pass
            return total if any_seen else None
        if step == '__latest_by__':
            date_key = path[i + 1]
            value_key = path[i + 2]
            best_date = None
            best_value = None
            for item in cur if isinstance(cur, list) else []:
                if not isinstance(item, dict):
                    continue
                d = item.get(date_key)
                v = item.get(value_key)
                if d and v not in (None, ''):
                    if best_date is None or str(d) > str(best_date):
                        best_date = d
                        best_value = v
            return best_value
        if isinstance(cur, dict):
            cur = cur.get(step)
            i += 1
        else:
            return None
    return cur if cur not in (None, '') else None


# ── Computed formulas (universal mathematical identities) ────────────────────

def _to_dec(v):
    if v is None or v == '':
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _sum_field(merged, block, field):
    rows = merged.get(block) or []
    if not isinstance(rows, list):
        return None
    total = Decimal('0')
    any_seen = False
    for r in rows:
        if isinstance(r, dict):
            v = _to_dec(r.get(field))
            if v is not None:
                total += v
                any_seen = True
    return total if any_seen else None


def _sum_commitments(merged):
    return _sum_field(merged, 'commitments', 'commitment_amount')


def _sum_called(merged):
    return _sum_field(merged, 'capital_calls', 'total_call_amount') \
        or _sum_field(merged, 'capital_calls', 'called_amount')


def _sum_distributions(merged):
    return _sum_field(merged, 'distributions', 'total_net_amount') \
        or _sum_field(merged, 'distributions', 'net_amount')


def _sum_capital_distributions(merged):
    """ILPA-aligned: capital types only (return_of_capital, stcg, ltcg)."""
    rows = merged.get('distributions') or []
    total = Decimal('0')
    any_seen = False
    for r in rows:
        if not isinstance(r, dict):
            continue
        dt = (r.get('distribution_type') or '').lower()
        if dt not in ('return_of_capital', 'stcg', 'ltcg'):
            continue
        v = _to_dec(r.get('total_net_amount') or r.get('net_amount'))
        if v is not None:
            total += v
            any_seen = True
    return total if any_seen else None


def _sum_active_fv(merged):
    rows = merged.get('valuations') or []
    total = Decimal('0')
    any_seen = False
    for r in rows:
        if not isinstance(r, dict):
            continue
        v = _to_dec(r.get('fair_value_of_holding') or r.get('fv_holding'))
        if v is not None:
            total += v
            any_seen = True
    return total if any_seen else None


def _sum_active_cost(merged):
    return _sum_field(merged, 'portfolio_investments', 'total_invested')


def _uncalled_capital(merged):
    c = _to_dec(resolve_path(merged, ('__compute__', 'sum_commitments')))
    k = _to_dec(resolve_path(merged, ('__compute__', 'sum_called')))
    return (c - k) if (c is not None and k is not None) else None


def _drawdown_pct(merged):
    c = _to_dec(resolve_path(merged, ('__compute__', 'sum_commitments')))
    k = _to_dec(resolve_path(merged, ('__compute__', 'sum_called')))
    if c and c > 0 and k is not None:
        return (k / c) * Decimal('100')
    return None


def _tvpi_canonical(merged):
    """(Distributions + NAV) ÷ Called."""
    dist = _to_dec(resolve_path(merged, ('__compute__', 'sum_distributions'))) \
        or _to_dec(resolve_path(merged, ('fund_performance', 'total_distributions')))
    nav = _to_dec(resolve_path(merged, ('fund_performance', 'fund_nav_latest'))) \
        or _to_dec(resolve_path(merged, ('nav_records', '__latest_by__', 'nav_date', 'total_nav')))
    called = _to_dec(resolve_path(merged, ('__compute__', 'sum_called'))) \
        or _to_dec(resolve_path(merged, ('fund_performance', 'total_called_capital')))
    if called and called > 0 and dist is not None and nav is not None:
        return (dist + nav) / called
    return None


def _dpi_canonical(merged):
    """Capital distributions ÷ Called (ILPA-aligned)."""
    dist = _to_dec(_sum_capital_distributions(merged)) \
        or _to_dec(resolve_path(merged, ('fund_performance', 'total_distributions')))
    called = _to_dec(resolve_path(merged, ('__compute__', 'sum_called'))) \
        or _to_dec(resolve_path(merged, ('fund_performance', 'total_called_capital')))
    if called and called > 0 and dist is not None:
        return dist / called
    return None


def _rvpi_canonical(merged):
    """NAV ÷ Called."""
    nav = _to_dec(resolve_path(merged, ('fund_performance', 'fund_nav_latest'))) \
        or _to_dec(resolve_path(merged, ('nav_records', '__latest_by__', 'nav_date', 'total_nav')))
    called = _to_dec(resolve_path(merged, ('__compute__', 'sum_called'))) \
        or _to_dec(resolve_path(merged, ('fund_performance', 'total_called_capital')))
    if called and called > 0 and nav is not None:
        return nav / called
    return None


def _moic_canonical(merged):
    """Active FV ÷ Invested Cost (universal).

    Falls back to fund_performance summary cells when valuations[] or
    portfolio_investments[] are empty — guarantees MOIC computes whenever
    EITHER the row-level data OR the summary cell is present.
    """
    fv = _to_dec(_sum_active_fv(merged)) \
        or _to_dec(resolve_path(merged, ('fund_performance', 'total_unrealised_fv_holding')))
    cost = _to_dec(_sum_active_cost(merged)) \
        or _to_dec(resolve_path(merged, ('fund_performance', 'total_invested_capital')))
    if cost and cost > 0 and fv is not None:
        return fv / cost
    return None


def _unrealised_gain(merged):
    fv = _to_dec(_sum_active_fv(merged)) \
        or _to_dec(resolve_path(merged, ('fund_performance', 'total_unrealised_fv_holding')))
    cost = _to_dec(_sum_active_cost(merged)) \
        or _to_dec(resolve_path(merged, ('fund_performance', 'total_invested_capital')))
    return (fv - cost) if (fv is not None and cost is not None) else None


COMPUTED: dict[str, Callable[[dict], object]] = {
    'sum_commitments': _sum_commitments,
    'sum_called': _sum_called,
    'sum_distributions': _sum_distributions,
    'sum_capital_distributions': _sum_capital_distributions,
    'sum_active_fv': _sum_active_fv,
    'sum_active_cost': _sum_active_cost,
    'uncalled_capital': _uncalled_capital,
    'drawdown_pct': _drawdown_pct,
    'tvpi_canonical': _tvpi_canonical,
    'dpi_canonical': _dpi_canonical,
    'rvpi_canonical': _rvpi_canonical,
    'moic_canonical': _moic_canonical,
    'unrealised_gain': _unrealised_gain,
}


# ── Tolerance bands by metric type (universal defaults) ──────────────────────

TOL_CURRENCY = {'pct': Decimal('1.0'), 'abs': Decimal('0.10')}   # ±1% or ±₹0.10 Cr
TOL_PERCENT  = {'pct': None,           'abs': Decimal('0.5')}    # ±0.5 pp
TOL_MULTIPLE = {'pct': None,           'abs': Decimal('0.05')}   # ±0.05x
TOL_COUNT    = {'pct': None,           'abs': Decimal('0')}      # exact
TOL_DATE     = {'pct': None,           'abs': None}              # exact string match


# ── FIELD_PRIORITIES — universal priority rules for every reconciled field ──
# Maps canonical_field_name → {principles, sources, tolerance, reason, kind}.
# Reconciler iterates this map; the persister already knows how to write
# the picked value into the right DB model via existing block-key conventions.

FIELD_PRIORITIES: dict[str, dict] = {

    # ── A. Identity & Master ─────────────────────────────────────────────────
    'fund_name': {
        'principles': ['P1'],
        'sources': [
            (('fund_master', 'fund_name'), 'Layer 1 fund_master.fund_name'),
            (('fund_master', 'scheme_name'), 'Layer 1 fund_master.scheme_name'),
        ],
        'tolerance': TOL_DATE,
        'reason': 'SEBI Registration certificate is the legal source; appears verbatim on Cover.',
        'kind': 'text',
    },
    'sebi_registration_number': {
        'principles': ['P1', 'P5'],
        'sources': [(('fund_master', 'sebi_registration_number'), 'Layer 1 fund_master')],
        'tolerance': TOL_DATE,
        'reason': 'SEBI Reg No is the identifier — universal format IN/AIF[123]/YY-YY/NNNNNNN.',
        'kind': 'text',
    },
    'vintage_year': {
        'principles': ['P1', 'P6'],
        'sources': [
            (('fund_master', 'vintage_year'), 'Layer 1 fund_master.vintage_year'),
        ],
        'tolerance': TOL_COUNT,
        'reason': 'Year of first close OR inception per SEBI definition.',
        'kind': 'int',
    },

    # ── C. Capital ───────────────────────────────────────────────────────────
    'total_committed_capital': {
        'principles': ['P3', 'P1'],
        'sources': [
            (('__compute__', 'sum_commitments'), 'Sum of LP Commitment ledger rows'),
            (('fund_performance', 'total_committed_capital'), 'fund_performance summary cell'),
        ],
        'tolerance': TOL_CURRENCY,
        'reason': 'LP commitment ledger is the legal record; summary cells are hand roll-ups.',
        'kind': 'currency',
    },
    'total_called_capital': {
        'principles': ['P3', 'P1'],
        'sources': [
            (('__compute__', 'sum_called'), 'Sum of capital_calls ledger'),
            (('fund_performance', 'total_called_capital'), 'fund_performance summary'),
            (('waterfall', 'total_capital_called'), 'waterfall.total_capital_called'),
        ],
        'tolerance': TOL_CURRENCY,
        'reason': 'Capital call ledger is the authoritative drawdown record.',
        'kind': 'currency',
    },
    'total_uncalled_capital': {
        'principles': ['P3', 'P7'],
        'sources': [
            (('__compute__', 'uncalled_capital'), 'Commitment minus Called (identity)'),
            (('fund_performance', 'total_uncalled_capital'), 'fund_performance summary'),
        ],
        'tolerance': TOL_CURRENCY,
        'reason': 'Mathematical identity always beats a stated value.',
        'kind': 'currency',
    },
    'drawdown_pct': {
        'principles': ['P7'],
        'sources': [(('__compute__', 'drawdown_pct'), 'Called ÷ Committed')],
        'tolerance': TOL_PERCENT,
        'reason': 'Definitional ratio.',
        'kind': 'percent',
    },

    # ── D. Distributions ─────────────────────────────────────────────────────
    'total_distributions': {
        'principles': ['P3', 'P7'],
        'sources': [
            (('__compute__', 'sum_distributions'), 'Sum of distribution ledger'),
            (('fund_performance', 'total_distributions'), 'fund_performance summary'),
            (('waterfall', 'total_distributions'), 'waterfall summary'),
        ],
        'tolerance': TOL_CURRENCY,
        'reason': 'Per-line distributions are auditable; summaries can lag.',
        'kind': 'currency',
    },
    'lp_distributions': {
        'principles': ['P3', 'P5'],
        'sources': [
            (('__compute__', 'sum_capital_distributions'), 'Sum of capital distributions (ILPA)'),
            (('__compute__', 'sum_distributions'), 'Sum of all distributions (fallback)'),
            (('fund_performance', 'total_distributions'), 'fund_performance summary'),
        ],
        'tolerance': TOL_CURRENCY,
        'reason': 'DPI numerator per ILPA convention: capital-only.',
        'kind': 'currency',
    },
    'return_of_capital_amount': {
        'principles': ['P1'],
        'sources': [
            (('waterfall', 'step_1_return_of_capital'), 'Waterfall Step 1'),
            (('fund_performance', 'return_of_capital_amount'), 'fund_performance'),
        ],
        'tolerance': TOL_CURRENCY,
        'reason': 'Waterfall Step 1 is the canonical tier for ROC.',
        'kind': 'currency',
    },

    # ── E. Performance Metrics ───────────────────────────────────────────────
    'net_irr': {
        'principles': ['P1', 'P4'],
        'sources': [
            (('fund_performance', 'net_irr_computed'), 'Audited / computed net IRR'),
            (('fund_performance', 'net_irr_stated'), 'GP-stated net IRR'),
            (('fund_performance', 'net_irr'), 'Generic net_irr field'),
        ],
        'tolerance': TOL_PERCENT,
        'reason': 'Audited net IRR overrides stated; XIRR is the universal computation.',
        'kind': 'percent',
    },
    'gross_irr': {
        'principles': ['P1'],
        'sources': [
            (('fund_performance', 'gross_irr'), 'fund_performance.gross_irr'),
        ],
        'tolerance': TOL_PERCENT,
        'reason': 'Per-deal IRR aggregation; PPM standard.',
        'kind': 'percent',
    },
    'tvpi': {
        'principles': ['P7', 'P1'],
        'sources': [
            (('__compute__', 'tvpi_canonical'), 'Canonical identity (Dist+NAV)/Called'),
            (('fund_performance', 'tvpi'), 'fund_performance.tvpi'),
        ],
        'tolerance': TOL_MULTIPLE,
        'reason': 'TVPI is a definitional ratio; computed identity is authoritative.',
        'kind': 'multiple',
    },
    'dpi': {
        'principles': ['P7', 'P1'],
        'sources': [
            (('__compute__', 'dpi_canonical'), 'Canonical identity capital-dist/Called'),
            (('fund_performance', 'dpi'), 'fund_performance.dpi'),
        ],
        'tolerance': TOL_MULTIPLE,
        'reason': 'DPI per ILPA — capital distributions only.',
        'kind': 'multiple',
    },
    'rvpi': {
        'principles': ['P7'],
        'sources': [
            (('__compute__', 'rvpi_canonical'), 'NAV ÷ Called'),
            (('fund_performance', 'rvpi'), 'fund_performance.rvpi'),
        ],
        'tolerance': TOL_MULTIPLE,
        'reason': 'Definitional ratio.',
        'kind': 'multiple',
    },
    'moic': {
        'principles': ['P3', 'P7'],
        'sources': [
            (('__compute__', 'moic_canonical'), 'Active FV ÷ Invested Cost'),
            (('fund_performance', 'moic_portfolio'), 'fund_performance.moic_portfolio'),
            (('fund_performance', 'moic'), 'fund_performance.moic'),
        ],
        'tolerance': TOL_MULTIPLE,
        'reason': 'Row-sum identity is authoritative for portfolio MOIC.',
        'kind': 'multiple',
    },
    'active_fair_value': {
        'principles': ['P3', 'P7'],
        'sources': [
            (('__compute__', 'sum_active_fv'), 'Sum of Valuation.fair_value_of_holding'),
            (('fund_performance', 'total_unrealised_fv_holding'), 'fund_performance summary'),
        ],
        'tolerance': TOL_CURRENCY,
        'reason': 'Per-row valuation sum is the ground truth.',
        'kind': 'currency',
    },
    'invested_cost': {
        'principles': ['P3'],
        'sources': [
            (('__compute__', 'sum_active_cost'), 'Sum of Investment.total_invested'),
            (('fund_performance', 'total_invested_capital'), 'fund_performance summary'),
        ],
        'tolerance': TOL_CURRENCY,
        'reason': 'Per-investment cost ledger sums to the authoritative total.',
        'kind': 'currency',
    },

    # ── F. NAV ───────────────────────────────────────────────────────────────
    'fund_nav': {
        'principles': ['P1', 'P2', 'P6'],
        'sources': [
            (('nav_records', '__latest_by__', 'nav_date', 'total_nav'), 'Latest NAVRecord row'),
            (('fund_performance', 'fund_nav_latest'), 'fund_performance.fund_nav_latest'),
        ],
        'tolerance': TOL_CURRENCY,
        'reason': 'NAV walks are SEBI-mandated quarterly ledgers; latest row IS the as-of NAV.',
        'kind': 'currency',
    },

    # ── L. Waterfall & Carry ─────────────────────────────────────────────────
    'carry_base': {
        'principles': ['P1', 'P7'],
        'sources': [
            (('waterfall', 'carry_base'), 'waterfall.carry_base'),
            (('waterfall', 'available_after_roc_and_pref'), 'waterfall.available_after_roc_and_pref'),
            (('fund_performance', 'carry_base'), 'fund_performance.carry_base'),
        ],
        'tolerance': TOL_CURRENCY,
        'reason': 'Carry base is contractual per LPA; waterfall sheet authoritative.',
        'kind': 'currency',
    },
    'preferred_return_amount': {
        'principles': ['P1'],
        'sources': [
            (('waterfall', 'step_2_preferred_return'), 'Waterfall Step 2'),
            (('waterfall', 'preferred_return_amount'), 'waterfall.preferred_return_amount'),
            (('fund_performance', 'preferred_return_amount'), 'fund_performance'),
        ],
        'tolerance': TOL_CURRENCY,
        'reason': 'Waterfall Step 2 is the canonical hurdle slot.',
        'kind': 'currency',
    },
    'gp_catchup_amount': {
        'principles': ['P1'],
        'sources': [
            (('waterfall', 'step_3_catchup_amount'), 'Waterfall Step 3'),
            (('waterfall', 'gp_catchup_amount'), 'waterfall.gp_catchup_amount'),
        ],
        'tolerance': TOL_CURRENCY,
        'reason': 'Waterfall Step 3 is the canonical catch-up slot.',
        'kind': 'currency',
    },
    'carry_amount_gross': {
        'principles': ['P1'],
        'sources': [
            (('waterfall', 'carry_amount_gross'), 'waterfall.carry_amount_gross'),
            (('fund_performance', 'carry_amount_gross'), 'fund_performance.carry_amount_gross'),
        ],
        'tolerance': TOL_CURRENCY,
        'reason': 'Gross carry per LPA waterfall.',
        'kind': 'currency',
    },
    'carry_amount_net': {
        'principles': ['P1', 'P7'],
        'sources': [
            (('waterfall', 'net_carry'), 'waterfall.net_carry'),
            (('waterfall', 'carry_amount_net'), 'waterfall.carry_amount_net'),
            (('fund_performance', 'carry_amount_net'), 'fund_performance.carry_amount_net'),
        ],
        'tolerance': TOL_CURRENCY,
        'reason': 'Net carry = gross − clawback (identity).',
        'kind': 'currency',
    },
    'gp_clawback_provision': {
        'principles': ['P1'],
        'sources': [
            (('waterfall', 'clawback_provision'), 'waterfall.clawback_provision'),
            (('fund_performance', 'gp_clawback_provision'), 'fund_performance.gp_clawback_provision'),
        ],
        'tolerance': TOL_CURRENCY,
        'reason': 'Clawback reserve per LPA (industry standard 20%).',
        'kind': 'currency',
    },
    'accrued_management_fees': {
        'principles': ['P1'],
        'sources': [(('fund_performance', 'accrued_management_fees'), 'fund_performance')],
        'tolerance': TOL_CURRENCY,
        'reason': 'Accrued fees accumulate quarterly per management agreement.',
        'kind': 'currency',
    },
}


# ── IDENTITY_CHECKS — universal mathematical identities (P7) ─────────────────
# Cross-layer validator runs these AFTER reconciliation. Violations are
# attached to the field's provenance as a quality flag — never block persistence.

IDENTITY_CHECKS = [
    {
        'name': 'tvpi_identity',
        'description': 'TVPI ≡ (Distributions + NAV) ÷ Called',
        'lhs': lambda m: _to_dec(resolve_path(m, ('fund_performance', 'tvpi'))),
        'rhs': lambda m: _to_dec(_tvpi_canonical(m)),
        'tolerance': TOL_MULTIPLE,
        'principle': 'P7',
    },
    {
        'name': 'rvpi_plus_dpi_eq_tvpi',
        'description': 'RVPI + DPI ≡ TVPI',
        'lhs': lambda m: (
            (_to_dec(_rvpi_canonical(m)) or Decimal('0'))
            + (_to_dec(_dpi_canonical(m)) or Decimal('0'))
        ),
        'rhs': lambda m: _to_dec(_tvpi_canonical(m)),
        'tolerance': TOL_MULTIPLE,
        'principle': 'P7',
    },
    {
        'name': 'moic_row_sum',
        'description': 'MOIC ≡ Σ active FV ÷ Σ invested cost',
        'lhs': lambda m: _to_dec(resolve_path(m, ('fund_performance', 'moic_portfolio'))),
        'rhs': lambda m: _to_dec(_moic_canonical(m)),
        'tolerance': TOL_MULTIPLE,
        'principle': 'P7',
    },
    {
        'name': 'active_fv_row_sum',
        'description': 'active_fair_value ≡ Σ Valuation.fair_value_of_holding',
        'lhs': lambda m: _to_dec(resolve_path(m, ('fund_performance', 'total_unrealised_fv_holding'))),
        'rhs': lambda m: _to_dec(_sum_active_fv(m)),
        'tolerance': TOL_CURRENCY,
        'principle': 'P3',
    },
    {
        'name': 'committed_row_sum',
        'description': 'total_committed_capital ≡ Σ Commitment.commitment_amount',
        'lhs': lambda m: _to_dec(resolve_path(m, ('fund_performance', 'total_committed_capital'))),
        'rhs': lambda m: _to_dec(_sum_commitments(m)),
        'tolerance': TOL_CURRENCY,
        'principle': 'P3',
    },
    {
        'name': 'called_row_sum',
        'description': 'total_called_capital ≡ Σ CapitalCall.total_call_amount',
        'lhs': lambda m: _to_dec(resolve_path(m, ('fund_performance', 'total_called_capital'))),
        'rhs': lambda m: _to_dec(_sum_called(m)),
        'tolerance': TOL_CURRENCY,
        'principle': 'P3',
    },
    {
        'name': 'distributions_row_sum',
        'description': 'total_distributions ≡ Σ Distribution.total_net_amount',
        'lhs': lambda m: _to_dec(resolve_path(m, ('fund_performance', 'total_distributions'))),
        'rhs': lambda m: _to_dec(_sum_distributions(m)),
        'tolerance': TOL_CURRENCY,
        'principle': 'P3',
    },
    {
        'name': 'net_carry_identity',
        'description': 'carry_amount_net ≡ carry_amount_gross − gp_clawback_provision',
        'lhs': lambda m: _to_dec(resolve_path(m, ('waterfall', 'net_carry')))
                         or _to_dec(resolve_path(m, ('waterfall', 'carry_amount_net'))),
        'rhs': lambda m: (
            (_to_dec(resolve_path(m, ('waterfall', 'carry_amount_gross'))) or Decimal('0'))
            - (_to_dec(resolve_path(m, ('waterfall', 'clawback_provision'))) or Decimal('0'))
        ),
        'tolerance': TOL_CURRENCY,
        'principle': 'P7',
    },
]


# ── Helper: tolerance comparison (universal) ─────────────────────────────────

def within_tolerance(a, b, tol: dict) -> bool:
    """Return True if values a and b agree within the given tolerance band."""
    if a is None or b is None:
        return False
    try:
        a_d = Decimal(str(a))
        b_d = Decimal(str(b))
    except Exception:
        return str(a) == str(b)
    diff = abs(a_d - b_d)
    if tol.get('abs') is not None and diff <= tol['abs']:
        return True
    if tol.get('pct') is not None:
        denom = max(abs(a_d), abs(b_d))
        if denom == 0:
            return diff == 0
        if (diff / denom) * Decimal('100') <= tol['pct']:
            return True
    return False
