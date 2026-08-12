"""
Stage 7 — Assembly (CODE). Schema-driven template engine.

Iterates the Stage-0 schema and produces, per sheet, {columns, rows}. Extracted
records fill table/key_value/line_item/per_company sheets; the formula engine
fills the derived sheets (Cover, NAV Calculation, Waterfall); code aggregations
fill Sector Allocation and Realised & Unrealised. No number is fabricated: a
value is a copied cell, a code aggregation, or a formula.html derivation.
"""
from ..phase6_extractor.helpers import slug
from . import schema as sch
from .formulas import FundMetrics

# schema domain -> old canonical extraction buckets produced by the reused mover
DOMAIN_ALIAS = {
    'fund_master': ['fund_scheme_master'],
    'lp_register': ['investors_aml', 'commitments', 'lp_capital_accounts'],
    'capital_calls': ['capital_calls'],
    'investments': ['portfolio_investments'],
    'tranches': ['investment_tranches'],
    'valuations': ['valuations_kpis'],
    'distributions': ['exits_distributions'],
    'exits': ['exits_distributions'],
    'fees': ['fees_register'],
    'fund_pl': ['fund_pl_bs'],
    'budget_vs_actual': ['financials_pl_bva'],
    'compliance': ['compliance'],
}
FIELD_SYNONYMS = {
    'inv_id': ['inv_id', 'company_cin'],
    'geography': ['geography', 'headquarters_country'],
    'instrument_type': ['instrument_type', 'instrument'],
    'fair_value_of_holding': ['fair_value_of_holding', 'fair_value'],
    'moic': ['moic', 'multiple'],
}


def _norm(s):
    return slug(s) if s else ''


def _f(v):
    try:
        return float(str(v).replace(',', '').strip())
    except (TypeError, ValueError):
        return None


def _get(rec, source, ctx):
    if source is None:
        return None
    for f in FIELD_SYNONYMS.get(source, [source]):
        v = rec.get(f)
        if v not in (None, ''):
            return v
    return None


def _recs(dom, domains):
    out = []
    for d in domains or []:
        for old in DOMAIN_ALIAS.get(d, [d]):
            out.extend(dom.get(old, []))
    return out


def _passes(rec, flt):
    if not flt:
        return True
    if flt == 'distribution':
        return any(rec.get(k) not in (None, '') for k in
                   ('distribution_number', 'distribution_date', 'distribution_type',
                    'total_gross_amount', 'total_net_amount'))
    if flt == 'exit':
        return any(rec.get(k) not in (None, '') for k in
                   ('exit_date', 'exit_type', 'proceeds', 'net_exit_proceeds'))
    if flt == 'compliance_check':
        return any(rec.get(k) not in (None, '') for k in
                   ('check_description', 'check_status', 'regulation_reference'))
    if flt == 'calendar':
        return any(rec.get(k) not in (None, '') for k in
                   ('calendar_title', 'due_date', 'calendar_status'))
    return True


def _tranche_totals(dom):
    tot = {}
    for r in dom.get('investment_tranches', []):
        name = r.get('company_name')
        if sch.is_summary_label(name):        # skip 'total deployed' roll-up rows
            continue
        k = _norm(name)
        a = _f(r.get('tranche_amount'))
        if k and a is not None:
            tot[k] = tot.get(k, 0.0) + a
    return tot


def _company_index(dom):
    idx = {}
    for r in dom.get('portfolio_investments', []):
        k = _norm(r.get('company_name'))
        if k:
            idx.setdefault(k, {})
            idx[k].setdefault('inv_id', r.get('company_cin') or r.get('inv_id'))
            idx[k].setdefault('sector', r.get('sector'))
    for r in dom.get('valuations_kpis', []):
        k = _norm(r.get('company_name'))
        fv = r.get('fair_value_of_holding') or r.get('fair_value')
        if k and fv not in (None, ''):
            idx.setdefault(k, {}).setdefault('fund_fv', fv)
    return idx


def _match(idx, name):
    k = _norm(name)
    if k in idx:
        return idx[k]
    for kk, v in idx.items():
        if kk and (kk in k or k in kk):
            return v
    return {}


