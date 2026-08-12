"""
Stage 0 — Canonical Schema (the foundation).

A single versioned, machine-readable contract that defines the target output:
the list of sheets, and for each sheet the exact columns, their source field,
type, unit, and (for derived sheets) the formula key. Every downstream stage
keys off this. Changing the template = bump SCHEMA_VERSION, never rewrite code.

Grounded in the two reference workbooks (TrackFundAI_Master_Training_v2.xlsx and
consoldiated_fund _data.xlsx).

Column = {header, source, type, unit, required}
  source : canonical extraction field, or None (id/derived-in-assembler slot)
  type   : 'text' | 'num' | 'date' | 'pct' | 'ratio'
  unit   : 'cr' | 'pct' | 'x' | None      (cr => normalize to ₹ Crore)

sheet.mode:
  'table'        one output row per extracted record of `domains`
  'per_company'  one row per portfolio company (Type-B TTM summary)
  'key_value'    Parameter | Value | Note
  'line_item'    Line Item | Amount | Note   (extracted statement verbatim)
  'derived'      rows produced by the formula engine (NAV / Waterfall / Cover)
  'sector_rollup'/'realised_unrealised' — code aggregations
"""

SCHEMA_VERSION = 'schema-1.0.0'


def _c(header, source=None, type='text', unit=None, required=False):
    return {'header': header, 'source': source, 'type': type,
            'unit': unit, 'required': required}


