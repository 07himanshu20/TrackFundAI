"""
Stage 3 — assemble the persister-shaped unified_json from per-sheet extractions.

The persister expects a specific top-level key structure:
    fund_master, waterfall, fund_performance, workbook_aggregates,
    investors, commitments, capital_calls, distributions,
    portfolio_investments, valuations, exits, nav_records,
    compliance_records, quoted_unquoted, portfolio_kpis_periodic,
    monthly_pl_rows, monthly_bs_rows, monthly_cf_rows, budget_vs_actual,
    burn_runway, entities, sheet_completeness, __source_filepath__

This module is where all shape-translation happens — extractors emit domain-
agnostic dicts; here we route them into the correct top-level bucket and
translate label slugs → canonical persister field names.
"""
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .coercers import extract_pct


# ── Fund-level P&L pivot (Fix C) ────────────────────────────────────────────
# Sentinel PortfolioCompany name used to attach fund-level KPIs (Monthly P&L,
# BvA, aggregate metrics) to the existing PortfolioKPI table. The persister
# auto-creates a real PortfolioCompany row with is_aggregate=True the first
# time it sees this name; the custom manager hides that row from every
# user-facing query. Universal — every fund that publishes fund-level P&L
# lands in the same slot; no per-fund configuration.
FUND_AGGREGATE_SENTINEL = '__FUND_PORTFOLIO_AGGREGATE__'

# Universal P&L line-item label → canonical KPI field name.
# The persister's derivation block (revenue - cogs - opex → EBITDA + margins)
# consumes these canonical field names, so once we pivot line_item rows onto
# these fields the existing derivation ladder runs unchanged.
_PL_LINE_ITEM_ALIAS: dict[str, str] = {
    # Revenue
    'revenue': 'revenue',
    'portfolio revenue': 'revenue',
    'total revenue': 'revenue',
    'net revenue': 'revenue',
    'net sales': 'revenue',
    'gross revenue': 'revenue',
    'top line': 'revenue',
    'turnover': 'revenue',
    'sales': 'revenue',
    'operating revenue': 'revenue',
    # COGS
    'cogs': 'cogs',
    'portfolio cogs': 'cogs',
    'cost of goods sold': 'cogs',
    'cost of revenue': 'cogs',
    'cost of sales': 'cogs',
    'direct cost': 'cogs',
    'direct costs': 'cogs',
    # Gross profit — "gross margin" alone is ambiguous with the ratio
    # (Gross Margin %), so we only accept the unambiguous "gross profit" here.
    'gross profit': 'gross_profit',
    # Operating expenses
    'r d cost': 'rd_cost',
    'r and d': 'rd_cost',
    'r and d cost': 'rd_cost',
    'research and development': 'rd_cost',
    'rnd': 'rd_cost',
    'marketing cost': 'marketing_cost',
    's and m': 'marketing_cost',
    's and m cost': 'marketing_cost',
    'sales and marketing': 'marketing_cost',
    'sales marketing': 'marketing_cost',
    'sm cost': 'marketing_cost',
    'g and a': 'g_and_a',
    'g and a cost': 'g_and_a',
    'general and admin': 'g_and_a',
    'general and administration': 'g_and_a',
    'sga': 'g_and_a',
    # EBITDA / Depreciation / PAT
    'ebitda': 'ebitda',
    'operating profit': 'ebitda',
    'operating income': 'ebitda',
    'depreciation': 'depreciation',
    'depreciation and amortisation': 'depreciation',
    'depreciation and amortization': 'depreciation',
    'd and a': 'depreciation',
    'pat': 'pat',
    'net income': 'pat',
    'net profit': 'pat',
    'profit after tax': 'pat',
    'bottom line': 'pat',
    # Balance-sheet + cash lines that also appear on portfolio-level BS/CF sheets
    'total assets': 'total_assets',
    'total debt': 'total_debt',
    'cash and equivalents': 'cash_balance',
    'cash balance': 'cash_balance',
    'net worth': 'net_worth',
}


# Canonical fields that represent COSTS / EXPENSES. Sheets in accounting
# convention publish these as negative numbers (visual subtraction from the
# preceding subtotal). We store the mathematical magnitude — always positive
# — so the persister's derivation ladder (rev - cogs → gross_profit) yields
# the right sign. Applied at pivot time in _pivot_fund_level_pl. Universal.
_COST_FIELDS = {
    'cogs', 'rd_cost', 'marketing_cost', 'g_and_a', 'depreciation',
    'tax', 'finance_cost',
}

# Longest-alias-first list — used for substring matching so labels like
# "(-) COGS / Cost of Services" resolve to `cogs` (not to a partial hit
# on `revenue` embedded in "cost of revenue"). Rebuilt on module import.
_PL_ALIAS_KEYS_LONGEST_FIRST = sorted(
    _PL_LINE_ITEM_ALIAS.keys(), key=len, reverse=True,
)


def _canon_pl_line_item(text: Any) -> str | None:
    """Normalise a P&L line-item label and look up its canonical field name.

    Universal — case-insensitive, punctuation-insensitive, unit-suffix stripped.
    Percentage-suffixed rows (Gross Margin %, EBITDA Margin) are intentionally
    skipped: the persister re-derives those ratios from the amount inputs, and
    accepting the pre-computed % here would double-count and mis-map the value.
    Returns None when the label doesn't match a known P&L / BS line item; the
    caller then leaves the row alone (no fabrication).
    """
    if text is None:
        return None
    raw = str(text)
    # Percentage rows are derived quantities — skip.
    if '%' in raw:
        return None
    s = raw.lower()
    s = re.sub(r'\([^)]*\)', ' ', s)          # strip "(Cr)", "(₹Cr)", "(-)" etc.
    s = re.sub(r'\s*&\s*', ' and ', s)        # 'D&A' → 'd and a' (before punctuation strip)
    s = re.sub(r'[^a-z0-9\s]+', ' ', s)       # keep letters/digits/spaces
    s = re.sub(r'\s+', ' ', s).strip()
    if not s:
        return None
    # Ratio-flavoured labels (e.g. "EBITDA Margin", "Gross Margin", "Debt to
    # Equity Ratio") are always derived percentages; skip so we don't
    # accidentally store a fraction (0.221 = 22.1%) as an amount field.
    if re.search(r'\bmargin\b|\bratio\b', s):
        return None
    # Exact match first — cheapest and unambiguous.
    exact = _PL_LINE_ITEM_ALIAS.get(s)
    if exact:
        return exact
    # Longest-alias-first substring match — resolves accounting-style labels
    # like "less cogs cost of services" or "gross profit before depreciation"
    # without falling back to a shorter-alias false positive.
    for alias in _PL_ALIAS_KEYS_LONGEST_FIRST:
        if ' ' in alias:
            if alias in s:
                return _PL_LINE_ITEM_ALIAS[alias]
        else:
            # Single-word alias — require token boundary so 'revenue' doesn't
            # match inside 'cost of revenue' (which should map to cogs).
            if re.search(rf'\b{re.escape(alias)}\b', s):
                return _PL_LINE_ITEM_ALIAS[alias]
    return None


