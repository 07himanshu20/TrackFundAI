"""
Layer 1 — Identity & Fund-Level prompt.

Extracts: fund_master, scheme_terms, entities, investors, commitments,
capital_calls, distributions, nav_records, waterfall, fund_performance,
compliance_records, sheet_completeness.

Layer 1 owns every fund-level scalar metric (NAV, called/distributed totals,
waterfall, performance). Layers 2 and 3 must NOT emit these — only L1 does.

TEMPLATING DISCIPLINE (universal, post-2026-06-30 incident):
  This module's multi-line prompt bodies are PLAIN triple-quoted strings
  (no `f` prefix). Interpolation happens via `.replace('__SENTINEL__', value)`
  so the body content can contain ANY characters — JSON examples, code
  snippets, set notation, currency symbols — without Python interpreting
  them as format specifiers.

  Why: a single un-escaped `{...}` inside an f-string body throws
  `ValueError: Invalid format specifier` at runtime inside parallel worker
  threads, killing every Phase 3 L1 chunk simultaneously. Sentinel
  templating eliminates that entire class of bug.

  Short conditional one-liners (one \\n plus a variable) stay as f-strings
  for readability — they have negligible risk of containing literal braces.
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


_SCHEMA_TEMPLATE = """
═══════════════════════════════════════════════════════════════════════════
UNIVERSAL EXTRACTION PRINCIPLE — applies to every field in every section
═══════════════════════════════════════════════════════════════════════════

  RULE 1 — EXTRACT-FIRST, DERIVE-AS-FALLBACK:
    If a value for a field is explicitly written in any sheet (a labeled
    cell containing a numeric value), EXTRACT it semantically and emit
    that value with provenance = "Sheet!Cell".

    ONLY when a value is genuinely absent from every sheet, derive it via
    a documented formula using OTHER explicit values, and set provenance
    to the formula expression starting with "=" so Python can recognise
    it as derived.

  RULE 2 — A FACT IS A FACT:
    For any field, extraction and correct calculation MUST yield the same
    answer. If they disagree, one of them is wrong (usually misread input
    cell or wrong formula choice). Re-importing the SAME workbook MUST
    produce IDENTICAL numbers. No stochastic re-derivation across runs.

  RULE 3 — NEVER FABRICATE PROVENANCE:
    Do not write a cell reference unless the value you emit equals what is
    actually in that cell. Do not write "(assumed X)" — if you cannot find
    a value, OMIT the field. Do not synthesize terminal NAV cashflows,
    Net NAV, or any aggregate that the workbook does not contain.

  RULE 4 — UNIVERSALITY:
    These rules apply to every fund, every Excel format, every sheet
    layout, and (when this system later ingests data from Tally, SAP, or
    other ERPs) every source. No sheet-name or cell-coordinate is fixed;
    discover them per workbook.

═══════════════════════════════════════════════════════════════════════════

TOP-LEVEL KEYS ALLOWED IN LAYER 1 (omit any you cannot populate):

  fund_master            — object  (fund + scheme identity & lifecycle —
                                    INCLUDES extracted LPA terms: hurdle %,
                                    carry %, mgmt-fee %, inception date)
  workbook_aggregates    — array   (Option C — cell-verified overrides.
                                    Whenever you SEE a labeled aggregate
                                    value in ANY sheet, emit one entry per
                                    label. Python downstream re-reads the
                                    cell to confirm. Universal across any
                                    sheet name / cell position / layout.)
  investors              — array   (one per LP row — EMIT ALL rows)
  commitments            — array   (one per LP commitment — EMIT ALL rows)
  capital_calls          — array   (one per call header — EMIT ALL rows)
  distributions          — array   (one per distribution event header,
                                    INCLUDING interim dividends / GP carry
                                    payouts — EMIT EVERY DISTRIBUTION row)
  nav_records            — array   (one per period in the NAV walk — EMIT ALL)
  waterfall              — object  (extract verified summary if labelled in a
                                    Carry/Waterfall sheet; OMIT every field
                                    you cannot extract from a real cell —
                                    Python will compute deterministically
                                    from atomic ledger rows)
  fund_performance       — object  (fund-level scalars, atomic-extracted only)
  entities               — array   (sponsor / trustee / manager / custodian / auditor)
  compliance_records     — array   (SEBI / regulator filings, calendar events)
  sheet_completeness     — array   (one per workbook sheet you touched)
  provenance             — object  (cell refs for extracted values, formula
                                    expressions for derived values)

