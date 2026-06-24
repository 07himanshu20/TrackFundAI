"""
Layer 1 — Identity & Fund-Level prompt.

Extracts: fund_master, scheme_terms, entities, investors, commitments,
capital_calls, distributions, nav_records, waterfall, fund_performance,
compliance_records, sheet_completeness.

Layer 1 owns every fund-level scalar metric (NAV, called/distributed totals,
waterfall, performance). Layers 2 and 3 must NOT emit these — only L1 does.
"""

from ...canonical_schema import (
    FUND_SCHEME_MASTER_FIELDS,
    INVESTORS_AML_FIELDS,
    COMMITMENTS_FIELDS,
    CAPITAL_CALLS_FIELDS,
    NAV_ACCOUNTING_FIELDS,
    EXITS_DISTRIBUTIONS_FIELDS,
    WATERFALL_CARRY_FIELDS,
)
from .common_rules import COMMON_PREAMBLE, JSON_OUTPUT_CONTRACT


def _vocab(fields: dict) -> str:
    return '\n'.join(f'    - {k}: {desc}' for k, desc in fields.items())


def _schema_block() -> str:
    return f"""
TOP-LEVEL KEYS ALLOWED IN LAYER 1 (omit any you cannot populate):

  fund_master            — object  (fund + scheme identity & lifecycle)
  investors              — array   (one per LP row)
  commitments            — array   (one per LP commitment)
  capital_calls          — array   (one per call header)
  distributions          — array   (one per distribution event header)
  nav_records            — array   (one per period in the NAV walk — EMIT ALL)
  waterfall              — object  (European waterfall summary, computed inline)
  fund_performance       — object  (fund-level summary metrics)
  entities               — array   (sponsor / trustee / manager / custodian / auditor)
  compliance_records     — array   (SEBI / regulator filings, calendar events)
  sheet_completeness     — array   (one per workbook sheet you touched)
  provenance             — object  (cell refs + formulas for every aggregate)

FIELD VOCABULARIES:

▸ fund_master (object):
{_vocab(FUND_SCHEME_MASTER_FIELDS)}

▸ investors[] (per-LP):
{_vocab(INVESTORS_AML_FIELDS)}

▸ commitments[] (per-commitment; often same rows as investors):
{_vocab(COMMITMENTS_FIELDS)}

▸ capital_calls[] (per-call header):
{_vocab(CAPITAL_CALLS_FIELDS)}

▸ distributions[] (per-distribution header — use NET amounts, Rule 29):
    scheme_name, distribution_number, distribution_date,
    distribution_type, total_gross_amount, total_tds_amount,
    total_net_amount, distribution_status, source_description

▸ nav_records[] (per-period — emit ALL periods, sorted ascending by period_end):
{_vocab(NAV_ACCOUNTING_FIELDS)}

▸ waterfall (object) — European waterfall summary (Rule 5 + Rule 23):
{_vocab(WATERFALL_CARRY_FIELDS)}
    - step_1_return_of_capital
    - step_2_preferred_return        (LP_called × ((1+hurdle)^years − 1) — Rule 20)
    - step_2_years_compounded
    - step_3_catchup_amount
    - step_4a_lp_residual
    - step_4b_gp_residual_carry
    - available_after_roc_and_pref   (≡ carry_base — Rule 28)
    - carry_status                   (indicative / crystallised / paid)

▸ fund_performance (object) — fund-level summary:
    - as_of_date
    - total_committed_capital, total_called_capital, total_uncalled_capital
    - total_invested_capital, total_realised_proceeds, total_distributions
    - total_unrealised_fv_holding    (≡ Σ valuations.fair_value_of_holding from Layer 2)
    - fund_nav_latest                (Net NAV — Rule 25 hard guards apply)
    - fund_units_outstanding
    - moic_portfolio, tvpi, dpi, rvpi
    - net_irr_stated                 (omit if not explicitly in source)
    - net_irr_cashflows              (Rule 19 — include terminal NAV entry)
    - accrued_management_fees
    - portfolio_companies            (count of distinct companies — Rule 33d)
    - lp_count, sectors_covered

▸ entities[] (per service entity):
    entity_type (sponsor / trustee / investment_manager / custodian / auditor /
                 legal_counsel / registrar / valuer),
    entity_name, pan, gstin, sebi_registration, contact_email, contact_phone

▸ compliance_records[]:
    fund_name, scheme_name, report_type, compliance_type, calendar_title,
    due_date, filing_status, calendar_status, filed_date, completed_date,
    regulation_reference, calendar_notes

▸ sheet_completeness[] (one per sheet you used in this layer):
    sheet_name, rows_in_source, rows_extracted, truncated_in_prompt,
    target_array

▸ provenance (object) — Rule 32 — cell refs / formula expressions for
  every aggregate in fund_performance + waterfall, keyed by field name.

DO NOT emit these (other layers handle them — would be discarded by merger):
  portfolio_investments, valuations, exits, quoted_unquoted, tranches
  (Layer 2 emits these.)
  portfolio_kpis_periodic, monthly_pl_rows, monthly_bs_rows, monthly_cf_rows,
  burn_runway, budget_vs_actual
  (Layer 3 emits these.)
"""


def LAYER1_PROMPT_TEMPLATE(workbook_text: str, identity_context: str = '') -> str:
    """Build Layer 1 prompt. identity_context is empty for L1 — it IS the identity layer."""
    schema = _schema_block()
    return f"""{COMMON_PREAMBLE}

{JSON_OUTPUT_CONTRACT}

LAYER 1 SCOPE: Identity, fund-level scalars, capital/distribution ledgers,
NAV walk, waterfall, performance summary, service entities, compliance.

WORKBOOK CONTENT (only the sheets routed to this layer):
{workbook_text}

{schema}

Return ONLY the JSON object. No prose, no markdown fences.
"""