# ── per-mode builders ────────────────────────────────────────────────────────
def _table(sheet, dom, ctx):
    cols = sheet['columns']
    recs = _recs(dom, sheet.get('domains'))
    flt = sheet.get('row_filter')
    tt = ctx['tranche_totals']
    rows = []
    for r in recs:
        if not _passes(r, flt):
            continue
        if sheet['key'] in ('investment_tranches', 'investments') \
                and sch.is_summary_label(r.get('company_name')):
            continue
        row = []
        for c in cols:
            v = _get(r, c['source'], ctx)
            if v in (None, '') and c['header'].startswith('Total Invested'):
                v = tt.get(_norm(r.get('company_name')))
            row.append(v)
        if all(v in (None, '') for v in row):
            continue
        rows.append(row)
    return {'columns': [c['header'] for c in cols], 'rows': rows}


def _key_value(sheet, dom, ctx):
    recs = _recs(dom, sheet.get('domains'))
    rows = []
    for r in recs:
        label = r.get('line_item') or r.get('parameter')
        if label not in (None, ''):
            rows.append([label, r.get('value'), r.get('note') or ''])
    return {'columns': ['Parameter', 'Value', 'Basis / Note'], 'rows': rows}


def _line_item(sheet, dom, ctx):
    recs = _recs(dom, sheet.get('domains'))
    rows = []
    for r in recs:
        label = r.get('line_item')
        if label in (None, ''):
            continue
        rows.append([label, r.get('amount', r.get('value')),
                     r.get('period') or r.get('statement_type') or ''])
    return {'columns': ['Line Item', 'Amount', 'Period / Note'], 'rows': rows}


def _per_company(sheet, ctx):
    cols = sheet['columns']
    idx = ctx['company_index']
    rows = []
    for cr in ctx['company_rows']:
        j = _match(idx, cr.get('company'))
        m = dict(cr)
        for k in ('inv_id', 'sector', 'fund_fv'):
            if m.get(k) in (None, '') and j.get(k) not in (None, ''):
                m[k] = j[k]
        rows.append([m.get(c['source']) for c in cols])
    return {'columns': [c['header'] for c in cols], 'rows': rows}


def _sector_rollup(sheet, dom, ctx):
    tt = ctx['tranche_totals']
    cost, fv, cnt = {}, {}, {}
    for r in dom.get('portfolio_investments', []):
        sec = r.get('sector')
        if not sec:
            continue
        c = _f(r.get('total_invested')) or tt.get(_norm(r.get('company_name')))
        cnt[sec] = cnt.get(sec, 0) + 1
        if c is not None:
            cost[sec] = cost.get(sec, 0.0) + c
    for r in dom.get('valuations_kpis', []):
        sec = r.get('sector')
        v = _f(r.get('fair_value_of_holding') or r.get('fair_value'))
        if sec and v is not None:
            fv[sec] = fv.get(sec, 0.0) + v
    total_fv = sum(fv.values()) or None
    rows = []
    for sec in sorted(set(cost) | set(fv) | set(cnt)):
        c, v = cost.get(sec), fv.get(sec)
        tv = (c or 0) + (v or 0) if (c is not None or v is not None) else None
        moic = (v / c) if (c and v is not None) else None
        pct = (v / total_fv) if (v is not None and total_fv) else None
        rows.append([sec, cnt.get(sec), c, v, tv, moic, pct])
    return {'columns': [c['header'] for c in sheet['columns']], 'rows': rows}


def _realised_unrealised(sheet, dom, ctx):
    tt = ctx['tranche_totals']
    # per company: cost (tranches), realised (exit proceeds), residual (FV)
    exits = {}
    for r in dom.get('exits_distributions', []):
        if r.get('exit_date') or r.get('proceeds') or r.get('net_exit_proceeds'):
            k = _norm(r.get('company_name'))
            p = _f(r.get('net_exit_proceeds')) or _f(r.get('proceeds'))
            if k and p is not None:
                exits[k] = exits.get(k, 0.0) + p
    fv = {}
    for r in dom.get('valuations_kpis', []):
        k = _norm(r.get('company_name'))
        v = _f(r.get('fair_value_of_holding') or r.get('fair_value'))
        if k and v is not None:
            fv[k] = v
    rows = []
    for r in dom.get('portfolio_investments', []):
        name = r.get('company_name')
        k = _norm(name)
        cost = _f(r.get('total_invested')) or tt.get(k)
        real = exits.get(k)
        resid = fv.get(k)
        tv = (real or 0) + (resid or 0) if (real is not None or resid is not None) else None
        gm = (tv / cost) if (cost and tv is not None) else None
        pr = (real / tv) if (tv and real is not None) else None
        rows.append([r.get('company_cin') or r.get('inv_id'), name, cost, real, resid, tv, gm, pr])
    return {'columns': [c['header'] for c in sheet['columns']], 'rows': rows}