def _pivot_fund_level_pl(pl_rows: list[dict]) -> list[dict]:
    """Group fund-level P&L rows by period → one dict per period with canonical
    field names, ready for the KPI persister's derivation ladder.

    Fund-level rows are identified by:
      • presence of `line_item` (or `label` / `metric`)
      • presence of a period signal (`period` field OR `valuation_date`)
      • presence of a numeric value (`period_value`, `value`, `amount`)
      • absence of `company_name` (per-company rows keep the existing path)

    Universal — matches every fund that publishes fund-level Monthly P&L in
    a Line Item × Period matrix. No sheet-name / fund-name hardcoding.
    Rows that don't match this signature are ignored (returned as empty list),
    leaving the caller's per-company routing untouched.
    """
    if not pl_rows:
        return []
    pivoted: dict[str, dict] = {}
    for r in pl_rows:
        if not isinstance(r, dict):
            continue
        if r.get('company_name'):
            continue
        line_item = r.get('line_item') or r.get('label') or r.get('metric')
        canon_field = _canon_pl_line_item(line_item)
        if not canon_field:
            continue
        period_label = r.get('period')
        period_end = r.get('valuation_date') or r.get('period_end')
        # Pick the raw value: prefer explicit period_value, then any numeric
        # canonical variant, then generic 'value' / 'amount'.
        raw_val = (r.get('period_value')
                   if 'period_value' in r else None)
        if raw_val is None:
            raw_val = r.get('value') if 'value' in r else None
        if raw_val is None:
            raw_val = r.get('amount') if 'amount' in r else None
        if raw_val is None:
            continue
        try:
            num_val = Decimal(str(raw_val).replace(',', '').strip())
        except (ValueError, InvalidOperation, AttributeError):
            continue
        # Fix 3 — Cost-field sign normalisation. Accounting-format sheets
        # publish costs as negative numbers (visual subtraction from the
        # subtotal row above). The persister's derivation ladder
        # (revenue - cogs = gross_profit) expects the mathematical
        # magnitude, so we take abs() for known cost canonical fields.
        # Non-cost fields (revenue, ebitda, pat) preserve their sign so
        # an operating LOSS remains negative.
        if canon_field in _COST_FIELDS and num_val < 0:
            num_val = -num_val
        # Group key: prefer date-shaped period_end for correct ordering,
        # else fall back to the raw period label, else a single-slot 'total'
        # (used by the headerless-MIS rescue — one aggregate row per fund).
        if period_end:
            group_key = str(period_end)
        elif period_label:
            group_key = str(period_label)
        else:
            group_key = '__fund_total__'
        slot = pivoted.setdefault(group_key, {
            'company_name': FUND_AGGREGATE_SENTINEL,
        })
        if period_label and 'period' not in slot:
            slot['period'] = period_label
        if period_end and 'period_end' not in slot:
            slot['period_end'] = period_end
        # Universal non-clobber: keep first-seen value if the same canonical
        # field is set by two different line-item labels (unlikely but safe).
        slot.setdefault(canon_field, num_val)
    return list(pivoted.values())


# ── Fund master: label-slug → persister field name ──────────────────────────
# Universal: matches slug-form of typical fund master labels used by every
# fund architecture (Fund_Overview, FUND_MASTER, PPM_Details, etc.).
_FUND_MASTER_SLUG_ALIAS: dict[str, str] = {
    'fund_name': 'fund_name',
    'legal_form': 'structure_type',
    'sebi_reg_no': 'sebi_registration_number',
    'sebi_registration_number': 'sebi_registration_number',
    'sebi_registration': 'sebi_registration_number',
    'fund_pan': 'fund_pan',
    'category': 'category',
    'strategy': 'strategy',
    'investment_manager': 'manager_name',
    'fund_manager': 'manager_name',
    'manager_sebi_reg': 'manager_sebi_reg',
    'trustee': 'trustee_name',
    'custodian': 'custodian_name',
    'compliance_officer': 'compliance_officer',
    'registrar_transfer_agent': 'rta_name',
    'statutory_auditor': 'auditor_name',
    'legal_counsel': 'legal_counsel',
    # Economics
    'target_corpus_inr_cr': 'corpus_target',
    'final_close_corpus_inr_cr': 'corpus_target',
    'corpus_at_final_close': 'corpus_target',
    'fund_corpus': 'corpus_target',
    'investable_funds_9_exp': 'investable_funds',
    'gp_commitment_inr_cr': 'sponsor_commitment_amount',
    'gp_commitment': 'sponsor_commitment_pct',
    'lp_aggregate_commitment_inr_cr': 'total_committed_capital',
    'total_lp_commitments_cr': 'total_committed_capital',
    # Dates
    'fund_inception_date': 'inception_date',
    'fund_launch': 'inception_date',
    'first_close_date': 'first_close_date',
    'initial_close': 'first_close_date',
    'final_close_date': 'final_close_date',
    'final_close': 'final_close_date',
    'investment_period_end_date': 'investment_period_end_date',
    'fund_tenure_end_date': 'end_date',
    'end_date': 'end_date',
    'fiscal_year_end': 'fiscal_year_end',
    'ppm_filing_date': 'ppm_filing_date',
    'sebi_communication': 'sebi_communication_date',
    'tenure': 'tenure_years_text',
    'vintage_year': 'vintage_year',
    # Terms
    'carried_interest_rate': 'carry_pct',
    'carried_interest': 'carry_pct',
    'gp_carry_rate': 'carry_pct',
    'gp_carry': 'carry_pct',
    'carry_rate': 'carry_pct',
    'preferred_return_hurdle_rate': 'hurdle_rate_pct',
    'preferred_return': 'hurdle_rate_pct',
    'preferred_return_pct': 'hurdle_rate_pct',
    'preferred_return_rate': 'hurdle_rate_pct',
    'hurdle_rate': 'hurdle_rate_pct',
    'hurdle_rate_p_a_compounded': 'hurdle_rate_pct',
    'hurdle': 'hurdle_rate_pct',
    'management_fee_investment_period': 'management_fee_pct',
    'management_fee_rate_investment_period': 'management_fee_pct',
    'management_fee': 'management_fee_pct',
    'management_fee_post_inv_period': 'management_fee_pct_post',
    'management_fee_rate_post_ip': 'management_fee_pct_post',
    'distribution_waterfall': 'waterfall_type',
    'waterfall_type': 'waterfall_type',
    'fund_term_years': 'tenure_years',
    'fund_term': 'tenure_years',
    'fund_tenure_years': 'tenure_years',
    # Totals
    'total_lp_count': 'lp_count',
    'lp_count': 'lp_count',
    'total_portfolio_companies': 'portfolio_companies',
    'portfolio_companies_total': 'portfolio_companies',
    'portfolio_companies': 'portfolio_companies',
    'total_investment_transactions': 'investment_count',
    'total_capital_called_inr_cr': 'total_called_capital',
    'total_capital_called_cr': 'total_called_capital',
    'total_capital_called': 'total_called_capital',
    'uncalled_remaining_commitment_inr_cr': 'total_uncalled_capital',
    'total_distributions_made_inr_cr': 'total_distributions',
    'total_distributions_made_gross_inr_cr': 'total_distributions',
    'total_distributions': 'total_distributions',
    'reporting_currency': 'base_currency',
    'reporting_unit': 'reporting_unit',
    # Performance metrics (may live in fund master too)
    'total_cost_cr': 'invested_cost',
    'total_fair_value_cr': 'active_fair_value',
    'unrealised_gain_cr': 'unrealized_gain',
    'blended_portfolio_moic': 'moic',
    'portfolio_moic_x': 'moic',
    'net_irr_estimated': 'net_irr',
    'net_irr_estimated_pct': 'net_irr',
    'net_irr_post_fees_carry': 'net_irr',
    'gross_irr_pre_fees': 'gross_irr',
    'dpi_estimated': 'dpi',
    'dpi_estimated_x': 'dpi',
    'rvpi_estimated': 'rvpi',
    'rvpi_estimated_x': 'rvpi',
    'deployment': 'deployment_pct',
    # Solution D — Fund_Master summary-block aliases (Sequoia-style
    # PERFORMANCE SUMMARY rows). Additive: each aliases an EXISTING
    # canonical field, never introduces a new one. Files that don't
    # publish these labels see no behavior change.
    'lp_distributions_cumulative': 'total_distributions',
    'lp_distributions': 'total_distributions',
    'total_lp_distributions': 'total_distributions',
    'total_lp_distributions_cumulative': 'total_distributions',
    'exit_proceeds_cumulative': 'total_realised_proceeds',
    'cumulative_exit_proceeds': 'total_realised_proceeds',
    'preferred_return_accrued': 'preferred_return_amount',
    'preferred_return_amount': 'preferred_return_amount',
    'carry_provision_escrow': 'carry_amount_net',
    'carry_escrow_balance': 'carry_amount_net',
    'active_portfolio_fv': 'total_portfolio_fv',
    'active_portfolio_cost': 'active_portfolio_cost',
    'closing_fund_nav': 'total_nav',
    'net_asset_value_closing': 'total_nav',
    'nav_per_unit_rs': 'nav_per_unit',
    'nav_per_unit': 'nav_per_unit',
    'unrealised_gain_fv_cost': 'unrealized_gain',
    'unrealised_gain': 'unrealized_gain',
}


