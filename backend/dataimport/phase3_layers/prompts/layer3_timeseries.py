"""
Layer 3 — Time-Series prompt.

Extracts: portfolio_kpis_periodic, monthly_pl_rows, monthly_bs_rows,
monthly_cf_rows, budget_vs_actual, burn_runway.

Highest output-token risk layer. Flavor B chunks by company-group
(preferred) or by period-range when one company has too many periods.

TEMPLATING DISCIPLINE: see layer2_universe.py docstring. Multi-line bodies
are plain triple-quoted strings + .replace() sentinels; short conditional
one-liners stay as f-strings for readability.
"""

from ...canonical_schema import BURN_RUNWAY_FIELDS
from .common_rules import COMMON_PREAMBLE, JSON_OUTPUT_CONTRACT


def _vocab(fields: dict) -> str:
    return '\n'.join(f'    - {k}: {desc}' for k, desc in fields.items())


_SCHEMA_TEMPLATE = """
TOP-LEVEL KEYS ALLOWED IN LAYER 3:

  portfolio_kpis_periodic  — array  (one per (company, period) KPI row)
  monthly_pl_rows          — array  (one per source row in monthly P&L)
  monthly_bs_rows          — array  (one per source row in Balance Sheet)
  monthly_cf_rows          — array  (one per source row in Cash Flow)
  budget_vs_actual         — array  (one per (company, period, line_item))
  burn_runway              — array  (one per company SaaS/burn snapshot)
  sheet_completeness       — array  (one per workbook sheet you touched)
  provenance               — object (any aggregate citations)

FIELD VOCABULARIES:

▸ portfolio_kpis_periodic[] (per (company, period) — use SUBSET that company
  actually reports; period values are literal source strings):
    - company_name, period, period_type, currency
    - revenue, cogs, gross_profit, gross_margin_pct, ebitda,
      ebitda_margin_pct, pat, headcount
    - gmv, orders, aov, returns_pct, repeat_pct
    - mrr, arr, nrr, churn_rate, cac, ltv, ltv_cac_ratio,
      burn_rate, runway_months
    - nim_pct, gnpa_pct, nnpa_pct, roe_pct, cost_to_income
    - capacity_utilization, export_pct, debt_to_ebitda
    - bed_occupancy, arpob, cap_rate_pct, aum_value

▸ monthly_pl_rows[] (per (company, period) — Rule 11):
    company_name, period, period_type, currency, revenue,
    other_income, total_revenue, cogs, gross_profit, employee_cost,
    marketing_cost, rd_cost, g_and_a, total_opex, ebitda,
    depreciation, ebit, finance_cost, pbt, tax, pat

▸ monthly_bs_rows[] (per (company, period) — Rule 11):
    company_name, period, period_type, total_assets, current_assets,
    fixed_assets, investments, cash_and_equivalents, receivables,
    inventory, total_liabilities, total_debt, current_liabilities,
    long_term_debt, net_worth, share_capital, reserves

▸ monthly_cf_rows[] (per (company, period) — Rule 11):
    company_name, period, period_type, cash_from_operations,
    cash_from_investing, cash_from_financing, net_cash_change,
    opening_cash, closing_cash, capex, working_capital_change,
    interest_paid, tax_paid

▸ budget_vs_actual[]:
    company_name, period, line_item, budget, actual, variance,
    variance_pct, is_favorable

▸ burn_runway[] (per-company SaaS/burn snapshot):
__VOCAB_BURN_RUNWAY__

▸ sheet_completeness[]: sheet_name, rows_in_source, rows_extracted,
  truncated_in_prompt, target_array

CRITICAL FOR THIS LAYER:
  • Rule 11: row replication is per (company, period). Do NOT collapse.
  • Rule 13: emit one portfolio_kpis_periodic entry per (company, period).
  • Rule 14: derive KPIs (gross_margin_pct from revenue & cogs, etc.) inline.
  • Period values are LITERAL strings as written in source ("Apr-24",
    "FY 2024-25", "Q1 FY25"). Don't normalise; the persister does that.

DO NOT emit (other layers own these):
  fund_master, fund_performance, waterfall, nav_records, investors,
  commitments, capital_calls, distributions, entities, compliance_records,
  portfolio_investments, valuations, exits, quoted_unquoted
"""


def _schema_block() -> str:
    return _SCHEMA_TEMPLATE.replace('__VOCAB_BURN_RUNWAY__', _vocab(BURN_RUNWAY_FIELDS))


_LAYER3_TEMPLATE = """__COMMON_PREAMBLE__

__JSON_OUTPUT_CONTRACT__

LAYER 3 SCOPE: Per-company time-series — KPIs, monthly P&L / BS / CF,
budget-vs-actual, burn & runway.
__CTX_BLOCK____CHUNK_BLOCK__
WORKBOOK CONTENT (only the sheets routed to this layer; if this is a chunk,
only the row slice listed above is included — extract every row you see and
make no assumptions about omitted rows):
__WORKBOOK_TEXT__

__SCHEMA__

Return ONLY the JSON object. No prose, no markdown fences.
"""


def LAYER3_PROMPT_TEMPLATE(workbook_text: str, identity_context: str = '',
                           chunk_filter: str = '') -> str:
    """Build Layer 3 prompt.

    identity_context: Layer 1 fund identity context replicated so this layer
        can reason independently.
    chunk_filter: orchestrator-supplied note describing the row slice this
        chunk is responsible for. Row filtering happens in Python; this is
        only informational so Gemini doesn't extrapolate.
    """
    schema = _schema_block()
    ctx_block = (
        f"\nIDENTITY CONTEXT (from Layer 1 — for reference only, do NOT re-emit):\n{identity_context}\n"
        if identity_context else ''
    )
    chunk_block = f"\nCHUNK SCOPE: {chunk_filter}\n" if chunk_filter else ''
    return (
        _LAYER3_TEMPLATE
        .replace('__COMMON_PREAMBLE__',      COMMON_PREAMBLE)
        .replace('__JSON_OUTPUT_CONTRACT__', JSON_OUTPUT_CONTRACT)
        .replace('__CTX_BLOCK__',            ctx_block)
        .replace('__CHUNK_BLOCK__',          chunk_block)
        .replace('__WORKBOOK_TEXT__',        workbook_text)
        .replace('__SCHEMA__',               schema)
    )
