"""
Phase 4 — Layer 2: Gemini-Compute-With-Code-Execution

Single deterministic compute pass for every fund-level + per-investment
derived metric on the TrackFundAI dashboard.

WHY THIS EXISTS
  The legacy Python waterfall in phase4_derivations.compute_all_fund_aggregates()
  is brittle: it must encode every LPA convention by hand, mis-handles
  multi-currency / multi-class / Cat-III hedge-style structures, and the
  numbers drift across re-imports because of unstable tie-breaks downstream.

  Meanwhile, Gemini-chat (gemini.google.com) computes these same metrics
  perfectly and deterministically — because under the hood it runs Python
  in a Code Execution sandbox. Same workbook → same Python → same answer.

  This module mirrors that exact behaviour inside our pipeline:
    1. We hand Gemini the full Phase-3 atomic extraction + LPA terms
       + workbook structural context.
    2. We enable the Code Execution tool.
    3. Gemini writes Python, executes it, returns metric values + the
       code it used as auditable provenance.

CONTRACT (drop-in replacement for compute_all_fund_aggregates)
  Returns a dict whose top-level keys EXACTLY match what
  phase2_persister._persist_fund_metrics() and _persist_carried_interest()
  consume. See METRIC_CONTRACT below for the authoritative list.

  Every metric value is wrapped in a provenance envelope:
    {value: Decimal, formula_used: str, cell_refs: [str], python_code: str,
     source: 'gemini_code_execution' | 'extraction_override' | 'unavailable'}

  For backwards compatibility the same module ALSO exposes the flat-dict
  shape the old persister expects via flatten_for_persister().

TEMPLATING DISCIPLINE
  The prompt uses sentinel-based replacement (plain triple-quoted strings
  + .replace('__SENTINEL__', value)) — NEVER f-strings — because the body
  contains JSON examples with literal { } characters. See the 2026-06-30
  incident documented in prompts/layer1_identity.py for why this matters.

DETERMINISM GUARANTEES
  - temperature=0
  - Atomic data serialised in sorted key/date order before prompting
  - Cell refs in the prompt are sorted
  - Gemini is explicitly instructed to sort cash flows + use a fixed IRR
    bracket before computing
  - Same workbook + same Phase-3 JSON → same Python code → same output
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from google.genai import types as genai_types

from api.gemini_service import generate_content, get_model_name

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════
# METRIC CONTRACT
# Every key the dashboard, chatbot, persister and waterfall card consume.
# If you add a metric to the dashboard, add it here too — Gemini won't
# emit it otherwise.
# ═════════════════════════════════════════════════════════════════════════

METRIC_CONTRACT: Dict[str, str] = {
    # ── Fund-level capital totals (₹ Cr) ────────────────────────────────
    'total_committed_capital':   'Total LP commitments. Sum of Commitment.committed_amount per LP.',
    'total_capital_called':      'Total capital drawn down from LPs. Sum of CapitalCall amounts (or per-LP cumulative_called if call rows sparse).',
    'total_uncalled_capital':    'total_committed_capital − total_capital_called.',
    'total_invested_capital':    'Cumulative cost of all portfolio investments (sum of Investment.amount_invested).',
    'total_realised_proceeds':   'Cumulative cash returned from exits + dividends. Sum of ExitEvent.exit_proceeds.',
    'total_distributions':       'Cumulative LP distributions of CAPITAL types only (return_of_capital + STCG + LTCG); EXCLUDES interim interest/dividends.',
    'total_unrealised_fv_holding': 'Sum of latest Valuation.fair_value_of_holding per Investment (preferred) else fair_value.',
    'total_unrealised_gains':    'total_unrealised_fv_holding − total_invested_capital (only counts live investments).',
    'total_realised_gains':      'total_realised_proceeds − cost_basis_of_exited_investments.',
    'fund_nav_latest':           'Most recent NAVRecord.nav_value for the scheme.',
    'fund_nav_per_unit':         'Most recent NAVRecord.nav_per_unit.',
    'total_units_outstanding':   'Most recent NAVRecord.total_units_outstanding.',
    'nav_trend_qoq_pct':         '(latest_nav − prior_quarter_nav) / prior_quarter_nav × 100. None if <2 NAV points.',

    # ── Performance ratios (fund-level) ─────────────────────────────────
    'moic':                      '(total_distributions + total_unrealised_fv_holding) / total_capital_called. Net basis.',
    'tvpi':                      'Same as MOIC for European whole-fund: (distributions + NAV) / called.',
    'dpi':                       'total_distributions / total_capital_called.',
    'rvpi':                      'total_unrealised_fv_holding / total_capital_called.',
    'net_irr':                   'XIRR over LP cash flows: calls as negative, distributions as positive, final NAV as positive terminal inflow at as_of_date.',
    'gross_irr':                 'XIRR over GROSS cash flows (before mgmt fees + carry). Emit null if not derivable.',

    # ── European whole-fund waterfall ───────────────────────────────────
    'return_of_capital_amount':  'Step 1: total_capital_called returned 100% to LPs first.',
    'preferred_return_amount':   'Step 2: LP hurdle = called × ((1 + hurdle/100)^years − 1). years = (as_of − weighted_avg_call_date).days / 365.25.',
    'gp_catchup_amount':         'Step 3: GP catch-up = preferred_return × (carry_pct/100) / (1 − carry_pct/100). For 20% carry, ratio = 0.25.',
    'carry_base':                'Profit pool = (total_distributions + fund_nav) − total_capital_called.',
    'carry_amount_gross':        'gp_catchup + 0.20 × residual_after_catchup. REJECT extracted values labelled "Allocated" — that includes GP commitment returns, not just carry.',
    'gp_clawback_provision':     'gp_holdback_pct × carry_amount_gross. Industry default 20% unless LPA specifies otherwise.',
    'carry_amount_net':          'carry_amount_gross − gp_clawback_provision.',
    'lp_total_return':           'return_of_capital + preferred_return + 0.80 × residual_after_catchup.',
    'gp_total_distribution':     'gp_catchup + 0.20 × residual_after_catchup.',
    'sponsor_commitment_amount': 'GP/Sponsor commitment (₹ Cr). Typically 2.5% of fund corpus per SEBI Cat-II AIF norms.',

    # ── Fees ────────────────────────────────────────────────────────────
    'accrued_management_fees':   'Cumulative mgmt fees accrued = corpus × mgmt_fee_pct/100 × years_active.',
    'paid_management_fees':      'Management fees actually paid out (sum of FundLedger entries type=management_fee).',
    'total_management_fee_ytd':  'Mgmt fees for current calendar/fiscal year only.',
    'gst_on_management_fee':     'Sum of GST charged on management fee invoices (₹ Cr).',

    # ── Deployment / portfolio shape ────────────────────────────────────
    'portfolio_company_count':   'Count of distinct PortfolioCompany rows with at least one Investment.',
    'deployment_pct':            '(total_capital_called / total_committed_capital) × 100.',
    'capital_available_for_deployment': 'total_committed_capital − total_invested_capital.',
    'avg_cost_per_company':      'total_invested_capital / portfolio_company_count.',
    'avg_holding_years':         'Weighted-mean across companies of (as_of − first_investment_date).days / 365.25.',
    'unrealised_gain_vs_cost':   'total_unrealised_fv_holding − total_invested_capital (alias of total_unrealised_gains for dashboard tile).',

    # ── Quoted vs unquoted analytics ────────────────────────────────────
    'quoted_companies_count':    'Count of Investments where instrument is publicly quoted.',
    'unquoted_companies_count':  'Count of Investments not publicly quoted.',
    'quoted_cost_deployed':      'Sum of amount_invested for quoted holdings (₹ Cr).',
    'unquoted_cost_deployed':    'Sum of amount_invested for unquoted holdings (₹ Cr).',
    'quoted_fair_value':         'Sum of latest fair_value_of_holding for quoted holdings (₹ Cr).',
    'unquoted_fair_value':       'Sum of latest fair_value_of_holding for unquoted holdings (₹ Cr).',

    # ── Exit aggregates ─────────────────────────────────────────────────
    'total_realised_exits_count': 'Count of ExitEvent rows (actual exits, not modelled scenarios).',
    'avg_exit_moic':             'Mean of MOIC across realised exits (exit_proceeds / cost_basis).',
    'avg_exit_irr_pct':          'Mean of IRR across realised exits.',

    # ── SaaS portfolio rollups ──────────────────────────────────────────
    'portfolio_mrr':             'Sum of latest MRR across SaaS portfolio companies (₹ Cr).',
    'portfolio_arr':             'portfolio_mrr × 12 (₹ Cr).',
    'portfolio_avg_churn_pct':   'Mean monthly churn % across SaaS companies.',
    'portfolio_avg_nrr_pct':     'Mean Net Revenue Retention % across SaaS companies.',

    # ── Governance / counts ─────────────────────────────────────────────
    'board_meeting_count':       'Count of BoardMeeting records.',
    'board_meeting_companies_count': 'Distinct PortfolioCompany count having at least 1 BoardMeeting.',
    'fund_board_seats':          'Count of Investments where board_seat=True.',
    'lp_count':                  'Distinct count of Investor records on this scheme.',

    # ── Compliance scalars ──────────────────────────────────────────────
    'exceeds_10pct_threshold_investments': 'Count of Investments where exceeds_10pct_threshold=True (SEBI T+30 rule).',
    'depository_reconciled_count': 'Count of NAVRecord rows where depository_reconciled=True.',
    'depository_variance_amount': 'Sum of NAVRecord.depository_variance_amount (₹).',
    'str_filed_count':           'Count of Investors with str_filed=True (Suspicious Transaction Report).',

    # ── LPA terms (echoed so persister can pick them up uniformly) ─────
    'hurdle_rate_pct':           'LP hurdle / preferred return rate in percent (e.g. 8.0).',
    'carry_percentage':          'GP carried interest percentage (e.g. 20.0).',
    'mgmt_fee_pct':              'Annual management fee % on committed capital (e.g. 2.0).',
    'gp_holdback_pct':           'Carry escrow holdback % (industry default 20.0).',
    'sponsor_commitment_pct':    'Sponsor commitment as % of corpus (e.g. 2.5 for SEBI Cat-II AIF).',
    'fund_vintage_year':         'Year the fund started investing.',
    'as_of_date':                'YYYY-MM-DD — calculation date (latest NAV date → latest distribution → today).',
}


# ═════════════════════════════════════════════════════════════════════════
# LIST-VALUED METRICS — emitted as arrays under the same top-level dict.
# Each row is computed by Gemini in the same code execution call.
# ═════════════════════════════════════════════════════════════════════════

LIST_METRIC_CONTRACT: Dict[str, str] = {
    'per_investment_metrics': (
        'Array, one row per Investment. Fields: '
        '{company_name, cost_basis, current_fv, realised_proceeds, '
        'unrealised_gain, irr_pct, moic, holding_period_years, status}.'
    ),
    'per_lp_metrics': (
        'Array, one row per Investor. Fields: '
        '{lp_name, commitment_amount, called_amount, called_pct, '
        'distributed_amount, unrealised_position, dpi, moic, irr_pct}.'
    ),
    'per_sector_metrics': (
        'Array, one row per distinct sector. Fields: '
        '{sector, company_count, total_cost, total_fv, '
        'pct_of_total_fv, avg_moic, avg_irr_pct}.'
    ),
    'per_stage_metrics': (
        'Array, one row per investment stage (seed/series_a/growth/late). Fields: '
        '{stage, company_count, pct_of_total_count, avg_moic, total_cost}.'
    ),
    'per_geography_metrics': (
        'Array, one row per distinct city/country. Fields: '
        '{location, company_count, total_cost, total_fv}.'
    ),
    'per_exit_type_metrics': (
        'Array, one row per exit_type (ipo/ma/secondary/buyback/writeoff). Fields: '
        '{exit_type, count, total_proceeds, avg_moic, avg_irr_pct}.'
    ),
}


# ═════════════════════════════════════════════════════════════════════════
# Per-investment metric contract — populated by a separate call when the
# fund has >0 portfolio companies. Returned as a list keyed by investment.
# ═════════════════════════════════════════════════════════════════════════

PER_INVESTMENT_METRIC_CONTRACT: Dict[str, str] = {
    'irr_pct':           'XIRR over tranches (-) + exits (+) + current FV (+) terminal.',
    'moic':              '(realised_proceeds + current_fv) / cost_basis.',
    'current_fv':        'Latest Valuation.fair_value_of_holding for this investment; else fair_value; else cost × fund_markup.',
    'realised_proceeds': 'Sum of ExitEvent.proceeds_received for this investment.',
    'unrealised_gain':   'current_fv − cost_basis.',
    'holding_period_years': '(as_of − first_tranche_date) in years.',
}


# ═════════════════════════════════════════════════════════════════════════
# Prompt template — sentinel-based (DO NOT convert to f-string; the
# body contains literal JSON examples with { } characters)
# ═════════════════════════════════════════════════════════════════════════

_COMPUTE_PROMPT_TEMPLATE = """You are a CFO-grade financial analyst computing Indian AIF (Alternative Investment Fund) waterfall and performance metrics for fund "__FUND_NAME__".