_WATERFALL_SLUG_ALIAS: dict[str, str] = {
    'carry_rate': 'carry_percentage',
    'carried_interest': 'carry_percentage',
    'carried_interest_rate': 'carry_percentage',
    'hurdle_rate': 'hurdle_rate',
    'preferred_return_hurdle_rate': 'hurdle_rate',
    'clawback_applicable': 'clawback_applicable',
    'catch_up_provision': 'catchup_provision',
    'carry_recipient': 'carry_recipient',
    'clawback_holdback': 'gp_holdback_pct',
    'clawback_escrow_bank': 'clawback_escrow_bank',
    'distribution_waterfall': 'waterfall_type',
    'waterfall_type': 'waterfall_type',
}

# Aliases from KV slugs (labels found in Fund_Overview VERIFIED CARRY FIGURES
# and Carry_Clawback CARRY SUMMARY blocks) to the exact metric names that
# Phase 4's reconciler expects in workbook_aggregates. Universal: every fund
# publishes these under one of these labels, so we translate here rather
# than teaching the reconciler ever-more aliases.
_WATERFALL_AGG_ALIAS: dict[str, str] = {
    # Carry base variants
    'carry_base':                          'carry_base',
    'carry_base_total_profit_above_capital': 'carry_base',
    'total_profit_above_capital':          'carry_base',
    # GP carry — gross entitlement
    'gp_carry_gross':                      'gp_carry_amount',
    'gp_carry_gross_entitlement':          'gp_carry_amount',
    'gp_carry_entitlement':                'gp_carry_amount',
    # GP carry — distributed (may include over-distribution)
    'gp_carry_distributed':                'gp_total_distribution',
    'gp_carry_gross_distributed':          'gp_total_distribution',
    # Clawback
    'clawback_provision':                  'gp_clawback_provision',
    'clawback_provision_required':         'gp_clawback_provision',
    # Escrow / holdback
    'gp_holdback_in_escrow':               'gp_carry_holdback_amount',
    'gp_carry_holdback':                   'gp_carry_holdback_amount',
    # Net carry
    'gp_carry_net':                        'gp_carry_amount_net',
    'gp_carry_net_after_holdback_before_clawback': 'gp_carry_amount_net',
    'gp_carry_net_after_holdback_clawback': 'gp_carry_amount_net_final',
    # Preferred return / catchup
    'total_preferred_return_accrued':      'preferred_return_amount',
    'preferred_return':                    'preferred_return_amount',
    'gp_catch_up_amount':                  'gp_catchup_amount',
    'gp_catchup_amount':                   'gp_catchup_amount',
    # Capital / distributions totals
    'total_capital_called':                'total_capital_called',
    'total_net_distributions_made_to_date': 'total_distributions',
    'total_distributions':                 'total_distributions',
    # LP share
    'lp_share_from_step_4':                'lp_total_return',
    'lp_total_return':                     'lp_total_return',
    # Solution D — Waterfall / Fund_Master summary aliases (Sequoia
    # WATERFALL sheet + Fund_Master PERFORMANCE SUMMARY block).
    # All map to metric names the reconciler + persister recognize
    # (carry_amount_gross, carry_amount_net, carry_base, etc.). No new
    # canonical fields are introduced — files that don't publish these
    # labels are unaffected.
    'profit_above_hurdle':                 'carry_base',
    'carry_provision_20':                  'carry_amount_gross',
    'carry_provision':                     'carry_amount_gross',
    'preferred_return_accrued':            'preferred_return_amount',
    # Fix D (2026-07-06 Sequoia clawback fix) — "Carry Escrow Balance" and
    # "Carry Provision (Escrow)" are amounts HELD ASIDE against future
    # clawback, not the final net carry. Aliasing to gp_clawback_provision
    # is semantically correct AND surfaces the value on the dashboard's
    # "Clawback Provision" tile. Downstream Python still computes
    # carry_amount_net = carry_amount_gross - gp_clawback_provision.
    # Universal: files using explicit "Net Carry" labels use different
    # aliases (net_carry, carry_amount_net_after_holdback) that still
    # map to carry_amount_net unchanged.
    'carry_provision_escrow':              'gp_clawback_provision',
    'carry_escrow_balance':                'gp_clawback_provision',
    'lp_distributions_cumulative':         'total_distributions',
    'lp_distributions':                    'total_distributions',
    'total_lp_distributions_cumulative':   'total_distributions',
    'exit_proceeds_cumulative':            'total_realised_proceeds',
    'cumulative_exit_proceeds':            'total_realised_proceeds',
    'gross_value_exits_active_fv':         'gross_portfolio_value',
    'gross_value':                         'gross_portfolio_value',
}


