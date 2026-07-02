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
from decimal import Decimal
from typing import Any

from .coercers import extract_pct


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
    'preferred_return_hurdle_rate': 'hurdle_rate_pct',
    'preferred_return': 'hurdle_rate_pct',
    'preferred_return_pct': 'hurdle_rate_pct',
    'preferred_return_rate': 'hurdle_rate_pct',
    'hurdle_rate': 'hurdle_rate_pct',
    'hurdle': 'hurdle_rate_pct',
    'management_fee_investment_period': 'management_fee_pct',
    'management_fee': 'management_fee_pct',
    'management_fee_post_inv_period': 'management_fee_pct_post',
    'distribution_waterfall': 'waterfall_type',
    'waterfall_type': 'waterfall_type',
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
    'dpi_estimated': 'dpi',
    'dpi_estimated_x': 'dpi',
    'rvpi_estimated': 'rvpi',
    'rvpi_estimated_x': 'rvpi',
    'deployment': 'deployment_pct',
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
    capital_calls = capital_calls_events if capital_calls_events else capital_calls_rows

    dist_events = events_by_dom.get('exits_distributions', [])
    dist_rows = by_dom.get('exits_distributions', [])
    distributions = [e for e in dist_events if e.get('distribution_date')] \
        + [r for r in dist_rows if r.get('distribution_date')]

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
        if not r.get('company_name'):
            continue
        # Universal: normalise the period field. Persister expects `period`,
        # but Portfolio_Financials-style sheets publish `financial_year` or `fy`.
        if 'period' not in r:
            r['period'] = (r.get('financial_year') or r.get('fy')
                           or r.get('period_end') or r.get('period_start'))
        if _is_bva_row(r):
            budget_vs_actual_rows.append(r)
        else:
            portfolio_kpis_periodic.append(r)

    # Universal merge: KPI-shape rows extracted from the valuations_kpis domain
    # (SaaS Metrics sheets, KPI matrix wide-period unpivots) join the periodic
    # KPI stream so the persister can create PortfolioKPI records for
    # mrr / arr / churn_rate / cac / ltv / headcount / etc.
    for r in valuations_kpi_rows:
        if not r.get('company_name'):
            continue
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