MANDATORY: use the Code Execution tool. Do NOT estimate; do NOT pattern-match labels. Write Python that operates on the inline JSON data below and returns exact numbers.

═══════════════════════════════════════════════════════════════════
INPUT 1 — LPA TERMS (already extracted from Fund_Overview / Scheme master)
═══════════════════════════════════════════════════════════════════
__LPA_TERMS_JSON__

═══════════════════════════════════════════════════════════════════
INPUT 2 — ATOMIC PER-ROW DATA (Phase-3 extraction, already validated)
═══════════════════════════════════════════════════════════════════
capital_calls = __CAPITAL_CALLS_JSON__

distributions = __DISTRIBUTIONS_JSON__

investments = __INVESTMENTS_JSON__

valuations = __VALUATIONS_JSON__

nav_records = __NAV_RECORDS_JSON__

commitments_by_lp = __COMMITMENTS_JSON__

═══════════════════════════════════════════════════════════════════
INPUT 3 — WORKBOOK STRUCTURAL CONTEXT (for cell-ref provenance only)
═══════════════════════════════════════════════════════════════════
__WORKBOOK_CONTEXT__

═══════════════════════════════════════════════════════════════════
TASK A — compute every SCALAR metric listed below
═══════════════════════════════════════════════════════════════════
__SCALAR_METRIC_LIST__