def _derived(sheet, dom, ctx):
    m = ctx['metrics']
    key = sheet['derive']
    if key == 'cover_snapshot':
        terms_name = None
        for r in dom.get('fund_scheme_master', []):
            li = str(r.get('line_item') or '').lower()
            if 'fund name' in li:
                terms_name = r.get('value')
        n_cos = len({_norm(r.get('company_name')) for r in dom.get('portfolio_investments', [])
                     if r.get('company_name')}) or None
        nav, _ = m.fund_nav()
        rows = [
            ['Fund Name', terms_name],
            ['Total Commitments (₹Cr)', m.total_commitments()],
            ['Total Called Capital (₹Cr)', m.total_called()],
            ['Uncalled Commitments (₹Cr)', m.uncalled()],
            ['Total Invested / Deployment (₹Cr)', m.total_invested()],
            ['No. of Portfolio Companies', n_cos],
            ['Total Distributions (₹Cr)', m.total_distributions()],
            ['Total Realised Proceeds (₹Cr)', m.total_realised()],
            ['Fund NAV (₹Cr)', nav],
            ['NAV per Unit (₹ Lakhs)', m.nav_per_unit()],
            ['Residual NAV (₹Cr)', m.residual_nav()],
            ['Gross Portfolio MOIC', m.moic()],
            ['Net TVPI', m.tvpi()],
            ['DPI', m.dpi()],
            ['RVPI', m.rvpi()],
            ['Net IRR (%)', m.net_irr()],
            ['Carry Base (₹Cr)', m.carry_base()],
            ['GP Carry Gross (₹Cr)', m.gp_carry_gross()],
            ['GP Carry Net (₹Cr)', m.gp_carry_net()],
        ]
        return {'columns': ['Metric', 'Value'], 'rows': rows}
    if key == 'nav_buildup':
        nav, comps = m.fund_nav()
        rows = [[label, amt, ''] for label, amt in (comps or [])]
        if nav is not None:
            rows.append(['= Fund NAV', nav, 'Σ components'])
            rows.append(['NAV per Unit (₹ Lakhs)', m.nav_per_unit(), 'NAV×100÷units'])
        return {'columns': ['Line Item', 'Amount (₹Cr)', 'Source'], 'rows': rows}
    if key == 'waterfall':
        rows = [
            ['Total Called Capital', m.total_called()],
            ['Total Distributed', m.total_distributions()],
            ['Return of Capital (Step 1)', m.return_of_capital()],
            ['Preferred Return / Hurdle', m.preferred_return()],
            ['GP Catch-Up', m.catch_up()],
            ['Carry Base', m.carry_base()],
            ['GP Carry Gross', m.gp_carry_gross()],
            ['Clawback Provision', m.clawback()],
            ['GP Carry Net', m.gp_carry_net()],
            ['Hurdle Rate (%)', m.terms.get('hurdle_pct')],
            ['Carry (%)', m.terms.get('carry_pct')],
            ['GP Holdback (%)', m.terms.get('holdback_pct')],
        ]
        return {'columns': ['Parameter', 'Value'], 'rows': rows}
    return {'columns': [], 'rows': []}


def assemble(dom, company_rows):
    """dom = merged old-domain buckets (fund files). company_rows = Type-B rows.
    Returns ({sheet_key: {name,title,columns,rows}}, metrics)."""
    metrics = FundMetrics(dom)
    ctx = {'tranche_totals': _tranche_totals(dom),
           'company_index': _company_index(dom),
           'company_rows': company_rows, 'metrics': metrics}
    out = {}
    for sheet in sch.SHEETS:
        mode = sheet['mode']
        if mode == 'table':
            b = _table(sheet, dom, ctx)
        elif mode == 'key_value':
            b = _key_value(sheet, dom, ctx)
        elif mode == 'line_item':
            b = _line_item(sheet, dom, ctx)
        elif mode == 'per_company':
            b = _per_company(sheet, ctx)
        elif mode == 'sector_rollup':
            b = _sector_rollup(sheet, dom, ctx)
        elif mode == 'realised_unrealised':
            b = _realised_unrealised(sheet, dom, ctx)
        elif mode == 'derived':
            b = _derived(sheet, dom, ctx)
        else:
            b = {'columns': [], 'rows': []}
        out[sheet['key']] = {'name': sheet['name'], 'title': sheet['title'],
                             'columns': b['columns'], 'rows': b['rows']}
    return out, metrics
