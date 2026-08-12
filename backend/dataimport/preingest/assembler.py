"""
Assemble canonical records into the FIXED Fund Master template.

Everything here is deterministic Python data-operations (map, filter, group,
join). It consumes:
  • fund_records  : {domain: [canonical record, ...]} from the universal extractor,
                    restricted to FUND workbooks.
  • company_rows  : [Portfolio-row dict, ...] one per company MIS workbook.
It produces, per output sheet: {'columns': [...display headers...], 'rows': [[...]]}.

No metric is computed beyond light, exact data operations (sector totals, a
Total = Realised + Residual, a straight MOIC = value/cost). No IRR/NAV/waterfall
math is invented — those sheets are extracted verbatim from source statements.
"""
import re

from ..phase6_extractor.helpers import slug
from . import output_template as tmpl


def _norm(s):
    return slug(s) if s else ''


def _first_present(rec, fields):
    for f in fields:
        v = rec.get(f)
        if v not in (None, ''):
            return v
    return None


def _get(rec, spec, ctx=None):
    """Resolve one output column spec against a record.
      None                         -> blank (a slot we never fabricate)
      "<canonical_field>"          -> rec[field]
      {"any": [f1, f2, ...]}       -> first non-empty of the synonyms
      {"any": [...], "derive": ...}-> synonyms first, else a derivation
    Derivations (light, deterministic data-ops only):
      "tranche_sum" (by='<field>') -> sum of tranche amounts for rec[by]
    """
    if spec is None:
        return None
    if isinstance(spec, str):
        return rec.get(spec)
    if isinstance(spec, dict):
        v = _first_present(rec, spec.get('any', []))
        if v not in (None, ''):
            return v
        derive = spec.get('derive')
        if derive == 'tranche_sum' and ctx:
            key = _norm(rec.get(spec.get('by', 'company_name')))
            return ctx.get('tranche_totals', {}).get(key)
    return None


# ── row filters (universal: by which canonical fields a record carries) ──────
def _passes_filter(rec, flt):
    if not flt:
        return True
    if flt == 'distribution':
        return any(rec.get(k) not in (None, '') for k in
                   ('distribution_number', 'distribution_date', 'distribution_type',
                    'total_gross_amount', 'total_net_amount'))
    if flt == 'exit':
        return any(rec.get(k) not in (None, '') for k in
                   ('exit_date', 'exit_type', 'proceeds', 'net_exit_proceeds',
                    'realized_gain_loss'))
    if flt == 'compliance_check':
        return any(rec.get(k) not in (None, '') for k in
                   ('check_description', 'check_status', 'regulation_reference'))
    if flt == 'calendar':
        return any(rec.get(k) not in (None, '') for k in
                   ('calendar_title', 'due_date', 'calendar_status'))
    return True


def _records_for(fund_records, domains):
    out = []
    for d in (domains or []):
        out.extend(fund_records.get(d, []))
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── per-mode builders ────────────────────────────────────────────────────────
def _build_table(sheet, fund_records, ctx):
    cols = sheet['columns']
    recs = _records_for(fund_records, sheet['source_domains'])
    flt = sheet.get('row_filter')
    rows = []
    for r in recs:
        if not _passes_filter(r, flt):
            continue
        if sheet.get('fund_only') and r.get('__source_kind__') == 'company_mis':
            continue
        row = [_get(r, cf, ctx) for _h, cf in cols]
        if all(v in (None, '') for v in row):
            continue
        rows.append(row)
    return {'columns': [h for h, _ in cols], 'rows': rows}


_PROV = ('__source_file__', '__source_sheet__', '__entity__', '__unit__',
         '__currency__', '__unit_confidence__', '__domain__', '__source_kind__')


def _build_key_value(sheet, fund_records):
    """Parameter | Value | Note. Works whether the source master sheet was read
    as key_value (line_item/value records) OR tabular (canonical fields) — a
    tabular record is melted into one Parameter/Value pair per field."""
    recs = _records_for(fund_records, sheet['source_domains'])
    rows = []
    for r in recs:
        label = r.get('line_item') or r.get('parameter')
        if label not in (None, ''):
            rows.append([label, r.get('value'), r.get('note') or ''])
            continue
        for k, v in r.items():
            if k in _PROV or k in ('line_item', 'value', 'parameter', 'note'):
                continue
            if v in (None, ''):
                continue
            rows.append([k.replace('_', ' ').title(), v, ''])
    return {'columns': ['Parameter', 'Value', 'Basis / Note'], 'rows': rows}


def _build_line_item(sheet, fund_records):
    recs = _records_for(fund_records, sheet['source_domains'])
    rows = []
    for r in recs:
        label = r.get('line_item')
        if label in (None, ''):
            continue
        amount = r.get('amount', r.get('value'))
        rows.append([label, amount, r.get('period') or r.get('statement_type') or ''])
    return {'columns': ['Line Item', 'Amount', 'Period / Note'], 'rows': rows}


def _company_index(fund_records):
    """company_name(normalized) -> {inv_id, sector, fund_fv} from the fund's
    investments + valuations, for joining onto the Portfolio_KPI rows."""
    idx = {}
    for r in fund_records.get('portfolio_investments', []):
        nm = _norm(r.get('company_name'))
        if not nm:
            continue
        idx.setdefault(nm, {})
        idx[nm].setdefault('inv_id', r.get('company_cin'))
        idx[nm].setdefault('sector', r.get('sector'))
    for r in fund_records.get('valuations_kpis', []):
        nm = _norm(r.get('company_name'))
        if not nm:
            continue
        fv = r.get('fair_value_of_holding') or r.get('fair_value')
        if fv not in (None, ''):
            idx.setdefault(nm, {})
            idx[nm].setdefault('fund_fv', fv)
    return idx