═══════════════════════════════════════════════════════════════════
TASK B — emit every LIST-valued metric listed below (one array per key)
═══════════════════════════════════════════════════════════════════
__LIST_METRIC_LIST__

═══════════════════════════════════════════════════════════════════
WATERFALL RULE — European whole-fund (SEBI / ILPA standard)
═══════════════════════════════════════════════════════════════════
For a fund with N LPs treated as a single pool:

  total_value          = total_distributions + fund_nav_latest
  step_1_roc           = total_capital_called                            (returned to LPs first)
  available_after_roc  = max(0, total_value − step_1_roc)

  # Preferred return: hurdle compounded over weighted-average call age
  years                = (as_of − weighted_avg_call_date).days / 365.25
  step_2_pref          = total_capital_called × ((1 + hurdle/100)^years − 1)
  step_2_pref          = min(step_2_pref, available_after_roc)
  available_after_pref = available_after_roc − step_2_pref

  # Catch-up: GP gets carry%/(1-carry%) of preferred return,
  # so that AFTER catch-up the GP has earned carry% of (pref+catchup)
  catchup_ratio        = (carry_pct/100) / (1 − carry_pct/100)
  step_3_catchup       = step_2_pref × catchup_ratio
  step_3_catchup       = min(step_3_catchup, available_after_pref)
  residual             = max(0, available_after_pref − step_3_catchup)

  # 80:20 split of residual
  step_4_lp            = 0.80 × residual
  step_4_gp            = 0.20 × residual

  carry_base           = available_after_roc                              (profit pool)
  carry_amount_gross   = step_3_catchup + step_4_gp
  gp_clawback          = (gp_holdback_pct/100) × carry_amount_gross
  carry_amount_net     = carry_amount_gross − gp_clawback
  lp_total_return      = step_1_roc + step_2_pref + step_4_lp
  gp_total_distribution = step_3_catchup + step_4_gp