SHEETS = [
    {'key': 'cover', 'name': 'Cover', 'mode': 'derived', 'derive': 'cover_snapshot',
     'title': 'TFAI — Fund Master Workbook (consolidated)'},

    {'key': 'master_inputs', 'name': 'Master_inputs', 'mode': 'key_value',
     'domains': ['fund_master'], 'title': 'Master_inputs — Fund Identity & LPA Terms'},

    {'key': 'lp_register', 'name': 'LP Register', 'mode': 'table',
     'domains': ['lp_register'], 'title': 'LP Register',
     'columns': [
        _c('LP ID'), _c('Limited Partner', 'investor_name', 'text', required=True),
        _c('LP Type', 'investor_type'),
        _c('Commitment (₹Cr)', 'commitment_amount', 'num', 'cr'),
        _c('Cumulative Called (₹Cr)', 'cumulative_called', 'num', 'cr'),
        _c('Cumulative Distributed (₹Cr)', 'cumulative_distributed', 'num', 'cr'),
        _c('% of Fund', None, 'pct')]},

    {'key': 'capital_calls', 'name': 'Capital Calls', 'mode': 'table',
     'domains': ['capital_calls'], 'title': 'Capital Calls',
     'columns': [
        _c('Call No', 'call_number'), _c('Call Date', 'call_date', 'date'),
        _c('Purpose', 'purpose'),
        _c('Total Call Amount (₹Cr)', 'total_call_amount', 'num', 'cr', True),
        _c('Cumulative (₹Cr)', None, 'num', 'cr')]},

    {'key': 'investments', 'name': 'Investments', 'mode': 'table',
     'domains': ['investments'], 'title': 'Investments',
     'columns': [
        _c('Inv ID', 'inv_id'), _c('Portfolio Company', 'company_name', 'text', required=True),
        _c('Sector', 'sector'), _c('Geography', 'geography'), _c('Stage', 'stage'),
        _c('First Invest Date', 'investment_date', 'date'),
        _c('Total Invested (₹Cr)', 'total_invested', 'num', 'cr'),
        _c('Ownership %', 'ownership_pct', 'pct'),
        _c('Instrument', 'instrument_type'), _c('Source MIS File', '__source_file__')]},

    {'key': 'investment_tranches', 'name': 'Investment Tranches', 'mode': 'table',
     'domains': ['tranches'], 'title': 'Investment Tranches',
     'columns': [
        _c('Tranche', 'tranche_number'), _c('Inv ID', 'inv_id'),
        _c('Portfolio Company', 'company_name', 'text', required=True),
        _c('Date', 'tranche_date', 'date'),
        _c('Amount (₹Cr)', 'tranche_amount', 'num', 'cr', True),
        _c('Instrument', 'instrument_type')]},

    {'key': 'valuations', 'name': 'Valuations', 'mode': 'table',
     'domains': ['valuations'], 'title': 'Valuations',
     'columns': [
        _c('Inv ID', 'inv_id'), _c('Portfolio Company', 'company_name', 'text', required=True),
        _c('Val Date', 'valuation_date', 'date'),
        _c('Cost (₹Cr)', 'cost_basis', 'num', 'cr'),
        _c('Fair Value of Holding (₹Cr)', 'fair_value_of_holding', 'num', 'cr'),
        _c('Methodology', 'methodology'),
        _c('Gross MOIC', 'moic', 'ratio', 'x'), _c('Gross IRR %', 'irr_pct', 'pct')]},

    {'key': 'distributions', 'name': 'Distributions', 'mode': 'table',
     'domains': ['distributions'], 'title': 'Distributions',
     'columns': [
        _c('Dist ID', 'distribution_number'), _c('Date', 'distribution_date', 'date'),
        _c('Type', 'distribution_type'),
        _c('Gross Amount (₹Cr)', 'total_gross_amount', 'num', 'cr'),
        _c('Net Amount (₹Cr)', 'total_net_amount', 'num', 'cr'),
        _c('GP Carry Amount (₹Cr)', 'gp_carry_amount', 'num', 'cr'),
        _c('Source', '__source_file__')]},

    {'key': 'exit_events', 'name': 'Exit Events', 'mode': 'table',
     'domains': ['exits'], 'title': 'Exit Events',
     'columns': [
        _c('Exit ID', None), _c('Inv ID', 'inv_id'),
        _c('Portfolio Company', 'company_name', 'text', required=True),
        _c('Exit Date', 'exit_date', 'date'), _c('Exit Type', 'exit_type'),
        _c('Cost Realised (₹Cr)', 'cost_basis', 'num', 'cr'),
        _c('Gross Proceeds (₹Cr)', 'proceeds', 'num', 'cr'),
        _c('Net Exit Proceeds (₹Cr)', 'net_exit_proceeds', 'num', 'cr'),
        _c('Realised Gain (₹Cr)', 'realized_gain_loss', 'num', 'cr'),
        _c('IRR on Exit %', 'irr_pct', 'pct')]},

    {'key': 'realised_unrealised', 'name': 'Realised & Unrealised',
     'mode': 'realised_unrealised', 'title': 'Realised & Unrealised',
     'columns': [
        _c('Inv ID', 'inv_id'), _c('Portfolio Company', 'company_name'),
        _c('Cost (₹Cr)', 'cost', 'num', 'cr'),
        _c('Realised Proceeds (₹Cr)', 'realised', 'num', 'cr'),
        _c('Residual FV / Unrealised (₹Cr)', 'residual', 'num', 'cr'),
        _c('Total Value (₹Cr)', 'total_value', 'num', 'cr'),
        _c('Gross Mult.', 'gross_mult', 'ratio', 'x'), _c('% Realised', 'pct_realised', 'pct')]},

    {'key': 'portfolio_companies', 'name': 'Portfolio_KPI', 'mode': 'per_company',
     'title': 'Portfolio_KPI — Operating Metrics from Company MIS',
     'columns': [
        _c('Inv ID', 'inv_id'), _c('Company', 'company', 'text', required=True),
        _c('Sector', 'sector'), _c('Latest Mo.', 'latest_period'),
        _c('Reporting Ccy', 'currency'),
        _c('Revenue (TTM, ₹Cr)', 'revenue_ttm', 'num', 'cr'),
        _c('EBITDA/PBT (TTM, ₹Cr)', 'ebitda_ttm', 'num', 'cr'),
        _c('Cash Balance (₹Cr)', 'cash_balance', 'num', 'cr'),
        _c('Head-count', 'headcount', 'num'),
        _c('Fund FV (₹Cr)', 'fund_fv', 'num', 'cr'), _c('MIS note', 'note')]},

    {'key': 'sector_allocation', 'name': 'Sector Allocation', 'mode': 'sector_rollup',
     'title': 'Sector Allocation',
     'columns': [
        _c('Sector', 'sector'), _c('# Cos', 'n_cos', 'num'),
        _c('Cost (₹Cr)', 'cost', 'num', 'cr'),
        _c('Fair Value (₹Cr)', 'fair_value', 'num', 'cr'),
        _c('Total Value (₹Cr)', 'total_value', 'num', 'cr'),
        _c('MOIC', 'moic', 'ratio', 'x'), _c('% of FV', 'pct_fv', 'pct')]},

    {'key': 'fees', 'name': 'Fees', 'mode': 'line_item',
     'domains': ['fees'], 'title': 'Fees'},

    {'key': 'nav_calculation', 'name': 'NAV Calculation', 'mode': 'derived',
     'derive': 'nav_buildup', 'title': 'NAV Calculation'},

    {'key': 'waterfall', 'name': 'Waterfall', 'mode': 'derived',
     'derive': 'waterfall', 'title': 'Waterfall (European Whole-Fund)'},

    {'key': 'fund_pl', 'name': 'Fund P&L', 'mode': 'line_item',
     'domains': ['fund_pl'], 'title': 'Fund P&L'},

    {'key': 'budget_vs_actual', 'name': 'Budget vs Actual', 'mode': 'table',
     'domains': ['budget_vs_actual'], 'fund_only': True, 'title': 'Budget vs Actual',
     'columns': [
        _c('Line Item', 'line_item'), _c('Budget (₹Cr)', 'budget_amount', 'num', 'cr'),
        _c('Actual (₹Cr)', 'actual_amount', 'num', 'cr'),
        _c('Variance (₹Cr)', None, 'num', 'cr'), _c('Var %', None, 'pct')]},

    {'key': 'sebi_compliance', 'name': 'SEBI Compliance', 'mode': 'table',
     'domains': ['compliance'], 'row_filter': 'compliance_check', 'title': 'SEBI Compliance',
     'columns': [
        _c('#', None), _c('Requirement', 'check_description'),
        _c('SEBI Reference', 'regulation_reference'), _c('Applicable Norm / Limit', 'compliance_type'),
        _c('Status', 'check_status'), _c('Remarks', 'evidence')]},

    {'key': 'sebi_calendar', 'name': 'SEBI Calendar', 'mode': 'table',
     'domains': ['compliance'], 'row_filter': 'calendar', 'title': 'SEBI Calendar',
     'columns': [
        _c('#', None), _c('Activity / Filing', 'calendar_title'),
        _c('Frequency', 'filing_frequency'), _c('Authority / Reference', 'regulation_reference'),
        _c('Period', 'reporting_period'), _c('Due Date', 'due_date', 'date'),
        _c('Status', 'calendar_status')]},
]

