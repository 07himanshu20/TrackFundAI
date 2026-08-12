"""
S0 — Schema and contract (the pinned foundation).

A single versioned, machine-readable contract that everything downstream keys
off: the output schema, the concept lexicon (U2 signal 2), the arithmetic
identities (U2 signal 3), the check catalogue (Appendix A), the tolerances, the
staleness threshold and the rounding rule. Nothing downstream is hard-coded —
changing behaviour means bumping the contract version, which re-keys the cache
(U8) so no stale record is silently reused.

The output-schema DATA (19 sheets, ground-truth-derived) is reused verbatim from
the stable spec; only the contract wraps it with version + checks + lexicon.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

# Output schema is shared, stable, ground-truth-derived DATA (not pipeline
# logic) — reused rather than duplicated so it cannot drift between packages.
from ..preingest2.schema import (          # noqa: F401
    SHEETS as OUTPUT_SHEETS,
    DOMAINS,
    is_summary_label,
    sheet_by_key,
)

CONTRACT_VERSION = 'contract-2.0.0'

# ── Concept lexicon (U2 signal 2) ────────────────────────────────────────
# Seed synonyms per concept. GROWS automatically: every reviewer resolution at
# the gate writes the approved label here (see lexicon.py). Universal — the
# system is measurably better at the 1000th client than the 1st, with no code
# edit. Values are lower-cased; matching is done after normalisation.
CONCEPT_LEXICON = {
    # operating (company MIS)
    'revenue': ['revenue', 'turnover', 'sales', 'total income', 'net sales',
                'gross billings', 'total revenue', 'income from operations',
                'operating revenue', 'total sales'],
    'cogs': ['cost of goods sold', 'cogs', 'cost of sales', 'cost of revenue',
             'direct costs', 'cost of goods'],
    'gross_profit': ['gross profit', 'gross margin', 'gp', 'gross profit/(loss)'],
    'opex': ['operating expenses', 'opex', 'total opex', 'overheads',
             'administrative expenses', 'total operating expenses', 'sg&a'],
    'ebitda': ['ebitda', 'operating profit', 'pbt', 'profit before tax', 'ebit',
               'operating income', 'profit/(loss) before tax', 'pbt/(loss)'],
    'cash': ['cash', 'cash and cash equivalents', 'closing cash', 'bank balance',
             'cash balance', 'cash & bank', 'closing cash balance'],
    'headcount': ['headcount', 'employees', 'fte', 'total employees',
                  'no of employees', 'number of employees', 'total headcount'],
    # fund-level
    'commitment': ['commitment', 'committed capital', 'total commitment', 'capital commitment'],
    'called': ['called', 'capital called', 'drawn', 'drawdown', 'cumulative called', 'paid-in'],
    'distributed': ['distributed', 'distributions', 'cumulative distributed', 'dpi'],
    'fair_value': ['fair value', 'fv', 'fair value of holding', 'nav', 'carrying value'],
    'cost': ['cost', 'cost basis', 'invested', 'total invested', 'amount invested', 'deployment'],
    'proceeds': ['proceeds', 'exit proceeds', 'gross proceeds', 'realisation', 'realised proceeds'],
    # investment-schedule dimensions (fund anchor)
    'company': ['company', 'company name', 'portfolio company', 'investee',
                'investee company', 'name', 'entity'],
    'ownership_pct': ['ownership', 'ownership %', 'stake', 'stake %', 'holding %',
                      'shareholding', '% held', 'equity %', 'fund stake'],
    'domicile': ['geo', 'geography', 'domicile', 'country', 'location', 'region'],
    # descriptive investment-schedule attributes (text, not money) — enrich the
    # portfolio master; matched by the same whole-word lexicon, never positionally.
    'sector': ['sector', 'industry', 'vertical'],
    'stage': ['stage', 'investment stage', 'funding stage', 'round stage'],
    'investment_date': ['investment date', 'first investment', 'first investment date',
                        'initial investment', 'initial investment date', 'entry date',
                        'date of investment', 'invested on', '1st invest', 'inv date'],
    'instrument': ['instrument', 'instrument type', 'security type', 'share class'],
    'valuation_method': ['methodology', 'valuation methodology', 'valuation method',
                         'valuation basis', 'basis of valuation'],
    'assets': ['total assets', 'assets'],
    'liabilities': ['total liabilities', 'liabilities'],
    'equity': ['total equity', 'equity', 'net worth', 'shareholders funds'],
    'net_income': ['net income', 'pat', 'profit after tax', 'net profit',
                   'profit for the period', 'net profit/(loss)', 'profit/(loss) after tax'],
    'opening_cash': ['opening cash', 'opening balance', 'cash at beginning',
                     'opening cash balance', 'beginning cash'],
    'receipts': ['receipts', 'total receipts', 'cash inflows', 'inflows', 'collections'],
    'payments': ['payments', 'total payments', 'cash outflows', 'outflows', 'disbursements'],
    'closing_cash': ['closing cash', 'closing balance', 'cash at end',
                     'closing cash balance', 'ending cash'],
    'period_total': ['total', 'grand total', 'ytd', 'year to date', 'full year', 'fy total'],
}

# ── Stock vs flow per concept (U5) ───────────────────────────────────────
# A STOCK is a balance at an instant (never annualised); a FLOW accrues over a
# period. The reader types each located value by this map so the Quantity's
# annualisation rule is chosen by the concept, not guessed from the number.
CONCEPT_NATURE = {
    # flows
    'revenue': 'flow', 'cogs': 'flow', 'gross_profit': 'flow', 'opex': 'flow',
    'ebitda': 'flow', 'net_income': 'flow', 'receipts': 'flow', 'payments': 'flow',
    'called': 'flow', 'distributed': 'flow', 'proceeds': 'flow',
    # stocks
    'cash': 'stock', 'assets': 'stock', 'liabilities': 'stock', 'equity': 'stock',
    'opening_cash': 'stock', 'closing_cash': 'stock', 'headcount': 'stock',
    'fair_value': 'stock', 'cost': 'stock', 'commitment': 'stock',
}


def concept_nature(concept: str) -> str:
    """FLOW by default — an unknown measured concept is more often a flow, and
    an over-annualised stock is caught by the identity signals downstream."""
    return CONCEPT_NATURE.get((concept or '').strip().lower(), 'flow')


# ── Target ⇐ anchor equivalence (U5, advisor 2026-07-23) ─────────────────────
# A few OUTPUT targets are the SAME accounting quantity as an over-location anchor
# under a different statement's label. When the model precisely routes the anchor
# but marks the generic target absent, bind the target FROM the anchor's located
# row. Balance-sheet cash (the stock) IS the cash-flow "closing balance": on the
# Hubler MIS the model correctly split the cash sub-ledger — receipts="Cash
# Collected", closing_cash="Closing balance" — and called generic `cash` absent, so
# the target must inherit `closing_cash`. This is an accounting identity, not a
# file-specific patch, and it hands `cash` the cash-flow identity for free
# verification (opening_cash + receipts − payments = closing_cash).
CONCEPT_EQUIVALENCE = {'cash': 'closing_cash'}


# ── measure type per concept ─────────────────────────────────────────────
# 'money' → normalised to ₹Cr via FX + scale. 'count' → a headcount/units number,
# NEVER currency-converted or scaled (309 people is not ₹309 Cr). 'ratio' → a
# percentage/multiple carried as-is. Prevents a count from being money-scaled.
CONCEPT_MEASURE = {
    'headcount': 'count',
    'moic': 'ratio', 'tvpi': 'ratio', 'dpi': 'ratio', 'rvpi': 'ratio', 'irr': 'ratio',
    'ownership_pct': 'ratio',
}


def concept_measure(concept: str) -> str:
    return CONCEPT_MEASURE.get((concept or '').strip().lower(), 'money')

# ── Over-location anchor bundles (U1 §Deliberate over-location) ───────────
# The stable, identity-bearing lines to locate ALONGSIDE whatever the output
# needs — extra addresses in the same call, whose only purpose is proof. Bounded
# on purpose: each extra concept is failure surface, so we stop at the anchors
# that carry an arithmetic identity, never rare/ambiguous lines.
ANCHOR_PL_CHAIN = ['revenue', 'cogs', 'gross_profit', 'opex', 'ebitda', 'net_income']
ANCHOR_BS_TRIO = ['assets', 'liabilities', 'equity']
ANCHOR_CASH_CHAIN = ['opening_cash', 'receipts', 'payments', 'closing_cash']
# `period_total` is located when the statement has period columns + a total
# column, to verify sum(period columns) == stated total (the most common layout
# error). It is handled by the reader from the located cell's column siblings,
# not as a standalone equation between concepts.
OVER_LOCATION_ANCHORS = ANCHOR_PL_CHAIN + ANCHOR_BS_TRIO + ANCHOR_CASH_CHAIN + ['period_total']

# ── Arithmetic identities (U2 signal 3) ──────────────────────────────────
# The strongest, independent signal. The model is NEVER shown these; three
# independently located cells satisfying an equation is not chance. CODE
# evaluates them, so subtraction is fine here (the +-only restriction applies
# ONLY to the model's expression form, never to code-side verification).
#
# `expr` = list of (concept, sign) summing to `equals`. Each identity carries
# its OWN tolerance — real books don't tie to the rupee (rounding, adjustments,
# forex-inflated EBITDA, PAT with dividends added back). A break is severity
# 'escalate' (flag / disambiguate), NEVER a hard-fail on legitimately messy data.
#   tol_rel — fraction of the target magnitude allowed
#   tol_abs_cr — floor tolerance in ₹Cr so tiny targets don't false-break
IDENTITIES = [
    {'name': 'gross_profit_identity',
     'expr': [('revenue', +1), ('cogs', -1)], 'equals': 'gross_profit',
     'tol_rel': 0.01, 'tol_abs_cr': 0.05, 'severity': 'escalate',
     'proves': 'revenue/cogs/gp on same statement & column'},
    {'name': 'ebitda_identity',
     'expr': [('gross_profit', +1), ('opex', -1)], 'equals': 'ebitda',
     'tol_rel': 0.03, 'tol_abs_cr': 0.05, 'severity': 'escalate',
     'proves': 'EBITDA is the real line, not an adjusted/forecast variant'},
    {'name': 'balance_sheet_identity',
     'expr': [('liabilities', +1), ('equity', +1)], 'equals': 'assets',
     'tol_rel': 0.01, 'tol_abs_cr': 0.05, 'severity': 'escalate',
     'proves': 'balance sheet pointers internally coherent'},
    {'name': 'cash_flow_identity',
     'expr': [('opening_cash', +1), ('receipts', +1), ('payments', -1)],
     'equals': 'closing_cash',
     'tol_rel': 0.02, 'tol_abs_cr': 0.05, 'severity': 'escalate',
     'proves': 'the cash pointers sit on the correct period column'},
]

# Period-axis identity is structural (sum of the located figure's period-column
# siblings == its total-column value), handled by the reader, not a concept
# equation. Declared here so its tolerance is versioned with the contract.
PERIOD_SUM_TOLERANCE = {'tol_rel': 0.01, 'tol_abs_cr': 0.05, 'severity': 'escalate'}

# ── Check catalogue (Appendix A) ─────────────────────────────────────────
# Declared here as data; executed by S8 (reconcile). class ∈ hard|soft|disclosure.
CHECKS = [
    # structural / arithmetic — HARD (failure blocks the run)
    {'id': 'located_cells_populated', 'class': 'hard', 'kind': 'structural',
     'desc': 'Every located address resolves to a populated cell of the expected type'},
    {'id': 'entity_in_schedule', 'class': 'hard', 'kind': 'structural',
     'desc': "Every entity identifier exists in the fund's investment schedule"},
    {'id': 'no_dup_entity_period', 'class': 'hard', 'kind': 'structural',
     'desc': 'No two files resolve to the same entity & period without confirmation'},
    {'id': 'ratecard_covers_currencies', 'class': 'hard', 'kind': 'structural',
     'desc': 'Every currency present in the inputs is covered by the Rate Card'},
    {'id': 'tranches_sum_to_cost', 'class': 'hard', 'kind': 'arithmetic',
     'desc': 'Component tranches sum to total investment cost'},
    {'id': 'commitments_sum', 'class': 'hard', 'kind': 'arithmetic',
     'desc': 'Individual investor commitments sum to fund commitments'},
    {'id': 'called_le_committed', 'class': 'hard', 'kind': 'arithmetic',
     'desc': 'Capital called does not exceed committed'},
    {'id': 'period_sum_equals_total', 'class': 'hard', 'kind': 'arithmetic',
     'desc': 'Sum of period columns equals the stated total column'},
    # correspondence — SOFT (never blocks, ALWAYS published with variance)
    {'id': 'portfolio_revenue_vs_fund', 'class': 'soft', 'kind': 'correspondence',
     'desc': "Aggregated portfolio revenue vs the fund's own recorded figure"},
    {'id': 'realisations_vs_exits', 'class': 'soft', 'kind': 'correspondence',
     'desc': 'Realisations vs recorded exit proceeds'},
    {'id': 'distributions_vs_register', 'class': 'soft', 'kind': 'correspondence',
     'desc': 'Distributions vs the investor register'},
    # disclosure — ALWAYS published
    {'id': 'figure_absent', 'class': 'disclosure', 'kind': 'disclosure',
     'desc': 'Any figure absent from a source file'},
    {'id': 'period_stale', 'class': 'disclosure', 'kind': 'disclosure',
     'desc': 'Any period end older than the staleness threshold, with its age'},
    {'id': 'rate_estimated', 'class': 'disclosure', 'kind': 'disclosure',
     'desc': 'Any exchange rate sourced as a management estimate'},
    {'id': 'human_approved', 'class': 'disclosure', 'kind': 'disclosure',
     'desc': 'Any figure whose location or identity required human approval'},
    {'id': 'investment_without_mis', 'class': 'disclosure', 'kind': 'disclosure',
     'desc': 'Any investment with no corresponding MIS file in the run'},
]

# ── Tolerances (declared in the schema, versioned, reported) ─────────────
TOLERANCES = {
    'hard_abs_cr': Decimal('0.01'),    # hard identities must hold to the ~rupee
    'soft_pct': Decimal('0.15'),       # soft correspondence band (but ALWAYS published)
    'identity_abs_cr': Decimal('0.01'),
    'magnitude_band': Decimal('50'),   # existence: cell within band of its peers
    'moic_min': Decimal('0'), 'moic_max': Decimal('50'),
    'irr_min': Decimal('-0.99'), 'irr_max': Decimal('10'),
}

STALENESS_MONTHS = 6           # period ends older than this are flagged & disclosed
ROUNDING_DP = 4                # one place for rounding (build rule #11)
CONFIDENCE_TAU = 0.55          # classifier review gate


def contract_signature() -> str:
    """Part of the run signature / cache key. Any change to schema, lexicon,
    identities, checks or tolerances changes this, so records built under an old
    contract are never silently reused (U8)."""
    from ..preingest2.schema import schema_signature
    blob = json.dumps({
        'v': CONTRACT_VERSION,
        'schema': schema_signature(),
        'lexicon': {k: sorted(v) for k, v in CONCEPT_LEXICON.items()},
        'identities': [i['name'] for i in IDENTITIES],
        'checks': [(c['id'], c['class']) for c in CHECKS],
        'tolerances': {k: str(v) for k, v in TOLERANCES.items()},
        'staleness': STALENESS_MONTHS, 'rounding': ROUNDING_DP,
    }, sort_keys=True)
    return 'ct_' + hashlib.sha256(blob.encode()).hexdigest()[:12]