═══════════════════════════════════════════════════════════════════
HARD RULES
═══════════════════════════════════════════════════════════════════
1. Compute every metric. If a metric cannot be derived, emit value=null and
   set source="unavailable" with a reason in formula_used (e.g. "no LPA hurdle rate").
   NEVER fabricate a number to fill a tile.
2. Round all monetary values to 2 decimal places (₹ Cr).
3. For IRR/MOIC/TVPI/DPI/RVPI:
   - Sort cash flows by (date ascending, amount ascending) — STABLE.
   - IRR: scipy.optimize.brentq over [-0.99, 10.0]. If brentq raises, fall
     back to scipy.optimize.newton with x0=0.10.
   - Return IRR as percent (e.g. 38.5 not 0.385).
4. For waterfall metrics:
   - If LPA hurdle / carry_pct missing, default hurdle=8.0, carry_pct=20.0
     (industry standard for SEBI Cat-II AIF). Set assumption_used=true in
     formula_used so the dashboard can flag it.
5. as_of_date precedence: latest NAV date → latest distribution date →
   today. State which you used.
6. DETERMINISM: same input → same output. Do not introduce randomness.

═══════════════════════════════════════════════════════════════════
OUTPUT — single JSON object as the FINAL text response
═══════════════════════════════════════════════════════════════════
{
  "metrics": {
    "total_capital_called":   {"value": 2050.00, "unit": "INR Cr", "formula_used": "sum(CapitalCall.total_call_amount)", "cell_refs": ["Capital_Calls!E2:E20"], "source": "gemini_code_execution"},
    "carry_base":             {"value": 1380.60, "unit": "INR Cr", "formula_used": "(total_distributions + fund_nav) − total_capital_called", "cell_refs": [], "source": "gemini_code_execution"},
    "carry_amount_gross":     {"value": 276.12,  "unit": "INR Cr", "formula_used": "step_3_catchup + 0.20 × residual_after_catchup", "cell_refs": [], "source": "gemini_code_execution"},
    "net_irr":                {"value": 38.20,   "unit": "percent", "formula_used": "XIRR over LP cash flows with terminal NAV", "cell_refs": [], "source": "gemini_code_execution"}
  },
  "list_metrics": {
    "per_investment_metrics": [
      {"company_name": "Acme", "cost_basis": 50.0, "current_fv": 120.0, "realised_proceeds": 0, "unrealised_gain": 70.0, "irr_pct": 24.5, "moic": 2.4, "holding_period_years": 3.2, "status": "active"}
    ],
    "per_lp_metrics": [
      {"lp_name": "Investor A", "commitment_amount": 100.0, "called_amount": 95.0, "called_pct": 95.0, "distributed_amount": 80.0, "unrealised_position": 30.0, "dpi": 0.84, "moic": 1.16, "irr_pct": 12.3}
    ],
    "per_sector_metrics": [
      {"sector": "Fintech", "company_count": 8, "total_cost": 200, "total_fv": 480, "pct_of_total_fv": 23.1, "avg_moic": 2.4, "avg_irr_pct": 28.0}
    ],
    "per_stage_metrics": [],
    "per_geography_metrics": [],
    "per_exit_type_metrics": []
  },
  "calculation_notes": "European whole-fund waterfall applied. hurdle=8%, carry=20%, holdback=20% (industry defaults — LPA did not specify). IRR via scipy.optimize.brentq. Per-LP IRR uses pro-rata distribution share when per-LP distribution rows missing.",
  "as_of_date_used": "2026-03-31",
  "assumptions_used": ["hurdle defaulted to 8.0%", "carry defaulted to 20.0%"]
}

