"""
Stage 1 — ONE Gemini call classifies every sheet in the workbook.

Per-sheet output: {domain, layout, column_map}.
No row-level Gemini calls anywhere; Stage 2 handles rows deterministically.
"""
import json
import logging
import time

from ..canonical_schema import DOMAIN_FIELDS
from ..gemini_column_mapper import _call_gemini
from .helpers import find_header_row

logger = logging.getLogger(__name__)


def build_stage1_prompt(workbook_data: dict) -> str:
    sheets = workbook_data['sheets']
    data = workbook_data['data']

    parts: list[str] = []
    for sn in sheets:
        rows = data[sn]['rows']
        hdr_idx = find_header_row(rows)
        if hdr_idx < 0:
            parts.append(f'\nSheet "{sn}": (non-tabular or no clear header)')
            for r in rows[:6]:
                cells = [(i, str(v)[:40]) for i, v in enumerate(r) if v not in (None, '')]
                if cells:
                    parts.append(f'  row: {cells}')
            continue
        header = [str(v).strip() if v is not None else '' for v in rows[hdr_idx]]
        while header and not header[-1]:
            header.pop()
        parts.append(f'\nSheet "{sn}":')
        parts.append(f'  header (row {hdr_idx + 1}): {header}')
        sample = 0
        for r in rows[hdr_idx + 1:]:
            if not any(v not in (None, '') for v in r):
                continue
            trim = [str(r[ci])[:40] if ci < len(r) and r[ci] not in (None, '')
                    else '' for ci in range(len(header))]
            parts.append(f'  sample: {trim}')
            sample += 1
            if sample >= 2:
                break

    sheets_str = '\n'.join(parts)
    fields_by_domain = json.dumps(
        {d: list(DOMAIN_FIELDS[d].keys()) for d in sorted(DOMAIN_FIELDS.keys())},
        indent=2,
    )
    # Domain menu is generated from DOMAIN_FIELDS so it can never drift out of
    # sync with the canonical schema — every domain that has a field dictionary
    # (including investment_tranches, nav_calculation, fund_pl_bs, fees_register)
    # is offered to Gemini as a selectable option.
    domain_menu = ', '.join(sorted(DOMAIN_FIELDS.keys()))

    return f"""You are an Indian AIF (Alternative Investment Fund) data analyst.

For each SHEET below, decide:
  1. domain — the canonical business area it holds. Pick ONE:
     {domain_menu}.
     Use null for Cover/Summary/Index/Dashboard/Overview sheets.
     NOTE: a "Formula" column or a computed appearance does NOT by itself make a
     sheet a summary — a NAV build-up (nav_calculation), a distribution/carry
     waterfall (waterfall_carry), and a fund-level income statement (fund_pl_bs)
     are authoritative source sheets: classify them to their domain. A sheet with
     multiple funding rounds/tranches per company is investment_tranches (NOT
     portfolio_investments).
  2. layout — one of:
       "tabular"     : normal rows x columns table (default)
       "key_value"   : two columns "Parameter | Value" (Fund_Overview style)
       "wide_period" : one row per entity, columns are periods (Apr-24, Q1-25, ...)
                       ONE mapped column carries the value; the period column
                       IS the period.
       "entity_pivoted" : columns are entity IDs (LP001, LP002, ...),
                          rows are attributes (Committed, Called, Distributed).
                          One column typically labelled TOTAL.
  3. column_map — {{ raw_header_text -> canonical_field_name }}. Use the
     canonical field names below.

MAP AGGRESSIVELY. Every column that has a plausible canonical equivalent
should be mapped, even across domains. Examples:
  Commitment(INR Cr)      -> commitment_amount
  Capital_Called          -> cumulative_called
  Distributions_Received  -> cumulative_distributed
  Amount_Invested         -> total_invested
  Cost_of_Investment      -> cost_basis
  Realisation_Date        -> exit_date
  Realised_Amount         -> proceeds
  Gross_Realised          -> total_gross_amount
  Net_Distribution        -> total_net_amount

For key_value sheets (Fund_Overview, FUND_MASTER), Gemini need not map
columns — Python will pivot rows to a dict using the label text itself.

Return JSON only, no markdown:
{{
  "sheets": {{
    "<sheet_name>": {{
      "domain": "<domain_or_null>",
      "layout": "tabular"|"key_value"|"wide_period"|"entity_pivoted",
      "column_map": {{ "<raw_header>": "<canonical_field>" }}
    }}
  }}
}}

Canonical field names available per domain:
  {fields_by_domain}

WORKBOOK:{sheets_str}
"""


def run_stage1(workbook_data: dict, timeout_ms: int = 240_000) -> dict:
    """Call Gemini once to classify every sheet. Returns the parsed JSON dict."""
    prompt = build_stage1_prompt(workbook_data)
    logger.info(f'[phase6.stage1] prompt size: {len(prompt):,} chars '
                f'(~{len(prompt)//4:,} input tokens)')
    t0 = time.time()
    result = _call_gemini(prompt, context_label='phase6.stage1', timeout_ms=timeout_ms)
    elapsed = time.time() - t0
    logger.info(f'[phase6.stage1] Gemini call: {elapsed:.1f}s')
    return result or {}