# Canonical DOMAINS the extractor produces (fund files) and the fields per domain.
# The classifier's `target_sheets`/`subtype` route a source sheet to one of these.
DOMAINS = {
    'fund_master': 'Fund identity, LPA terms, NAV component inputs (key-value).',
    'lp_register': 'LP commitment register: investor, type, commitment, called, distributed.',
    'capital_calls': 'Drawdown ledger: call number/date/amount/purpose.',
    'investments': 'Portfolio master: one row per company (id, sector, stage, ownership).',
    'tranches': 'Deployment tranches: per-tranche company/date/amount/instrument.',
    'valuations': 'Fair value per holding: cost, FV, methodology, MOIC, IRR.',
    'distributions': 'Distributions to LPs: gross/net/carry/type/date.',
    'exits': 'Realised exits: proceeds, net proceeds, gain, exit IRR.',
    'fees': 'Management fee schedule by year/period.',
    'fund_pl': 'Fund-level income statement / accounts (line items).',
    'budget_vs_actual': 'Fund-level budget vs actual line items.',
    'compliance': 'SEBI compliance checks and filing calendar.',
    'company_mis': 'A single operating company monthly MIS (P&L/BS/CF/KPIs).',
}


import re as _re
_SUMMARY_RE = _re.compile(r'^\s*(grand\s+)?(total|subtotal|sub-total|sum|net\s+total)\b', _re.I)


def is_summary_label(name) -> bool:
    """True for a roll-up label like 'Total deployed' / 'Grand Total' that must
    not be counted as an entity row (prevents double-counting summary rows that
    the generic row reader let through). Universal — matches any leading total/
    subtotal/sum, not a specific sheet."""
    if name in (None, ''):
        return False
    return bool(_SUMMARY_RE.match(str(name)))


def sheet_by_key(key):
    for s in SHEETS:
        if s['key'] == key:
            return s
    return None


def schema_signature():
    """Part of the pipeline_version determinism key — changes when the schema
    changes so cached records built against an old schema are not reused."""
    import hashlib
    import json
    blob = json.dumps({'v': SCHEMA_VERSION,
                       'sheets': [(s['key'], s.get('mode'),
                                   [c['header'] for c in s.get('columns', [])])
                                  for s in SHEETS]},
                      sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]