_PCT_FIELDS = {
    'hurdle_rate_pct', 'carry_pct', 'management_fee_pct', 'management_fee_pct_post',
    'sponsor_commitment_pct', 'carry_percentage', 'hurdle_rate',
    'gp_holdback_pct', 'net_irr', 'deployment_pct',
}


def _remap_fund_master(fund_kv: dict) -> dict:
    out: dict = {}
    for src_slug, canon in _FUND_MASTER_SLUG_ALIAS.items():
        if src_slug not in fund_kv or src_slug == '__labels__':
            continue
        v = fund_kv[src_slug]
        if isinstance(v, dict):  # ignore side-channel dicts
            continue
        if canon in _PCT_FIELDS:
            v = extract_pct(v) or v
        out.setdefault(canon, v)
    return out


def _remap_waterfall(wf_kv: dict, fm: dict) -> dict:
    out: dict = {}
    for src_slug, canon in _WATERFALL_SLUG_ALIAS.items():
        if src_slug not in wf_kv or src_slug == '__labels__':
            continue
        v = wf_kv[src_slug]
        if isinstance(v, dict):
            continue
        if canon in _PCT_FIELDS:
            v = extract_pct(v) or v
        out.setdefault(canon, v)
    for src, dst in (('carry_pct', 'carry_percentage'),
                     ('hurdle_rate_pct', 'hurdle_rate'),
                     ('waterfall_type', 'waterfall_type'),
                     ('total_called_capital', 'total_capital_called'),
                     ('total_distributions', 'total_distributions')):
        if src in fm and dst not in out:
            out[dst] = fm[src]
    return out


def _merge_lp_line_items_into_commitments(
    commitments: list[dict],
    cc_events: list[dict], cc_lines: list[dict],
    dist_events: list[dict], dist_lines: list[dict],
) -> list[dict]:
    """Sum entity-pivoted line items per LP and stamp cumulative_called /
    cumulative_distributed on the commitment row. Only sets values that
    aren't already present (extraction wins over derivation)."""
    lp_id_by_name: dict[str, str] = {}
    for c in commitments:
        eid = c.get('lp_id') or c.get('investor_id') or c.get('entity_id')
        if eid and c.get('investor_name'):
            lp_id_by_name[eid] = c['investor_name']
    if not lp_id_by_name:
        for i, c in enumerate(commitments, start=1):
            eid = f'LP{i:03d}'
            if c.get('investor_name'):
                lp_id_by_name[eid] = c['investor_name']

    called_per_lp: dict[str, Decimal] = {}
    dist_per_lp: dict[str, Decimal] = {}
    for li in cc_lines:
        eid = li['entity_id']
        called_per_lp[eid] = called_per_lp.get(eid, Decimal(0)) + li['amount']
    for li in dist_lines:
        eid = li['entity_id']
        dist_per_lp[eid] = dist_per_lp.get(eid, Decimal(0)) + li['amount']

    for c in commitments:
        eid = c.get('lp_id')
        if not eid:
            for _eid, _name in lp_id_by_name.items():
                if _name == c.get('investor_name'):
                    eid = _eid
                    break
        if not eid:
            continue
        if 'cumulative_called' not in c and eid in called_per_lp:
            c['cumulative_called'] = called_per_lp[eid]
        if 'cumulative_distributed' not in c and eid in dist_per_lp:
            c['cumulative_distributed'] = dist_per_lp[eid]
    return commitments