ATOMIC-ROW COMPLETENESS IS THE PRIORITY:

  Python downstream computes every fund aggregate (TVPI, DPI, RVPI, MOIC,
  Net IRR, carry base, GP carry gross/net, clawback, preferred return,
  catch-up) from the atomic per-row arrays above (capital_calls[],
  distributions[], nav_records[], investments, valuations). Therefore:

    • Missing a single per-row entry corrupts every downstream aggregate
      that depends on it. EMIT EVERY ROW present in the source sheet.
    • Per-row fields must be extracted from the row's own cells (date,
      amount, type). Do NOT carry values forward or backward across rows.
    • Distribution rows include EVERY type: return_of_capital, dividend,
      interest, STCG, LTCG, carry distribution. Do NOT filter by type.
    • LPA-term cells (hurdle %, carry %, escrow holdback %, inception
      date, investment-period dates, mgmt fee %) MUST be extracted into
      fund_master so Python can read them from Scheme model fields.

FIELD VOCABULARIES:

▸ fund_master (object):
__VOCAB_FUND_MASTER__

▸ investors[] (per-LP):
__VOCAB_INVESTORS__

▸ commitments[] (per-commitment; often same rows as investors):
__VOCAB_COMMITMENTS__

▸ capital_calls[] (per-call header):
__VOCAB_CAPITAL_CALLS__

▸ distributions[] (per-distribution header — use NET amounts, Rule 29):
    scheme_name, distribution_number, distribution_date,
    distribution_type, total_gross_amount, total_tds_amount,
    total_net_amount, gp_carry_amount, distribution_status, source_description

    GP CARRY COMPONENT (universal, per-row):
      If the Distributions sheet has a column labeled "GP Carry Component",
      "Carried Interest Distribution", "Carry Component (Cr)", "GP Carry",
      "Carry to GP", or "GP Share of Distribution", extract that VALUE
      per row into gp_carry_amount. This is the portion of THIS distribution
      paid to the GP as carry (distinct from total_net_amount which is the
      whole event paid out to LPs + GP combined).

      Python downstream uses Σ gp_carry_amount to detect over-distribution
      → clawback. WITHOUT this per-row data the dashboard cannot compute
      clawback / GP holdback / net-after-clawback.

      Leave gp_carry_amount NULL on a row only when the source sheet has
      no such column or that row has no GP carry component.

▸ nav_records[] (per-period — emit ALL periods, sorted ascending by period_end):
__VOCAB_NAV__

▸ waterfall (object) — EXTRACT-FIRST. DO NOT compute. DO NOT assume.

  PRIMARY DIRECTIVE — A fact is a fact:
    If the workbook contains the answer, EXTRACT it verbatim with a cell
    reference. Do not re-derive a value that is already written down in the
    sheet. Every re-import of the same file must produce the exact same
    numbers — that is only possible when values are extracted, not computed
    from stochastic assumptions.

  STEP 1 — SCAN ALL ROUTED SHEETS for these explicit labels (case-insensitive,
  whitespace-tolerant). When found, emit the numeric value verbatim and set
  provenance to the exact cell, e.g. "Carry_Clawback!B37":
    - "Carry Base" / "Total Profit above Capital"          → carry_base
    - "GP Carry Gross" / "GP Carry Entitlement"            → carry_amount_gross
    - "GP Carry Distributed" (incl. over-distribution)     → carry_distributed_gross
    - "Clawback Provision" / "Clawback Required"           → clawback_provision
    - "GP Holdback" / "Escrow Holdback"                    → gp_holdback_escrow
    - "GP Carry Net" (after holdback & clawback)           → net_carry
    - "Preferred Return" / "Hurdle Cleared" total          → preferred_return_amount
    - "GP Catch-Up" / "Catch-Up Amount"                    → step_3_catchup_amount
    - "Return of Capital"                                  → step_1_return_of_capital
    - "LP Share" (Step 4)                                  → step_4a_lp_residual
    - "GP Share" / "GP Residual Carry" (Step 4)            → step_4b_gp_residual_carry

  STEP 2 — SCAN Fund_Overview / Cover for LPA terms. These are ALWAYS in the
  workbook for any real fund. Extract verbatim, never write "assumed":
    - "Carried Interest Rate" / "Carry %"                  → carry_percentage
    - "Hurdle Rate" / "Preferred Return Rate"              → hurdle_rate
    - "Clawback Provision" policy / "% holdback"           → clawback_holdback_pct
    - "Catch-Up Provision" (100% GP / 80:20)               → catchup_provision_type
    - "Distribution Waterfall" (European / American)       → waterfall_type
    - "Fund Inception Date" / "First Close Date"           → (use as inception_date input)

  STEP 3 — ONLY IF a waterfall result field is NOT explicitly written in
  ANY sheet, omit it from the waterfall object. Python (Phase 4) will
  compute it deterministically from the extracted ledgers below.

  STRICT PROHIBITIONS:
    × NEVER write provenance like "assumed 0.20" or "assumed hurdle 0.08".
      If a value is not in the workbook, OMIT THE FIELD. Do not invent.
    × NEVER synthesize a "computed_net_nav" or "terminal NAV" value when
      no Net NAV is in the workbook. Emit fund_nav_latest = null and let
      Python decide.
    × NEVER fabricate a "terminal NAV distribution" cashflow entry in
      net_irr_cashflows. The cashflows array must contain ONLY events that
      actually appear in Capital_Calls / Distributions / NAV sheets.
    × NEVER compute step_2 / step_3 / step_4 yourself. Extract them if
      they are written in a Carry_Clawback / Waterfall / Carry-Summary
      sheet, or OMIT them.

  Always-OK fields (these are flags / totals you may copy from a totals row):
    - total_capital_called (extract from Capital_Calls TOTAL row or
      NAV-walk latest period; provenance = exact cell)
    - total_distributions  (extract from Distributions TOTAL row or
      NAV-walk latest period; provenance = exact cell)
    - carry_status (indicative / crystallised / paid — based on
      sheet text or Fund_Overview reporting status)

  Allowed waterfall keys (omit any you cannot extract):