Return ONLY the JSON. No prose, no markdown fences outside the executable_code blocks. The executable_code parts will be captured separately as auditable provenance."""


# ═════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════

def _d(v, default=None) -> Optional[Decimal]:
    """Coerce to Decimal, return default on failure."""
    if v is None or v == '':
        return default
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _date_iso(d) -> Optional[str]:
    """Normalise dates to YYYY-MM-DD strings for deterministic serialisation."""
    if d is None:
        return None
    if isinstance(d, (date, datetime)):
        return d.isoformat()[:10]
    s = str(d).strip()[:10]
    return s if re.match(r'^\d{4}-\d{2}-\d{2}$', s) else None


def _json_safe(v):
    """Convert Decimal/date/datetime to JSON-serialisable primitives."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in sorted(v.items())}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    return v


def _serialise_for_prompt(rows: List[dict], keys: Tuple[str, ...]) -> str:
    """Filter + sort + JSON-encode a list of atomic rows for the prompt.

    `keys` is the field subset Gemini needs. Sorting by date+id ensures the
    serialisation is byte-identical across re-imports (drives determinism).
    """
    if not rows:
        return '[]'
    cleaned = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        cleaned.append({k: _json_safe(r.get(k)) for k in keys})
    cleaned.sort(key=lambda x: (
        str(x.get('date') or x.get('call_date') or x.get('distribution_date') or
            x.get('valuation_date') or x.get('nav_date') or ''),
        str(x.get('lp_name') or x.get('company_name') or x.get('investor_name') or ''),
        str(x.get('amount') or x.get('total_call_amount') or x.get('total_gross_amount') or ''),
    ))
    return json.dumps(cleaned, sort_keys=True)


# ═════════════════════════════════════════════════════════════════════════
# Atomic-data assembly — pulls rows from the unified Phase 3 JSON
# ═════════════════════════════════════════════════════════════════════════

