"""
Layer 2 — Investment Universe prompt.

Extracts: portfolio_investments, valuations, tranches, exits, quoted_unquoted.
Per-investment level data. Flavor B chunks this layer by company-range when
estimated output > token budget.
"""

from ...canonical_schema import (
    PORTFOLIO_INVESTMENTS_FIELDS,
    VALUATIONS_KPIS_FIELDS,
    QUOTED_UNQUOTED_FIELDS,
    EXITS_DISTRIBUTIONS_FIELDS,
)
from .common_rules import COMMON_PREAMBLE, JSON_OUTPUT_CONTRACT


def _vocab(fields: dict) -> str:
    return '\n'.join(f'    - {k}: {desc}' for k, desc in fields.items())


def _schema_block() -> str:
    return f"""
TOP-LEVEL KEYS ALLOWED IN LAYER 2:

  portfolio_investments  — array  (one row per ACTUAL investment — Rule 7)
  valuations             — array  (one per (investment, valuation_date) — Rule 26)
  exits                  — array  (one per exit event)
  quoted_unquoted        — array  (one per investment listing status)
  sheet_completeness     — array  (one per workbook sheet you touched)
  provenance             — object (cell refs / formulas for any aggregate)

FIELD VOCABULARIES:

▸ portfolio_investments[] (per-investment — include irr_pct per Rule 21,
  and moic per Rule 24):
{_vocab(PORTFOLIO_INVESTMENTS_FIELDS)}

▸ valuations[] (per (investment, valuation_date); PREFER fair_value_of_holding;
  ALWAYS include cost_basis per Rule 26 for row disambiguation):
{_vocab(VALUATIONS_KPIS_FIELDS)}

▸ exits[] (per exit event):
{_vocab(EXITS_DISTRIBUTIONS_FIELDS)}

▸ quoted_unquoted[] (per investment listing status):
{_vocab(QUOTED_UNQUOTED_FIELDS)}

▸ sheet_completeness[]: sheet_name, rows_in_source, rows_extracted,
  truncated_in_prompt, target_array

CRITICAL FOR THIS LAYER:
  • Apply Rule 4 (FV TRAP): fair_value vs fair_value_of_holding are TWO
    fields on ONE row, not two rows.
  • Apply Rule 22: NEVER store equity value in fair_value_of_holding.
  • Apply Rule 26: one valuation row per investment (not per company).
    cost_basis is MANDATORY on every valuations[] row.
  • Apply Rule 21: every portfolio_investments[] row MUST include irr_pct
    (computed inline if not stated).

DO NOT emit (other layers own these):
  fund_master, fund_performance, waterfall, nav_records, investors,
  commitments, capital_calls, distributions, entities, compliance_records
  (Layer 1 emits these.)
  portfolio_kpis_periodic, monthly_pl_rows, monthly_bs_rows, monthly_cf_rows,
  burn_runway, budget_vs_actual
  (Layer 3 emits these.)
"""


def LAYER2_PROMPT_TEMPLATE(workbook_text: str, identity_context: str = '',
                           chunk_filter: str = '') -> str:
    """Build Layer 2 prompt.

    identity_context: Layer 1 cover/fund-master context replicated so this
        layer can reason independently (e.g., "Fund: XYZ, vintage 2020").
    chunk_filter: when Flavor B is active and the orchestrator has filtered
        rows at the SOURCE (Python-side), this is a short note so Gemini
        knows the workbook excerpt is intentionally partial — extract what
        you see, do not extrapolate missing rows.
    """
    schema = _schema_block()
    ctx_block = f"\nIDENTITY CONTEXT (from Layer 1 — for reference only, do NOT re-emit):\n{identity_context}\n" if identity_context else ''
    chunk_block = f"\nCHUNK SCOPE: {chunk_filter}\n" if chunk_filter else ''
    return f"""{COMMON_PREAMBLE}

{JSON_OUTPUT_CONTRACT}

LAYER 2 SCOPE: Investment universe — portfolio companies, per-investment
tranches, valuations (latest per investment), exits, quoted/unquoted status.
{ctx_block}{chunk_block}
WORKBOOK CONTENT (only the sheets routed to this layer; if this is a chunk,
only the row slice listed above is included — extract every row you see and
make no assumptions about omitted rows):
{workbook_text}

{schema}

Return ONLY the JSON object. No prose, no markdown fences.
"""
