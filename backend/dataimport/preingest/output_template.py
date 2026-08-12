"""
The FIXED output contract — the TrackFundAI Fund Master Workbook.

This is the ONLY fixed part of the pre-ingestion layer, and deliberately so:
the user requires the consolidated file to look exactly like TFAI.xlsx. Every
other stage is universal (Gemini decides all semantics from the actual sheet
content; Python performs deterministic data operations). Here we only declare
WHAT the output looks like — the sheet list, their order, and each sheet's
columns — mapping each column to a CANONICAL FIELD NAME produced by the
existing universal extractor (DOMAIN_FIELDS). Nothing here inspects a filename,
a source sheet name, or a source column name.

Column spec grammar per sheet:
  mode = 'table'       rows come from canonical records of `source_domains`,
                       one output row per record, columns mapped by canonical field.
  mode = 'per_company' one row per portfolio company (Type-B summary rows).
  mode = 'key_value'   Parameter | Value | Note — from key_value canonical records.
  mode = 'line_item'   Line Item | Amount | ... — extracted verbatim from a
                       source statement sheet (NAV / Waterfall / Fund P&L / Fees).

`columns` = list of (display_header, canonical_field_or_None). None means the
column is left blank at extraction time (an id/derived slot the dashboard or a
later confirmed step fills) — we never fabricate it.
"""