def _match(idx, company):
    """Universal fuzzy join: exact normalized match, else containment either way
    (handles 'Hubbler (Hubler)' vs 'Hubler')."""
    nm = _norm(company)
    if nm in idx:
        return idx[nm]
    for k, v in idx.items():
        if k and (k in nm or nm in k):
            return v
    return {}


def _build_per_company(sheet, company_rows, fund_records):
    cols = sheet['columns']
    idx = _company_index(fund_records)
    rows = []
    for cr in company_rows:
        joined = _match(idx, cr.get('company'))
        merged = dict(cr)
        for k in ('inv_id', 'sector', 'fund_fv'):
            if merged.get(k) in (None, '') and joined.get(k) not in (None, ''):
                merged[k] = joined[k]
        rows.append([_get(merged, cf) for _h, cf in cols])
    return {'columns': [h for h, _ in cols], 'rows': rows}


def _build_cover(sheet, fund_records, company_rows, ctx):
    """Fund snapshot — extracted facts + light deterministic counts/sums only.
    No IRR/NAV/MOIC is computed; those appear only if present in the source
    master sheet."""
    master = _records_for(fund_records, ['fund_scheme_master'])
    master_kv = {}
    for r in master:
        li = r.get('line_item')
        if li not in (None, ''):
            master_kv[str(li).strip().lower()] = r.get('value')

    def find(*keys):
        for k in keys:
            for lk, v in master_kv.items():
                if k in lk:
                    return v
        return None

    lp_recs = fund_records.get('investors_aml', []) + fund_records.get('commitments', [])
    total_commit = sum((_num(r.get('commitment_amount')) or 0) for r in lp_recs) or None
    total_called = sum((_num(r.get('cumulative_called')) or 0)
                       for r in fund_records.get('investors_aml', [])) or None
    total_invested = sum(ctx.get('tranche_totals', {}).values()) or None
    n_cos = len({_norm(r.get('company_name'))
                 for r in fund_records.get('portfolio_investments', [])
                 if r.get('company_name')}) or None
    rows = [
        ['Fund Name', find('fund name'), ''],
        ['Total Commitments (₹Cr)', total_commit, 'Σ LP commitments'],
        ['Total Called Capital (₹Cr)', total_called, 'Σ LP called'],
        ['Total Invested (₹Cr)', total_invested, 'Σ tranche amounts'],
        ['No. of Portfolio Companies', n_cos, 'count of investments'],
        ['Fund NAV (₹Cr)', find('fund nav', 'total nav'), 'from source if present'],
        ['Net TVPI', find('tvpi'), 'from source if present'],
        ['Gross MOIC', find('gross moic', 'moic'), 'from source if present'],
        ['DPI', find('dpi'), 'from source if present'],
        ['Net IRR', find('net irr', 'irr'), 'from source if present'],
    ]
    return {'columns': ['Metric', 'Value', 'Basis'], 'rows': rows}


def _build_sector_rollup(sheet, fund_records, ctx):
    tranche_totals = ctx.get('tranche_totals', {})
    cost, fv = {}, {}
    for r in fund_records.get('portfolio_investments', []):
        sec = r.get('sector')
        if not sec:
            continue
        c = _num(r.get('total_invested'))
        if c is None:                       # no direct cost -> derive from tranches
            c = tranche_totals.get(_norm(r.get('company_name')))
        if c is not None:
            cost[sec] = cost.get(sec, 0.0) + c
    for r in fund_records.get('valuations_kpis', []):
        sec = r.get('sector')
        v = _num(r.get('fair_value_of_holding') or r.get('fair_value'))
        if sec and v is not None:
            fv[sec] = fv.get(sec, 0.0) + v
    total_fv = sum(fv.values()) or None
    rows = []
    for sec in sorted(set(cost) | set(fv)):
        c = cost.get(sec)
        v = fv.get(sec)
        tv = (c or 0) + (v or 0) if (c is not None or v is not None) else None
        moic = (v / c) if (c and v is not None) else None
        pct = (v / total_fv) if (v is not None and total_fv) else None
        rows.append([sec, c, v, tv, moic, pct])
    return {'columns': [h for h, _ in sheet['columns']], 'rows': rows}


def _tranche_totals(fund_records):
    totals = {}
    for r in fund_records.get('investment_tranches', []):
        key = _norm(r.get('company_name'))
        amt = _num(r.get('tranche_amount'))
        if key and amt is not None:
            totals[key] = totals.get(key, 0.0) + amt
    return totals


def assemble(fund_records, company_rows):
    """Return {sheet_key: {'name','title','columns','rows'}} for every template
    sheet (empty sheets included, so the output always has the full structure)."""
    ctx = {'tranche_totals': _tranche_totals(fund_records)}
    out = {}
    for sheet in tmpl.SHEETS:
        mode = sheet['mode']
        if mode == 'table':
            built = _build_table(sheet, fund_records, ctx)
        elif mode == 'key_value':
            built = _build_key_value(sheet, fund_records)
        elif mode == 'line_item':
            built = _build_line_item(sheet, fund_records)
        elif mode == 'per_company':
            built = _build_per_company(sheet, company_rows, fund_records)
        elif mode == 'sector_rollup':
            built = _build_sector_rollup(sheet, fund_records, ctx)
        elif mode == 'snapshot':
            built = _build_cover(sheet, fund_records, company_rows, ctx)
        else:
            built = {'columns': [], 'rows': []}
        out[sheet['key']] = {
            'name': sheet['name'], 'title': sheet['title'],
            'columns': built['columns'], 'rows': built['rows'],
        }
    return out