def build_unified_json(per_sheet: dict, workbook_data: dict) -> dict:
    """Assemble persister-shaped unified_json from per-sheet extractions."""
    by_dom: dict[str, list[dict]] = {}
    kv_by_dom: dict[str, dict] = {}
    events_by_dom: dict[str, list[dict]] = {}
    lines_by_dom: dict[str, list[dict]] = {}

    for sn, info in per_sheet.items():
        d = info.get('domain')
        if not d:
            continue
        if 'rows' in info:
            by_dom.setdefault(d, []).extend(info['rows'])
        if 'kv' in info:
            kv_by_dom.setdefault(d, {}).update(info['kv'])
        if 'events' in info:
            events_by_dom.setdefault(d, []).extend(info['events'])
        if 'line_items' in info:
            lines_by_dom.setdefault(d, []).extend(info['line_items'])

    fund_kv = kv_by_dom.get('fund_scheme_master', {})
    wf_kv = kv_by_dom.get('waterfall_carry', {})
    fund_master = _remap_fund_master(fund_kv)
    waterfall = _remap_waterfall(wf_kv, fund_master)

    portfolio_investments = list(by_dom.get('portfolio_investments', []))

    # Universal LP routing: An LP row is an LP row regardless of whether
    # Gemini classified the sheet as "commitments", "investors_aml", or
    # "lp_capital_accounts". Any row that has investor_name populated
    # contributes to BOTH investors[] and commitments[]. The persister
    # tolerates many aliases (commitment_amount / commitment / commitment_cr,
    # cumulative_called / drawdown / contributions, etc.) — see
    # _persist_commitments in phase2_persister.py.
    lp_rows: list[dict] = []
    for dom in ('commitments', 'investors_aml', 'lp_capital_accounts'):
        for r in by_dom.get(dom, []):
            if r.get('investor_name'):
                lp_rows.append(r)
    # Dedup by investor_name (keep the row with the most fields)
    seen: dict[str, dict] = {}
    for r in lp_rows:
        name = r.get('investor_name')
        cur = seen.get(name)
        if cur is None or len(r) > len(cur):
            seen[name] = {**(cur or {}), **r} if cur else dict(r)
    commitments = list(seen.values())
    investors = [dict(r) for r in commitments]

    capital_calls_events = events_by_dom.get('capital_calls', [])
    capital_calls_rows = by_dom.get('capital_calls', [])

    # Fix A — row-shape filter for the capital_calls bucket.
    #
    # When Gemini classifies a sheet as `capital_calls` domain, Stage 2 emits
    # EVERY row from that sheet into this bucket. But CAPITAL_CALLS sheets in
    # real workbooks often have TWO sections:
    #   (1) top: call-level rows with call_date + amount columns  ← real calls
    #   (2) bottom: LP-level "capital call tracking" rows with
    #       investor_name + commitment_amount + per-call allocations
    #
    # Without filtering, section (2) rows land in capital_calls without any
    # call_date, and the persister's date fallback turns them into phantom
    # rows dated today. This poisons Priority 1 XIRR (dated cashflows).
    #
    # Universal rule: a real capital call ALWAYS has at least one of these
    # call-EVENT signals. LP-commitment/KYC rows never carry any of them.
    #
    # Tightened 2026-07-07 after enriching CAPITAL_CALLS_FIELDS aliases.
    # Broader signals (call_percentage, total_call_amount, purpose) CAN
    # appear on LP-level rows too — e.g. an LP-pivot column called "% of
    # Commit" would alias to call_percentage; "Total Called (Cr)" to
    # total_call_amount. Restricting to strict event-shape signals
    # (date/number/ref) guarantees LP rows never leak into capital_calls
    # regardless of how the alias index is enriched in the future.
    _CALL_SHAPE_SIGNALS = ('call_date', 'call_number', 'call_ref')

    def _looks_like_capital_call(row: dict) -> bool:
        if not isinstance(row, dict):
            return False
        for k in _CALL_SHAPE_SIGNALS:
            v = row.get(k)
            if v not in (None, '', [], {}):
                return True
        return False

    def _split(rows):
        keep, reroute = [], []
        for r in rows:
            (keep if _looks_like_capital_call(r) else reroute).append(r)
        return keep, reroute

    _cc_events_keep, _cc_events_reroute = _split(capital_calls_events)
    _cc_rows_keep,   _cc_rows_reroute   = _split(capital_calls_rows)
    capital_calls = _cc_events_keep if _cc_events_keep else _cc_rows_keep

    # Reroute LP-shape rows to the commitments bucket so their commitment /
    # KYC info isn't lost when the LP_Register sheet is thin or missing. Dedup
    # by investor_name against the commitments already built above (line 579).
    _reroute = [r for r in (_cc_events_reroute + _cc_rows_reroute)
                if isinstance(r, dict) and r.get('investor_name')]
    if _reroute:
        _seen_names = {c.get('investor_name') for c in commitments if c.get('investor_name')}
        for r in _reroute:
            _n = r.get('investor_name')
            if _n and _n not in _seen_names:
                commitments.append(dict(r))
                investors.append(dict(r))
                _seen_names.add(_n)

    dist_events = events_by_dom.get('exits_distributions', [])
    dist_rows = by_dom.get('exits_distributions', [])

    # Solution A — widened distribution routing.
    # A row is a Distribution when EITHER it has an explicit distribution_date
    # OR it carries a distribution-signature field (numeric distribution_number
    # OR total_gross_amount + period) AND does NOT carry an exit_date.
    # This lets stacked-section extracted rows (Sequoia "DISTRIBUTION SCHEDULE"
    # sub-table below Exits) reach the persister even without an explicit
    # date column. exit_date exclusion prevents double-classification.
    #
    # Junk-row guard: distribution_number must be numeric-shaped (not a
    # validation footer like "Validation: Sum distributions = ..."). Rejects
    # the row-below-data trailer common in structured mock sheets.
    def _looks_like_distribution(r: dict) -> bool:
        if r.get('exit_date'):
            return False
        if r.get('distribution_date'):
            return True
        dn = r.get('distribution_number')
        # Require distribution_number to be numeric OR a short numeric-looking
        # string. Reject long text (validation footers etc.).
        dn_is_numeric = isinstance(dn, (int, float, Decimal)) or (
            isinstance(dn, str) and len(dn) < 12
            and any(ch.isdigit() for ch in dn)
        )
        has_amount = (r.get('total_gross_amount') is not None
                      or r.get('total_net_amount') is not None)
        if dn_is_numeric and has_amount:
            return True
        if r.get('period') and has_amount:
            return True
        return False

    distributions = [e for e in dist_events if e.get('distribution_date')] \
        + [r for r in dist_rows if _looks_like_distribution(r)]

    exit_rows = by_dom.get('exits_distributions', [])
    exits = [r for r in exit_rows if r.get('exit_date')]

    # Universal shape-based split for the valuations_kpis domain.
    #
    # This domain is a bag holding rows from THREE different consumers:
    #   1. Valuations (IPEV) sheets — rows carry fair_value / enterprise_value
    #      / methodology / cost_basis / valuer_name. These are Valuation records.
    #   2. SaaS Metrics / KPI sheets — rows carry arr / mrr / nrr / churn_rate /
    #      cac / ltv / gross_burn / runway_months / gross_margin_pct / headcount.
    #      These are PortfolioKPI records.
    #   3. Wide-period KPI matrix sheets — rows carry kpi_name / period_value.
    #      These are also PortfolioKPI records (long-format).
    # If we route the whole bag into `valuations`, the persister writes them
    # all as Valuation records — losing every SaaS metric (Multiples IV bug).
    # Solve by row-shape: a valuation-shaped row has any of the IPEV keys;
    # a kpi-shaped row has any of the SaaS/KPI keys; kpi rows get sent
    # downstream to portfolio_kpis_periodic. Universal — no sheet-name
    # hardcoding, works for any fund that ships SaaS Metrics or KPI matrix
    # sheets under this domain.
    _VAL_SIG_KEYS = ('fair_value', 'fair_value_of_holding', 'enterprise_value',
                     'cost_basis', 'methodology', 'valuer_name',
                     'valuer_reg_number', 'valuation_status',
                     'unrealized_gain_loss', 'discount_rate', 'multiple')
    _KPI_SIG_KEYS = ('arr', 'mrr', 'nrr', 'churn_rate', 'cac', 'ltv',
                     'ltv_cac_ratio', 'gross_burn', 'net_burn',
                     'runway_months', 'cash_balance', 'gross_margin_pct',
                     'ebitda_margin_pct', 'headcount', 'customers',
                     'new_customers', 'gmv', 'orders', 'aov', 'returns_pct',
                     'repeat_pct', 'kpi_name', 'kpi_value', 'period_value')
    valuations: list[dict] = []
    valuations_kpi_rows: list[dict] = []
    for row in by_dom.get('valuations_kpis', []):
        if not isinstance(row, dict):
            continue
        has_val = any(k in row for k in _VAL_SIG_KEYS)
        has_kpi = any(k in row for k in _KPI_SIG_KEYS)
        # A row can carry BOTH shapes on hybrid sheets (rare). Send to both
        # arrays so neither consumer loses data. Ambiguous rows (neither
        # shape) default to valuations for backward compatibility.
        if has_val:
            valuations.append(row)
        if has_kpi:
            valuations_kpi_rows.append(row)
        if not has_val and not has_kpi:
            valuations.append(row)

    # ── Universal FV mirror from Portfolio Investments ─────────────────
    # Many fund sheets publish a "FV (Cr)" column right on the Portfolio
    # Investments master sheet (True North Healthcare Fund VI is one example).
    # Without this mirror, that FV data never becomes a Valuation record —
    # dashboard shows FV=0 for every company even though the source file has
    # values. Universal across any fund whose Portfolio Investments sheet
    # publishes fair_value per row. Only fires when fair_value is present;
    # cost/date/sector inherit from the same row when available.
    _fv_date_by_company: dict[str, object] = {}
    for row in by_dom.get('portfolio_investments', []):
        if not isinstance(row, dict):
            continue
        co = row.get('company_name')
        fv = row.get('fair_value')
        if co and fv is not None:
            valuations.append({
                'company_name': co,
                'sector': row.get('sector'),
                'fair_value': fv,
                'fair_value_of_holding': row.get('fair_value_of_holding') or fv,
                'cost_basis': row.get('total_invested') or row.get('cost_basis'),
                'valuation_date': (row.get('valuation_date')
                                   or row.get('as_of_date')
                                   or row.get('val_date')),
                'methodology': row.get('methodology'),
            })
            if row.get('valuation_date'):
                _fv_date_by_company[str(co).strip().lower()] = row['valuation_date']

    # Universal cross-sheet date backfill: when a Valuations (IPEV) row has
    # no valuation_date (True North's IPEV sheet has no per-row date), borrow
    # the "as of" date from the same company's Portfolio Investments row.
    # Deterministic and file-scoped — no calendar guessing.
    for row in valuations:
        if not row.get('valuation_date') and row.get('company_name'):
            borrowed = _fv_date_by_company.get(str(row['company_name']).strip().lower())
            if borrowed:
                row['valuation_date'] = borrowed

    # Universal NAV vs Fund-ledger split. Sheets classified as
    # `nav_accounting` can be one of two very different things:
    #   (a) a quarterly balance-sheet-style NAV walk (rows = periods, cols =
    #       fund_cash / mgmt_fee / investments_at_fair_value / total_units_outstanding), OR
    #   (b) a transaction ledger (rows = timestamped cash events, cols =
    #       capital_called / investment_outflow / distribution_amount /
    #       description / net_movement).
    # Distinguisher: a NAV row ALWAYS has one of the balance-sheet aggregate
    # fields (investments_at_cost, investments_at_fair_value, total_nav,
    # net_nav, gross_nav, total_units_outstanding). A ledger row has txn-level
    # fields (description, net_movement, investment_outflow) or lacks the
    # balance-sheet aggregates entirely. Universal — every fund's NAV walk
    # publishes at least one balance-sheet aggregate per period.
    _NAV_SIGNATURE = ('investments_at_fair_value', 'investments_at_cost',
                      'total_nav', 'net_nav', 'gross_nav',
                      'total_units_outstanding')
    _LEDGER_SIGNATURE = ('description', 'net_movement', 'investment_outflow',
                         'realisation_inflow', 'realization_inflow',
                         'ref_no', 'reference_no', 'transaction_type')
    nav_records: list[dict] = []
    fund_ledger_rows: list[dict] = []
    for row in by_dom.get('nav_accounting', []):
        is_ledger = any(k in row for k in _LEDGER_SIGNATURE)
        has_nav_sig = any(k in row for k in _NAV_SIGNATURE)
        if is_ledger or not has_nav_sig:
            fund_ledger_rows.append(row)
        else:
            nav_records.append(row)

    # Universal multi-section rescue (added 2026-07-03): the nav_calculation
    # domain covers sheets like NAV_CALC that have a KV composition table at
    # the top AND a monthly NAV walk further down. The Stage-2 extractor now
    # emits rows for the deeper walk with nav_accounting-shaped fields. Route
    # any NAV-signature row from nav_calculation into nav_records so the
    # persister creates NAVRecord entries. No sheet-name hardcoding.
    for row in by_dom.get('nav_calculation', []):
        if not isinstance(row, dict):
            continue
        if any(k in row for k in _NAV_SIGNATURE):
            nav_records.append(row)

    # Fix 3 — universal wide-period NAV rescue.
    #
    # Some workbooks publish NAV as a pivot: row-labels are balance-sheet
    # components ("Closing Fund NAV", "Portfolio Fair Value"), columns are
    # periods (Oct-24, Nov-24, …). extract_wide_period unpivots these to
    # {line_item, period, period_value} rows. The tabular NAV_SIGNATURE
    # match above doesn't catch them because they carry line_item shape,
    # not balance-sheet aggregate fields. This block detects the wide-
    # period rows whose line_item aliases match a NAV concept and emits
    # one NAV record per period, letting the persister's period→date
    # helper handle the FY/quarter/month parsing.
    #
    # Non-regressing: dedup by period label against nav_records that
    # already carry that period, and against nav_date directly. Sheets
    # with proper tabular NAV extraction (Bharatcrest 13 monthly rows,
    # Trivesta 19) emit no line_item-shape rows and see zero change.
    _NAV_LINE_ITEM_ALIASES = {
        'closing_fund_nav', 'closing_nav', 'total_nav', 'net_asset_value',
        'fund_nav', 'net_nav', 'total_fund_nav', 'gross_nav',
        'closing_fund_nav_cr', 'closing_nav_cr', 'total_fund_nav_cr',
        'closing_nav_before_carry', 'net_asset_value_closing_nav',
    }
    _existing_period_labels: set = set()
    for _r in nav_records:
        for _k in ('nav_date', 'period', 'date', 'period_end',
                   'quarter', 'month', 'financial_year'):
            _v = _r.get(_k)
            if _v:
                _existing_period_labels.add(str(_v))
    for _dom in ('nav_accounting', 'nav_calculation'):
        for _row in by_dom.get(_dom, []):
            if not isinstance(_row, dict):
                continue
            _li = (_row.get('line_item') or _row.get('metric_name')
                   or _row.get('component'))
            _period = (_row.get('period') or _row.get('period_end')
                       or _row.get('quarter') or _row.get('month'))
            _value = _row.get('period_value')
            if _value is None:
                _value = _row.get('value')
            if not _li or not _period or _value is None:
                continue
            _slug = re.sub(r'[^a-z0-9]+', '_', str(_li).lower()).strip('_')
            if _slug not in _NAV_LINE_ITEM_ALIASES:
                continue
            if str(_period) in _existing_period_labels:
                continue
            try:
                if isinstance(_value, Decimal):
                    _num = _value
                else:
                    _num = Decimal(str(_value).replace(',', '').strip())
            except (ValueError, InvalidOperation, AttributeError):
                continue
            if _num == 0:
                continue
            nav_records.append({'period': _period, 'total_nav': _num})
            _existing_period_labels.add(str(_period))

    # Universal KV → single NAV record synthesis (added 2026-07-03).
    # Some NAV Calculation sheets are 100% key-value (True North's NAV
    # Calculation has 4 KV sections: Investable Funds, NAV Summary, QoQ Bridge,
    # Per-Unit NAV — but NO tabular monthly walk). The KV extractor captures
    # values like "NET ASSET VALUE (CLOSING NAV) = 2433.85" but nothing
    # consumed them → NAV History stayed empty. Emit ONE synthetic NAV record
    # from the KV so the dashboard's Total NAV tile populates. Persister's
    # fund-context date fallback provides the nav_date. Only fires when the
    # tabular rescue produced nothing — no double-writes.
    _NAV_KV_SLUG_TO_FIELD = {
        'net_asset_value': 'total_nav',
        'net_asset_value_closing_nav': 'total_nav',
        'closing_nav': 'total_nav',
        'total_nav': 'total_nav',
        'net_nav': 'total_nav',
        'total_fund_nav': 'total_nav',
        'total_fund_nav_inr_crores': 'total_nav',
        'gross_nav': 'gross_nav',
        'opening_nav': 'opening_nav',
        'total_units_outstanding': 'total_units_outstanding',
        'total_units_issued': 'total_units_outstanding',
        'units_outstanding': 'total_units_outstanding',
        'nav_per_unit': 'nav_per_unit',
        'closing_nav_per_unit': 'nav_per_unit',
        'nav_per_unit_inr_lakhs': 'nav_per_unit',
        'portfolio_fair_value': 'investments_at_fair_value',
        'portfolio_fair_value_ipev_certified': 'investments_at_fair_value',
        'add_temporary_investments': 'investments_at_fair_value',
        'cash_bank_balances': 'cash_and_equivalents',
        'cash_and_bank_balances': 'cash_and_equivalents',
        'add_cash_bank_balances': 'cash_and_equivalents',
        'management_fees_payable': 'management_fee_payable',
        'less_management_fees_payable': 'management_fee_payable',
        'accrued_fees': 'management_fee_payable',
    }
    if not any(r.get('total_nav') for r in nav_records):
        # Solution D+ (2026-07-06 Sequoia fix) — check BOTH domain buckets.
        # Gemini legitimately classifies a NAV sheet as either
        # `nav_calculation` OR `nav_accounting` depending on which sections
        # dominate (Investable Funds walk vs Balance Sheet aggregates). We
        # merge both KV pools with nav_calculation winning on collision so
        # behavior stays deterministic for files that already extracted
        # correctly under nav_calculation (True North).
        # Universal: guarded by "nav_records has no total_nav yet" — files
        # with proper tabular NAV extraction (Trivesta 36 monthly rows) skip
        # this entirely and are unaffected.
        nav_calc_kv = {}
        for _dom in ('nav_calculation', 'nav_accounting'):
            for _k, _v in (kv_by_dom.get(_dom) or {}).items():
                nav_calc_kv.setdefault(_k, _v)
        if nav_calc_kv:
            synthetic: dict = {}
            for src_slug, dst_field in _NAV_KV_SLUG_TO_FIELD.items():
                v = nav_calc_kv.get(src_slug)
                if v is None or isinstance(v, dict):
                    continue
                _num = None
                try:
                    if isinstance(v, (int, float, Decimal)):
                        _num = Decimal(str(v))
                    elif isinstance(v, str):
                        _num = Decimal(v.replace(',', '').strip())
                except (ValueError, InvalidOperation, AttributeError):
                    _num = None
                if _num is not None and dst_field not in synthetic:
                    synthetic[dst_field] = _num
            if synthetic.get('total_nav'):
                nav_records.append(synthetic)

    # Per-company P&L / KPIs / BvA. The financials_pl_bva domain is a bag
    # holding rows from P&L / BS / CF / KPI / SaaS Metrics / Budget vs
    # Actual sheets. Row-shape decides where each one goes:
    #   • budget/actual keys present   → budget_vs_actual  (BudgetVsActual persister)
    #   • else, company_name present   → portfolio_kpis_periodic (KPI persister)
    # A row without company_name is dropped from KPI stream but still passed
    # through monthly_pl_rows so P&L extractors can pick it up if useful.
    # Universal — no sheet-name hardcoding.
    pl_all = list(by_dom.get('financials_pl_bva', []))

    def _is_bva_row(r: dict) -> bool:
        return r.get('budget') is not None or r.get('actual') is not None

    portfolio_kpis_periodic: list[dict] = []
    budget_vs_actual_rows: list[dict] = []
    for r in pl_all:
        # Fix B (2026-07-06 Sequoia BVA fix) — BVA detection MUST run before
        # the company_name gate. Sequoia's Budget vs Actual sheet publishes
        # FUND-LEVEL rows (line_item=Portfolio Revenue, budget=254, actual=241,
        # variance_pct=-0.051) with no per-company scoping. Under the old
        # rule these rows got skipped (no company_name), leaving BVA empty
        # AND monthly_pl_rows populated with BVA-shaped noise.
        # Universal: files that DO scope BVA per-company (has both
        # company_name AND budget+actual) still land in budget_vs_actual —
        # the router just no longer requires a company scope.
        if _is_bva_row(r):
            budget_vs_actual_rows.append(r)
            continue
        if not r.get('company_name'):
            continue
        # Universal: normalise the period field. Persister expects `period`,
        # but Portfolio_Financials-style sheets publish `financial_year` or `fy`.
        if 'period' not in r:
            r['period'] = (r.get('financial_year') or r.get('fy')
                           or r.get('period_end') or r.get('period_start'))
        portfolio_kpis_periodic.append(r)

    # Fix C — Fund-level Monthly P&L pivot.
    # Sheets like "Monthly P&L (MIS)" publish a Line Item × Period matrix
    # (Portfolio Revenue / EBITDA / COGS × Oct-24 / Nov-24 / ...). After the
    # extractors.py wide-period rescue, these come through as unpivoted
    # {line_item, period, valuation_date, period_value} rows with NO
    # company_name. The current loop above skips them (needs company_name).
    #
    # _pivot_fund_level_pl re-groups them by period → one dict per period
    # carrying canonical KPI fields (revenue, cogs, ebitda, pat, ...) and
    # attaches the FUND_AGGREGATE_SENTINEL company_name so the persister's
    # KPI derivation ladder (revenue-cogs → gross_profit / gross_margin_pct,
    # then EBITDA + ebitda_margin_pct) runs unchanged. The persister
    # auto-creates a real PortfolioCompany with is_aggregate=True for the
    # sentinel; the custom manager hides it from every user-facing query.
    #
    # Universal: applies to every fund whose MIS sheet publishes fund-level
    # P&L; funds without such a sheet see zero pivoted rows and no behaviour
    # change.
    fund_level_pl = _pivot_fund_level_pl(pl_all)
    if fund_level_pl:
        portfolio_kpis_periodic.extend(fund_level_pl)

    # Universal merge: KPI-shape rows extracted from the valuations_kpis domain
    # (SaaS Metrics sheets, KPI matrix wide-period unpivots) join the periodic
    # KPI stream so the persister can create PortfolioKPI records for
    # mrr / arr / churn_rate / cac / ltv / headcount / etc.
    #
    # Universal canonical-field aliasing: Gemini frequently maps columns to
    # the closest field name in the valuations_kpis schema (e.g. EBITDA →
    # `ebitda_value`) even when the row is destined for the KPI persister,
    # which reads `ebitda`. Apply a small, non-lossy alias set BEFORE the
    # merge so the persister actually sees the values. Only added when the
    # target field is absent — never clobbers a value that came in directly.
    _KPI_CANONICAL_ALIASES = {
        'ebitda_value': 'ebitda',
        'ebitda_margin': 'ebitda_margin_pct',
        'gross_margin': 'gross_margin_pct',
        'net_burn_rate': 'net_burn',
        'gross_burn_rate': 'gross_burn',
        'monthly_burn': 'gross_burn',
        'cash': 'cash_balance',
        'runway': 'runway_months',
        # Solution F — widen KPI aliases so more Excel spellings map to
        # the canonical fields the persister writes. Additive only:
        # each src → dst pair fills dst ONLY when dst is already blank.
        # ── Revenue variants ─────────────────────────────
        'rev': 'revenue',
        'revenue_cr': 'revenue',
        'net_revenue': 'revenue',
        'net_sales': 'revenue',
        'total_revenue': 'revenue',
        'topline': 'revenue',
        'turnover': 'revenue',
        # ── SaaS: MRR / ARR variants ────────────────────
        'monthly_recurring_revenue': 'mrr',
        'monthly_recurring': 'mrr',
        'monthly_saas_revenue': 'mrr',
        'arr_cr': 'arr',
        'annual_recurring_revenue': 'arr',
        'annualised_run_rate': 'arr',
        'annualized_run_rate': 'arr',
        'annual_revenue_run_rate': 'arr',
        # ── Commerce KPIs ───────────────────────────────
        'gmv_cr': 'gmv',
        'gross_merch_value': 'gmv',
        'gross_sales_value': 'gmv',
        'total_gmv': 'gmv',
        'order_count': 'orders',
        'total_orders': 'orders',
        'no_of_orders': 'orders',
        'transactions': 'orders',
        'average_order_value': 'aov',
        'avg_order_value': 'aov',
        'avg_transaction_value': 'aov',
        'average_ticket_size': 'aov',
        'return_rate_pct': 'returns_pct',
        'return_rate': 'returns_pct',
        'rto_pct': 'returns_pct',
        'product_return_pct': 'returns_pct',
        'product_returns_pct': 'returns_pct',
        'repeat_customer_rate': 'repeat_pct',
        'repeat_rate': 'repeat_pct',
        'retention_rate': 'repeat_pct',
        'retention_pct': 'repeat_pct',
        'customer_retention_pct': 'repeat_pct',
        # ── CAC / LTV variants ──────────────────────────
        'customer_acquisition_cost': 'cac',
        'blended_cac': 'cac',
        'cost_to_acquire': 'cac',
        'customer_lifetime_value': 'ltv',
        'clv': 'ltv',
        # ── Margins ─────────────────────────────────────
        'gross_profit_margin': 'gross_margin_pct',
        'gross_profit_pct': 'gross_margin_pct',
        'gm_pct': 'gross_margin_pct',
        'operating_margin': 'ebitda_margin_pct',
        # ── Retention / churn spellings ─────────────────
        'net_revenue_retention': 'nrr',
        'net_dollar_retention': 'nrr',
        'ndr': 'nrr',
        'net_retention': 'nrr',
        'monthly_churn': 'churn_rate',
        'revenue_churn': 'churn_rate',
        'customer_churn': 'churn_rate',
    }
    for r in valuations_kpi_rows:
        if not r.get('company_name'):
            continue
        for src, dst in _KPI_CANONICAL_ALIASES.items():
            if src in r and r.get(dst) in (None, ''):
                r[dst] = r[src]
        if 'period' not in r:
            r['period'] = (r.get('financial_year') or r.get('fy')
                           or r.get('period_end') or r.get('period_start')
                           or r.get('valuation_date'))
        portfolio_kpis_periodic.append(r)

    commitments = _merge_lp_line_items_into_commitments(
        commitments,
        capital_calls_events,
        lines_by_dom.get('capital_calls', []),
        dist_events,
        lines_by_dom.get('exits_distributions', []),
    )

    fund_performance: dict = {}
    for key in ('total_called_capital', 'total_uncalled_capital',
                'total_committed_capital', 'total_distributions',
                'lp_count', 'portfolio_companies'):
        if key in fund_master:
            fund_performance[key] = fund_master[key]

    # Universal workbook_aggregates for Phase 4 reconciler. Every
    # (label, numeric_value) pair extracted from waterfall_carry or
    # fund_scheme_master that matches a known aggregate name gets emitted
    # in the shape the reconciler expects: {metric, value, label_text,
    # sheet, cell}. Reconciler prefers these over Python re-derivation.
    # label_text uses the ORIGINAL human-readable label from the sheet
    # (preserved by extract_key_value's __labels__ side-channel) so the
    # reconciler's whitelist can substring-match keywords like "clawback"
    # or "carry base" against the actual row wording.
    workbook_aggregates: list[dict] = []
    _agg_seen: set[str] = set()
    wf_labels = wf_kv.get('__labels__', {}) if isinstance(wf_kv, dict) else {}
    fm_labels = fund_kv.get('__labels__', {}) if isinstance(fund_kv, dict) else {}

    def _emit_agg(metric: str, value, sheet: str, slug_key: str, human_label: str):
        if metric in _agg_seen or value is None:
            return
        try:
            num = float(value)
        except (TypeError, ValueError):
            return
        workbook_aggregates.append({
            'metric': metric,
            'value': num,
            'label_text': human_label or slug_key.replace('_', ' '),
            'sheet': sheet,
            'cell': 'phase6_kv',
        })
        _agg_seen.add(metric)

    for slug_k, v in wf_kv.items():
        if slug_k == '__labels__':
            continue
        canon = _WATERFALL_AGG_ALIAS.get(slug_k)
        if canon:
            _emit_agg(canon, v, 'waterfall_carry', slug_k, wf_labels.get(slug_k, ''))
    # Fund_Overview can also publish carry aggregates in its
    # "VERIFIED CARRY FIGURES" block — pick them up too.
    for slug_k, v in fund_kv.items():
        if slug_k == '__labels__':
            continue
        canon = _WATERFALL_AGG_ALIAS.get(slug_k)
        if canon:
            _emit_agg(canon, v, 'fund_scheme_master', slug_k, fm_labels.get(slug_k, ''))

    unified = {
        'fund_master': fund_master,
        'waterfall': waterfall,
        'fund_performance': fund_performance,
        'workbook_aggregates': workbook_aggregates,
        'investors': investors,
        'commitments': commitments,
        'capital_calls': capital_calls,
        'distributions': distributions,
        'portfolio_investments': portfolio_investments,
        'valuations': valuations,
        'exits': exits,
        'nav_records': nav_records,
        'fund_ledger_rows': fund_ledger_rows,
        'compliance_records': list(by_dom.get('compliance', [])),
        'quoted_unquoted': list(by_dom.get('quoted_unquoted', [])),
        'portfolio_kpis_periodic': portfolio_kpis_periodic,
        'monthly_pl_rows': pl_all,
        'monthly_bs_rows': [],
        'monthly_cf_rows': [],
        'budget_vs_actual': budget_vs_actual_rows,
        'burn_runway': list(by_dom.get('burn_runway', [])),
        'entities': [],
        'sheet_completeness': [
            {
                'sheet_name': sn,
                'target_array': info.get('domain'),
                'rows_extracted': (
                    len(info.get('rows', [])) if 'rows' in info else
                    len(info.get('events', [])) if 'events' in info else
                    1 if 'kv' in info else 0
                ),
            }
            for sn, info in per_sheet.items()
        ],
        '__source_filepath__': workbook_data.get('__source_filepath__', ''),
    }
    return unified