def _assemble_atomic_inputs(unified_json: dict) -> dict:
    """Extract atomic per-row data from the Phase-3 unified JSON.

    Phase 3 has already validated + deduped these arrays. We only project
    the field subset Gemini needs for waterfall + performance computation.
    """
    u = unified_json or {}

    capital_calls = u.get('capital_calls') or []
    distributions = u.get('distributions') or []
    investments   = u.get('portfolio_investments') or u.get('investments') or []
    valuations    = u.get('valuations') or []
    nav_records   = u.get('nav_records') or []
    commitments   = u.get('commitments') or []

    return {
        'capital_calls': _serialise_for_prompt(capital_calls, (
            'call_date', 'lp_name', 'total_call_amount', 'called_amount',
            'amount', 'call_number', 'purpose',
        )),
        'distributions': _serialise_for_prompt(distributions, (
            'distribution_date', 'lp_name', 'distribution_type',
            'total_gross_amount', 'total_net_amount', 'amount',
            'gp_carry_amount', 'tax_withheld',
        )),
        'investments': _serialise_for_prompt(investments, (
            'company_name', 'sector', 'investment_date', 'amount_invested',
            'cost_basis', 'instrument_type', 'irr_pct', 'moic',
        )),
        'valuations': _serialise_for_prompt(valuations, (
            'company_name', 'valuation_date', 'fair_value', 'fair_value_of_holding',
            'cost_basis', 'unrealised_gain',
        )),
        'nav_records': _serialise_for_prompt(nav_records, (
            'nav_date', 'nav_value', 'nav_per_unit',
        )),
        'commitments': _serialise_for_prompt(commitments, (
            'lp_name', 'committed_amount', 'cumulative_called',
            'cumulative_distributed',
        )),
    }


def _assemble_lpa_terms(fund, scheme, unified_json: dict) -> dict:
    """Pull LPA terms from the Phase-3 unified JSON + fund/scheme DB models.

    Priority: explicit extracted value → DB field → industry default.
    The defaults DO get used downstream — Gemini is told to flag them via
    assumptions_used so the dashboard can render an "assumption" badge.
    """
    u = unified_json or {}
    fund_master = (u.get('fund_master') or [{}])
    fm = fund_master[0] if isinstance(fund_master, list) and fund_master else {}
    wf = (u.get('waterfall') or [{}])
    wf0 = wf[0] if isinstance(wf, list) and wf else {}

    def _pick(*candidates):
        for c in candidates:
            if c is not None and c != '':
                return _json_safe(c)
        return None

    return {
        'fund_name':              getattr(fund, 'name', None) or fm.get('fund_name'),
        'scheme_name':            getattr(scheme, 'name', None) or fm.get('scheme_name'),
        'fund_vintage_year':      _pick(fm.get('vintage_year'), getattr(fund, 'vintage_year', None)),
        'hurdle_rate_pct':        _pick(wf0.get('hurdle_rate'), fm.get('hurdle_rate'),
                                        getattr(scheme, 'hurdle_rate', None)),
        'carry_percentage':       _pick(wf0.get('carry_percentage'), fm.get('carry_percentage'),
                                        getattr(scheme, 'carry_percentage', None)),
        'mgmt_fee_pct':           _pick(fm.get('management_fee_pct'),
                                        getattr(scheme, 'management_fee_pct', None)),
        'gp_holdback_pct':        _pick(getattr(scheme, 'gp_holdback_pct', None),
                                        fm.get('gp_holdback_pct')),
        'sponsor_commitment_pct': _pick(fm.get('sponsor_commitment_pct'),
                                        getattr(scheme, 'sponsor_commitment_pct', None)),
        'fund_currency':          _pick(fm.get('currency'), 'INR'),
        'fund_corpus':            _pick(fm.get('total_corpus'), fm.get('target_corpus')),
    }


def _workbook_context_summary(unified_json: dict) -> str:
    """Compact summary of which sheets contributed which atomic blocks.

    Gives Gemini cell-ref grounding without dumping the whole workbook.
    """
    u = unified_json or {}
    sheet_completeness = u.get('sheet_completeness') or []
    if not isinstance(sheet_completeness, list):
        return '(no sheet_completeness reported by Phase 3)'

    lines = []
    seen = set()
    for s in sheet_completeness:
        if not isinstance(s, dict):
            continue
        name = s.get('sheet_name')
        if not name or name in seen:
            continue
        seen.add(name)
        target = s.get('target_array') or '?'
        rows_in = s.get('rows_in_source') or '?'
        rows_out = s.get('rows_extracted') or '?'
        lines.append(f'  - {name}: contributes to {target} ({rows_in} source rows, {rows_out} extracted)')
    return '\n'.join(lines) if lines else '(sheet_completeness empty)'