__VOCAB_WATERFALL__
    - step_1_return_of_capital
    - step_2_preferred_return
    - step_3_catchup_amount
    - step_4a_lp_residual
    - step_4b_gp_residual_carry
    - carry_distributed_gross
    - gp_holdback_escrow
    - net_carry
    - carry_status

▸ fund_performance (object) — fund-level summary. EXTRACT-ONLY.

  HARD RULE — DO NOT COMPUTE. DO NOT DERIVE. DO NOT ASSUME.

  Every aggregate below is RE-COMPUTED by Python downstream from the
  atomic ledgers (capital_calls[], distributions[], nav_records[],
  investments). Python uses the ATOMIC PER-ROW EVENTS as the single
  source of truth. Anything you emit here is used only as a CELL-REF
  override — and only if your provenance is a real cell reference.
  Values without cell-ref provenance are discarded by the persister.

  So: extract a field ONLY if a SPECIFIC CELL in the workbook contains
  that exact aggregate as a labeled number. Otherwise OMIT the field.

  Allowed fields (each must carry provenance = "Sheet!Cell"):
    - as_of_date                     (Fund_Overview "Current Reporting Period" or
                                      NAV-walk latest period)
    - total_committed_capital        (only if a "Total Committed / Final Close Corpus"
                                      cell exists with a numeric value)
    - total_called_capital           (only if a "Total Capital Called" TOTAL cell exists)
    - total_invested_capital         (only if an "Invested Capital" TOTAL cell exists)
    - total_realised_proceeds        (only if a "Realisations Cumulative" cell exists)
    - total_distributions            (only if a "Cumulative Net Distributions" cell
                                      exists; the LATEST PER-EVENT amount is NOT the
                                      cumulative total — never confuse them)
    - total_unrealised_fv_holding    (only if NAV-walk has a "Unrealised FMV" cell;
                                      MUST equal Σ Layer 2 valuations.fair_value_of_holding)
    - fund_nav_latest                (Net NAV — extract ONLY when the NAV-walk's
                                      "Net NAV" column has a NUMERIC value for the
                                      latest period. If blank or labeled "computed by
                                      AI software" → emit null. NEVER fabricate.)
    - fund_units_outstanding         (NAV-walk Units column for latest period)
    - lp_count                       (Fund_Overview "Total LP Count" cell)
    - portfolio_companies            (Fund_Overview "Total Portfolio Companies" cell)
    - accrued_management_fees        (NAV-walk Accrued Mgmt Fee cell, latest period)

  FORBIDDEN — these are 100% Python-derived; do not emit them at all:
    × tvpi, dpi, rvpi, moic, moic_portfolio
    × net_irr_computed, drawdown_pct
    × total_uncalled_capital (Python derives = committed − called)

  net_irr_stated: emit ONLY if a cell in Fund_Overview / Cover EXPLICITLY
    states a net IRR percentage. Otherwise omit. Never derive.

  net_irr_cashflows: FORBIDDEN. Python builds cashflows from
    capital_calls[] and distributions[] directly. Do not emit this array.

▸ workbook_aggregates[] (Option C — cell-verified aggregate overrides):

  PURPOSE: When the source workbook contains a verified labeled aggregate
  value in a specific cell (e.g. "Carry Base = 1,430.60" written by the CA
  in `Fund_Overview!B60`, or the carry summary in `Carry_Clawback`), emit
  ONE entry per labeled aggregate. Python re-reads the exact cell from the
  workbook and verifies your claimed value before accepting the override.

  WHY THIS PATTERN IS UNIVERSAL: You scan the workbook and discover where
  the aggregates live. Cell positions can move, sheet names can change,
  layouts can flip — none of that matters because you (the LLM) find the
  label each time and emit the cell ref you actually saw. Python verifies
  by re-reading that exact ref; no hardcoded coordinates anywhere.

  HARD RULES:
    • Emit ONLY when you actually see a labeled aggregate written down in
      a specific cell. NEVER fabricate a cell ref to legitimise a computed
      value. Python will compare your claimed value against the actual
      cell value; mismatches are rejected and logged.
    • The `cell` field MUST be the cell containing the VALUE (not the cell
      containing the label). If label is in column A and value in column B,
      emit the column B cell.
    • If the same aggregate appears in multiple sheets, emit one entry per
      sheet — Python deduplicates after verification.
    • If the workbook doesn't publish a labeled aggregate for a metric,
      OMIT IT — Python will derive from atomic ledger rows instead.

  Each entry:
    {
      "metric":     "carry_base",                  // canonical name; see list below
      "value":      1430.60,                       // numeric value as you read it
      "sheet":      "Fund_Overview",               // exact sheet name (case-sensitive)
      "cell":       "B60",                         // A1-style cell ref of the VALUE
      "label_text": "Carry Base (Total Profit...)" // optional — for audit
    }

  Canonical `metric` names (use these exact strings):
    - carry_base              : Total profit above capital (carry base)
    - carry_amount_gross      : GP carry entitlement (= carry% × carry_base)
    - carry_distributed_gross : GP carry actually distributed to date (may
                                exceed entitlement → triggers clawback)
    - gp_clawback             : Clawback provision (excess of distributed
                                over entitlement)
    - gp_holdback             : Escrow holdback (% of distributed)
    - carry_amount_net        : Net carry to GP (distributed − holdback − clawback)
    - preferred_return        : Total preferred return accrued
    - gp_catchup              : GP catch-up amount
    - return_of_capital       : Step 1 return-of-capital total
    - total_capital_called    : Cumulative capital called
    - total_distributions     : Cumulative net distributions
    - total_committed_capital : Total LP committed capital
    - total_invested_capital  : Total invested cost in portfolio
    - total_realised_proceeds : Cumulative realised exit proceeds
    - fund_nav_latest         : Latest Net NAV (only if a NUMERIC cell exists)

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


def _schema_block() -> str:
    return (
        _SCHEMA_TEMPLATE
        .replace('__VOCAB_FUND_MASTER__',   _vocab(FUND_SCHEME_MASTER_FIELDS))
        .replace('__VOCAB_INVESTORS__',     _vocab(INVESTORS_AML_FIELDS))
        .replace('__VOCAB_COMMITMENTS__',   _vocab(COMMITMENTS_FIELDS))
        .replace('__VOCAB_CAPITAL_CALLS__', _vocab(CAPITAL_CALLS_FIELDS))
        .replace('__VOCAB_NAV__',           _vocab(NAV_ACCOUNTING_FIELDS))
        .replace('__VOCAB_WATERFALL__',     _vocab(WATERFALL_CARRY_FIELDS))
    )


_LAYER1_TEMPLATE = """__COMMON_PREAMBLE__

__JSON_OUTPUT_CONTRACT__

LAYER 1 SCOPE: Identity, fund-level scalars, capital/distribution ledgers,
NAV walk, waterfall, performance summary, service entities, compliance.

WORKBOOK CONTENT (only the sheets routed to this layer):
__WORKBOOK_TEXT__

__SCHEMA__

Return ONLY the JSON object. No prose, no markdown fences.
"""


def LAYER1_PROMPT_TEMPLATE(workbook_text: str, identity_context: str = '') -> str:
    """Build Layer 1 prompt. identity_context is empty for L1 — it IS the identity layer."""
    schema = _schema_block()
    return (
        _LAYER1_TEMPLATE
        .replace('__COMMON_PREAMBLE__',      COMMON_PREAMBLE)
        .replace('__JSON_OUTPUT_CONTRACT__', JSON_OUTPUT_CONTRACT)
        .replace('__WORKBOOK_TEXT__',        workbook_text)
        .replace('__SCHEMA__',               schema)
    )