# Order matters — this is the exact tab order of the output workbook.
SHEETS = [
    {
        'key': 'cover', 'name': 'Cover', 'mode': 'snapshot',
        'title': 'TFAI — Fund Master Workbook (consolidated)',
        'source_domains': ['fund_scheme_master'],
    },
    {
        'key': 'master_inputs', 'name': 'Master_inputs', 'mode': 'key_value',
        'title': 'Master_inputs — Fund Identity, LPA Terms & Component Inputs',
        'source_domains': ['fund_scheme_master'],
        'columns': [('Parameter', None), ('Value', None), ('Basis / Note', None)],
    },
    {
        'key': 'lp_register', 'name': 'LP Register', 'mode': 'table',
        'title': 'LP Register',
        'source_domains': ['investors_aml', 'commitments', 'lp_capital_accounts'],
        'columns': [
            ('LP ID', None),
            ('Limited Partner', 'investor_name'),
            ('LP Type', 'investor_type'),
            ('Commitment (₹Cr)', 'commitment_amount'),
            ('Cumulative Called (₹Cr)', 'cumulative_called'),
            ('Cumulative Distributed (₹Cr)', 'cumulative_distributed'),
            ('% of Fund', None),
        ],
    },
    {
        'key': 'capital_calls', 'name': 'Capital Calls', 'mode': 'table',
        'title': 'Capital Calls',
        'source_domains': ['capital_calls'],
        'columns': [
            ('Call No', 'call_number'),
            ('Call Date', 'call_date'),
            ('Purpose', 'purpose'),
            ('Total Call Amount (₹Cr)', 'total_call_amount'),
            ('Preferred Return Accrued (₹Cr)', None),
        ],
    },
    {
        'key': 'investments', 'name': 'Investments', 'mode': 'table',
        'title': 'Investments',
        'source_domains': ['portfolio_investments'],
        'columns': [
            ('Inv ID', 'company_cin'),
            ('Portfolio Company', 'company_name'),
            ('Sector', 'sector'),
            ('Geography', 'headquarters_country'),
            ('Stage', 'stage'),
            ('First Invest Date', 'investment_date'),
            # invested amount may be a direct column OR live only in the tranche
            # sheet — accept any canonical synonym, else derive by summing the
            # company's tranches (universal: works whether or not a cost column
            # exists on the investments sheet).
            ('Total Invested (₹Cr)',
             {'any': ['total_invested', 'cost_basis', 'amount'],
              'derive': 'tranche_sum', 'by': 'company_name'}),
            ('Ownership %', 'ownership_pct'),
            ('Sector Tag', 'sub_sector'),
            ('Source MIS File', '__source_file__'),
        ],
    },
    {
        'key': 'investment_tranches', 'name': 'Investment Tranches', 'mode': 'table',
        'title': 'Investment Tranches',
        'source_domains': ['investment_tranches'],
        'columns': [
            ('Tranche', 'tranche_number'),
            ('Inv ID', 'company_cin'),
            ('Portfolio Company', 'company_name'),
            ('Date', 'tranche_date'),
            ('Amount (₹Cr)', 'tranche_amount'),
            ('Instrument', 'instrument_type'),
        ],
    },
    {
        'key': 'valuations', 'name': 'Valuations', 'mode': 'table',
        'title': 'Valuations',
        'source_domains': ['valuations_kpis'],
        'columns': [
            ('Val ID', None),
            ('Inv ID', None),
            ('Portfolio Company', 'company_name'),
            ('Val Date', 'valuation_date'),
            ('Cost (₹Cr)', 'cost_basis'),
            ('Fair Value of Holding (₹Cr)', 'fair_value_of_holding'),
            ('Methodology', 'methodology'),
            ('Gross MOIC', 'multiple'),
            ('Gross IRR %', 'irr_pct'),
        ],
    },
    {
        'key': 'distributions', 'name': 'Distributions', 'mode': 'table',
        'title': 'Distributions',
        'source_domains': ['exits_distributions'],
        'row_filter': 'distribution',   # keep rows that carry distribution fields
        'columns': [
            ('Dist ID', 'distribution_number'),
            ('Date', 'distribution_date'),
            ('Type', 'distribution_type'),
            ('Gross Amount (₹Cr)', 'total_gross_amount'),
            ('Net Amount (₹Cr)', 'total_net_amount'),
            ('GP Carry Amount (₹Cr)', 'gp_carry_amount'),
            ('Source', '__source_file__'),
        ],
    },
    {
        'key': 'exit_events', 'name': 'Exit Events', 'mode': 'table',
        'title': 'Exit Events',
        'source_domains': ['exits_distributions'],
        'row_filter': 'exit',           # keep rows that carry exit fields
        'columns': [
            ('Exit ID', None),
            ('Inv ID', None),
            ('Portfolio Company', 'company_name'),
            ('Exit Date', 'exit_date'),
            ('Exit Type', 'exit_type'),
            ('Cost Realised (₹Cr)', 'cost_basis'),
            ('Gross Proceeds (₹Cr)', 'proceeds'),
            ('Net Exit Proceeds (₹Cr)', 'net_exit_proceeds'),
            ('Realised Gain (₹Cr)', 'realized_gain_loss'),
            ('IRR on Exit %', 'irr_pct'),
        ],
    },
    {
        'key': 'realised_unrealised', 'name': 'Realised & Unrealised', 'mode': 'table',
        'title': 'Realised & Unrealised',
        'source_domains': ['exits_distributions', 'valuations_kpis'],
        'columns': [
            ('Inv ID', None),
            ('Portfolio Company', 'company_name'),
            ('Cost (₹Cr)', 'cost_basis'),
            ('Realised Proceeds (₹Cr)', 'proceeds'),
            ('Residual FV / Unrealised (₹Cr)', 'fair_value_of_holding'),
            ('Total Value (₹Cr)', None),
            ('Gross Mult.', None),
            ('% Realised', None),
        ],
    },
    {
        'key': 'portfolio_companies', 'name': 'Portfolio_KPI', 'mode': 'per_company',
        'title': 'Portfolio_KPI — Operating Metrics from Company MIS',
        'columns': [
            ('Inv ID', 'inv_id'),
            ('Company', 'company'),
            ('Sector', 'sector'),
            ('Latest Mo.', 'latest_period'),
            ('Reporting Ccy', 'currency'),
            ('Revenue (local, TTM)', 'revenue_ttm'),
            ('EBITDA/PBT (local, TTM)', 'ebitda_ttm'),
            ('Cash Balance (local)', 'cash_balance'),
            ('Head-count', 'headcount'),
            ('Fund FV (₹Cr)', 'fund_fv'),
            ('MIS note', 'note'),
        ],
    },
    {
        'key': 'sector_allocation', 'name': 'Sector Allocation', 'mode': 'sector_rollup',
        'title': 'Sector Allocation',
        'source_domains': ['portfolio_investments', 'valuations_kpis'],
        'columns': [
            ('Sector', 'sector'),
            ('Cost (₹Cr)', 'cost'),
            ('Fair Value (₹Cr)', 'fair_value'),
            ('Total Value (₹Cr)', 'total_value'),
            ('MOIC', 'moic'),
            ('% of FV', 'pct_fv'),
        ],
    },
    {
        'key': 'fees', 'name': 'Fees', 'mode': 'line_item',
        'title': 'Fees', 'source_domains': ['fees_register'],
    },
    {
        'key': 'nav_calculation', 'name': 'NAV Calculation', 'mode': 'line_item',
        'title': 'NAV Calculation', 'source_domains': ['nav_calculation', 'nav_accounting'],
    },
    {
        'key': 'waterfall', 'name': 'Waterfall', 'mode': 'line_item',
        'title': 'Waterfall', 'source_domains': ['waterfall_carry'],
    },
    {
        'key': 'fund_pl', 'name': 'Fund P&L', 'mode': 'line_item',
        'title': 'Fund P&L', 'source_domains': ['fund_pl_bs'],
    },
    {
        'key': 'budget_vs_actual', 'name': 'Budget vs Actual', 'mode': 'table',
        'title': 'Budget vs Actual',
        'source_domains': ['financials_pl_bva'],
        'fund_only': True,   # only fund-level BvA; company BvA collapses to Portfolio_KPI
        'columns': [
            ('Line Item', 'line_item'),
            ('Budget (₹Cr)', 'budget_amount'),
            ('Actual (₹Cr)', 'actual_amount'),
            ('Variance (₹Cr)', None),
            ('Var %', None),
        ],
    },
    {
        'key': 'sebi_compliance', 'name': 'SEBI Compliance', 'mode': 'table',
        'title': 'SEBI Compliance',
        'source_domains': ['compliance'],
        'row_filter': 'compliance_check',
        'columns': [
            ('#', None),
            ('Requirement', 'check_description'),
            ('SEBI Reference', 'regulation_reference'),
            ('Applicable Norm / Limit', 'compliance_type'),
            ('Status', 'check_status'),
            ('Remarks', 'evidence'),
        ],
    },
    {
        'key': 'sebi_calendar', 'name': 'SEBI Calendar', 'mode': 'table',
        'title': 'SEBI Calendar',
        'source_domains': ['compliance'],
        'row_filter': 'calendar',
        'columns': [
            ('#', None),
            ('Activity / Filing', 'calendar_title'),
            ('Frequency', 'filing_frequency'),
            ('Authority / Reference', 'regulation_reference'),
            ('Period', 'reporting_period'),
            ('Due Date', 'due_date'),
            ('Status', 'calendar_status'),
        ],
    },
]

# Every canonical field a `table`/`per_company` column can pull from, mapped to
# the target sheet(s) that consume it — used only for validation/inspection.
def sheet_by_key(key):
    for s in SHEETS:
        if s['key'] == key:
            return s
    return None