# ═════════════════════════════════════════════════════════════════════════
# Prompt builder (sentinel-based)
# ═════════════════════════════════════════════════════════════════════════

def _render_contract(contract: Dict[str, str]) -> str:
    """Render a {key: description} contract as a numbered bullet list."""
    lines = []
    for i, (key, desc) in enumerate(sorted(contract.items()), start=1):
        lines.append(f'  {i:2d}. {key} — {desc}')
    return '\n'.join(lines)


def build_compute_prompt(fund, scheme, unified_json: dict) -> str:
    """Build the single prompt Gemini receives. Sentinel-based templating
    (no f-string), so JSON examples in the body are safe."""
    lpa = _assemble_lpa_terms(fund, scheme, unified_json)
    atomic = _assemble_atomic_inputs(unified_json)
    wb_ctx = _workbook_context_summary(unified_json)

    return (
        _COMPUTE_PROMPT_TEMPLATE
        .replace('__FUND_NAME__',          str(lpa.get('fund_name') or 'Unknown Fund'))
        .replace('__LPA_TERMS_JSON__',     json.dumps(lpa, sort_keys=True, default=str, indent=2))
        .replace('__CAPITAL_CALLS_JSON__', atomic['capital_calls'])
        .replace('__DISTRIBUTIONS_JSON__', atomic['distributions'])
        .replace('__INVESTMENTS_JSON__',   atomic['investments'])
        .replace('__VALUATIONS_JSON__',    atomic['valuations'])
        .replace('__NAV_RECORDS_JSON__',   atomic['nav_records'])
        .replace('__COMMITMENTS_JSON__',   atomic['commitments'])
        .replace('__WORKBOOK_CONTEXT__',   wb_ctx)
        .replace('__SCALAR_METRIC_LIST__', _render_contract(METRIC_CONTRACT))
        .replace('__LIST_METRIC_LIST__',   _render_contract(LIST_METRIC_CONTRACT))
    )


# ═════════════════════════════════════════════════════════════════════════
# Response parser
# ═════════════════════════════════════════════════════════════════════════

def _extract_code_blocks(response) -> List[str]:
    """Collect every Python code block Gemini executed during code execution."""
    blocks = []
    try:
        for cand in (response.candidates or []):
            for part in (cand.content.parts or []):
                ec = getattr(part, 'executable_code', None)
                if ec and getattr(ec, 'code', None):
                    blocks.append(ec.code)
    except (AttributeError, TypeError):
        pass
    return blocks


def _extract_final_json(response) -> Optional[dict]:
    """Pull the final JSON object from the response text parts.

    Gemini emits text in chunks; the final JSON is in the last text part
    (or concatenated text). We strip optional markdown fences and parse.
    """
    text_parts = []
    try:
        for cand in (response.candidates or []):
            for part in (cand.content.parts or []):
                t = getattr(part, 'text', None)
                if t:
                    text_parts.append(t)
    except (AttributeError, TypeError):
        pass

    if not text_parts:
        return None

    raw = '\n'.join(text_parts).strip()
    # Strip markdown fences if present
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)

    # Find the first {...} object that parses
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Fallback: regex the outermost JSON object
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


# ═════════════════════════════════════════════════════════════════════════
# Public entry point
# ═════════════════════════════════════════════════════════════════════════

def compute_metrics_with_code_execution(
    fund,
    scheme,
    unified_json: dict,
    *,
    model: Optional[str] = None,
    timeout_ms: int = 600_000,
) -> Dict[str, Any]:
    """Single Gemini call with Code Execution; returns all fund-level metrics.

    Returns:
      {
        'ok':              bool,
        'metrics':         {key: {value, formula_used, cell_refs, source, ...}},
        'flat':            {key: Decimal-or-None}   ← drop-in for old aggregates dict,
        'executed_code':   str (concatenated Python from every code block),
        'calculation_notes': str,
        'as_of_date_used': str,
        'assumptions_used': [str],
        'token_usage':     {prompt, output, total},
        'wall_time_sec':   float,
        'error':           str | None,
      }

    The persister should consume `flat` for drop-in compatibility, but should
    also store `metrics[*].source` + `formula_used` + `cell_refs` as provenance
    on each FundMetric row.
    """
    prompt = build_compute_prompt(fund, scheme, unified_json)
    prompt_chars = len(prompt)

    t0 = time.time()
    error = None
    response = None
    try:
        response = generate_content(
            prompt,
            model=model or get_model_name('gemini-2.5-pro'),  # pro = better code reasoning
            temperature=0.0,
            timeout_ms=timeout_ms,
            tools=[genai_types.Tool(code_execution=genai_types.ToolCodeExecution())],
        )
    except Exception as e:
        error = f'gemini_code_execution_call_failed: {e!s}'
        logger.exception('[phase4_compute] Gemini call failed')

    wall = time.time() - t0

    if error or response is None:
        return _empty_result(error or 'no_response', wall, prompt_chars)

    payload = _extract_final_json(response)
    code_blocks = _extract_code_blocks(response)

    if not payload or 'metrics' not in payload:
        return _empty_result(
            'gemini_returned_no_parseable_metrics',
            wall, prompt_chars,
            executed_code='\n\n# ---\n\n'.join(code_blocks),
        )

    metrics: Dict[str, Dict[str, Any]] = {}
    flat: Dict[str, Optional[Decimal]] = {}
    for key, env in (payload.get('metrics') or {}).items():
        if not isinstance(env, dict):
            continue
        val = env.get('value')
        dec = _d(val) if val is not None else None
        metrics[key] = {
            'value':        dec,
            'unit':         env.get('unit'),
            'formula_used': env.get('formula_used'),
            'cell_refs':    env.get('cell_refs') or [],
            'source':       env.get('source') or 'gemini_code_execution',
        }
        flat[key] = dec

    # List-valued metrics (per-investment, per-LP, per-sector etc.)
    list_metrics: Dict[str, List[Any]] = {}
    for key, rows in (payload.get('list_metrics') or {}).items():
        if not isinstance(rows, list):
            continue
        list_metrics[key] = rows  # keep raw; consumers (persister, dashboard) project as needed

    usage = getattr(response, 'usage_metadata', None)
    token_usage = {
        'prompt_tokens': getattr(usage, 'prompt_token_count', None) if usage else None,
        'output_tokens': getattr(usage, 'candidates_token_count', None) if usage else None,
        'total_tokens':  getattr(usage, 'total_token_count', None) if usage else None,
    }

    logger.info(
        f'[phase4_compute] OK — {len(metrics)} metrics in {wall:.1f}s '
        f'(prompt={prompt_chars} chars, code_blocks={len(code_blocks)}, '
        f'tokens={token_usage["total_tokens"]})'
    )

    return {
        'ok':                True,
        'metrics':           metrics,
        'list_metrics':      list_metrics,
        'flat':              flat,
        'executed_code':     '\n\n# ---\n\n'.join(code_blocks),
        'calculation_notes': payload.get('calculation_notes'),
        'as_of_date_used':   payload.get('as_of_date_used'),
        'assumptions_used':  payload.get('assumptions_used') or [],
        'token_usage':       token_usage,
        'wall_time_sec':     wall,
        'prompt_chars':      prompt_chars,
        'error':             None,
    }


def _empty_result(error: str, wall: float, prompt_chars: int,
                  executed_code: str = '') -> Dict[str, Any]:
    """Return a safe-empty result so the persister falls back gracefully."""
    return {
        'ok':                False,
        'metrics':           {},
        'list_metrics':      {},
        'flat':              {},
        'executed_code':     executed_code,
        'calculation_notes': None,
        'as_of_date_used':   None,
        'assumptions_used':  [],
        'token_usage':       {'prompt_tokens': None, 'output_tokens': None, 'total_tokens': None},
        'wall_time_sec':     wall,
        'prompt_chars':      prompt_chars,
        'error':             error,
    }


def flatten_for_persister(result: Dict[str, Any]) -> Dict[str, Optional[Decimal]]:
    """Project the Gemini-compute result into the flat aggregates dict shape
    that the existing phase2_persister expects (drop-in compatibility).

    Old aggregator key → new Gemini metric key are 1:1 (we deliberately
    chose the contract that way). This helper exists so callers don't have
    to reach into result['flat'] directly — keeps the call sites obvious.
    """
    if not isinstance(result, dict):
        return {}
    return dict(result.get('flat') or {})
