"""Python-computed totals stopgap — formula confidence WITHOUT LibreOffice.

The release gate's LibreOffice recalc verifies the workbook's live formulas; until
that engine is present (or as a permanent second check), this independently
recomputes the headline totals and asserts the ASSEMBLED workbook's written values
match — and that the hole-aware guard fires (a held input → INCOMPLETE, never a
silently-zeroed SUM). Fast: fund schedule only, no MIS extraction.
"""
import os
from decimal import Decimal

from backend.dataimport.preingest3 import fund_anchor, assemble, aggregate
from backend.dataimport.preingest3.cir import CIR, Record, Figure, Provenance

IN = 'backend/media/preingest/trivesta/100e86d5/in'
FUND = ['TFAI_Investments_and_Deployment.xlsx', 'TFAI_Valuations_and_Exits_Q2FY26.xlsx']
_PV = Provenance(source_file='fund', content_fingerprint='', sheet='Portfolio', cell='')


def _cover(wb):
    return {r[0]: r[1] for r in wb['Cover'].iter_rows(values_only=True)
            if r and isinstance(r[0], str)}


def _cells(wb):
    """Every sheet's DATA region as cell values — the comparison unit for the
    workbook determinism gate. Value-based (not raw bytes) by design, so openpyxl's
    zip ordering and core.xml created/modified stamps (the file's only wall-clock)
    are excluded; the sheets themselves stamp no time."""
    return {sn: [tuple(r) for r in wb[sn].iter_rows(values_only=True)] for sn in wb.sheetnames}


def _build(anchors, *, held_revenue=False):
    cir = CIR(as_of='2026-02-28')
    for key, ca in sorted(anchors.items()):
        f = {'company': ca.company}
        if ca.cost_cr is not None:
            f['cost'] = Figure('cost', Decimal(str(ca.cost_cr)), None, _PV)
        if ca.fair_value_cr is not None:
            f['fair_value'] = Figure('fair_value', Decimal(str(ca.fair_value_cr)), None, _PV)
        cir.add(Record('portfolio_investments', entity_id=ca.company, fields=f))
    if held_revenue:   # one held company must turn the revenue total INCOMPLETE
        cir.add(Record('mis', entity_id='HeldCo',
                       fields={'company': 'HeldCo',
                               'revenue': Figure('revenue', None, None, _PV, held=True,
                                                 hold_reason='synthetic held')}))
    return assemble.build(cir)


def test_cost_and_fv_totals_match_independent_sum():
    anchors = fund_anchor.build_fund_anchors([os.path.join(IN, f) for f in FUND])
    exp_cost = sum((a.cost_cr for a in anchors.values() if a.cost_cr is not None), Decimal('0'))
    exp_fv = sum((a.fair_value_cr for a in anchors.values() if a.fair_value_cr is not None), Decimal('0'))
    assert exp_cost == 448 and exp_fv == 827          # junk 'Total' row excluded → no double-count
    cover = _cover(_build(anchors))
    assert cover['Total Deployed Cost'] == float(exp_cost)
    assert cover['Total Fair Value'] == float(exp_fv)


def test_held_input_makes_total_incomplete_not_zero():
    anchors = fund_anchor.build_fund_anchors([os.path.join(IN, f) for f in FUND])
    cover = _cover(_build(anchors, held_revenue=True))
    assert cover['Portfolio Revenue (annualized)'] == assemble.INCOMPLETE   # never 0


# ── the mixed-basis / unknown-period guard (advisor 2026-08-03) ───────────────
def _flow(concept, val, months):
    return Figure(concept, Decimal(str(val)), None, _PV, basis='YTD', months=months)


def test_unknown_period_flow_is_NOT_summed_raw_NEGATIVE_CONTROL():
    # NEGATIVE CONTROL for the mixed-basis guard: a confirmed FLOW whose period is
    # unknown is un-annualisable — it must make the 'annualized' total INCOMPLETE,
    # never be added raw. Before the fix, sum_annualized returned a confident 30
    # (10 raw + 20) — the silent blend. It must now redden to None.
    r = aggregate.sum_annualized([_flow('revenue', 20, 12), _flow('revenue', 10, None)])
    assert r.value is None and r.complete is False        # INCOMPLETE, not 30
    assert r.unresolved_period                             # the culprit is named for disclosure


def test_known_period_flows_still_annualize_and_sum():
    # positive control — the intended path is unchanged: a 6-mo flow annualises ×2.
    r = aggregate.sum_annualized([_flow('revenue', 10, 6), _flow('revenue', 20, 12)])
    assert r.complete and r.value == Decimal('40')         # 10×12/6 + 20


def test_guard_is_nature_scoped_stocks_still_total_448_827():
    # the guard fires on FLOWS only. cost/fair_value are STOCKS with NO period
    # (months=None) and must STILL sum — proving the fix is output-neutral on the
    # real headline totals, not a blanket 'reject anything without months'.
    anchors = fund_anchor.build_fund_anchors([os.path.join(IN, f) for f in FUND])
    cover = _cover(_build(anchors))
    assert cover['Total Deployed Cost'] == 448.0 and cover['Total Fair Value'] == 827.0


def test_cover_names_unknown_period_company_in_disclosure():
    anchors = fund_anchor.build_fund_anchors([os.path.join(IN, f) for f in FUND])
    cir = CIR(as_of='2026-02-28')
    for key, ca in sorted(anchors.items()):
        f = {'company': ca.company}
        if ca.cost_cr is not None:
            f['cost'] = Figure('cost', Decimal(str(ca.cost_cr)), None, _PV)
        cir.add(Record('portfolio_investments', entity_id=ca.company, fields=f))
    cir.add(Record('mis', entity_id='NoPeriodCo',
                   fields={'company': 'NoPeriodCo',
                           'revenue': Figure('revenue', Decimal('12'), None, _PV,
                                             basis='partial', months=None)}))
    row = next(r for r in assemble.build(cir)['Cover'].iter_rows(values_only=True)
               if r and r[0] == 'Portfolio Revenue (annualized)')
    assert row[1] == assemble.INCOMPLETE
    assert 'unknown-period' in (row[2] or '') and 'NoPeriodCo' in (row[2] or '')


# ── output-layer determinism gate (advisor 2026-08-05) ────────────────────────
# The workbook is now a real computation surface (annualisation, comparables,
# cross-company totals) — same class of guard as the CIR determinism gate, at the
# output layer. Same CIR + same config ⇒ identical DATA region.
def _det_cir():
    anchors = fund_anchor.build_fund_anchors([os.path.join(IN, f) for f in FUND])
    cir = CIR(as_of='2026-02-28', rate_card_id='rc_test')
    for key, ca in sorted(anchors.items()):
        f = {'company': ca.company}
        if ca.cost_cr is not None:
            f['cost'] = Figure('cost', Decimal(str(ca.cost_cr)), None, _PV)
        if ca.fair_value_cr is not None:
            f['fair_value'] = Figure('fair_value', Decimal(str(ca.fair_value_cr)), None, _PV)
        cir.add(Record('portfolio_investments', entity_id=ca.company, fields=f))
    # exercise the NEW surface: an annualised flow + a held flow
    cir.add(Record('mis', entity_id='FlowCo',
                   fields={'company': 'FlowCo',
                           'revenue': Figure('revenue', Decimal('10'), None, _PV, basis='YTD', months=6),
                           'ebitda': Figure('ebitda', None, None, _PV, held=True, hold_reason='x')}))
    return cir


def test_workbook_data_region_is_deterministic():
    a = _cells(assemble.build(_det_cir()))
    b = _cells(assemble.build(_det_cir()))
    assert a == b                                          # same CIR + config → identical data
    # comparable columns are nature-correct: flows get ann. cols, stocks do not
    assert a['Portfolio_KPI'][0] == ('Company', 'Revenue', 'EBITDA', 'Cash', 'Head-count',
                                     'Basis', 'Revenue (ann.₹Cr)', 'EBITDA (ann.₹Cr)', 'Notes')


def test_determinism_comparison_is_content_sensitive_NEGATIVE_CONTROL():
    # the gate must not be vacuously green: a changed value MUST make the cells differ,
    # proving _cells actually distinguishes content (else determinism proves nothing).
    base = _cells(assemble.build(_det_cir()))
    cir2 = _det_cir()
    cir2.records[-1].fields['revenue'] = Figure('revenue', Decimal('999'), None, _PV,
                                                basis='YTD', months=6)
    assert _cells(assemble.build(cir2)) != base


# The FUND sections are new computation on the same CIR — extend the determinism gate to
# them, and pin that adding them leaves the MIS sheets byte-identical (new sheets, never
# modifications).
def _fund_det_cir():
    from backend.dataimport.preingest3.fund_terms import FundTerm
    cir = _det_cir()                                       # the MIS baseline, plus fund records
    cir.add(Record('fund_financials', entity_id='fund', fields={'fund': 'fund',
        'called': Figure('called', Decimal('600'), None, _PV, basis='cumulative'),
        'distributed': Figure('distributed', Decimal('70'), None, _PV, basis='cumulative')}))
    for name, com in (('Alpha LP', '400'), ('Beta LP', '600')):
        cir.add(Record('lp_register', entity_id=name, fields={'lp_name': name,
            'commitment': Figure('commitment', Decimal(com), None, _PV, basis='point_in_time')}))
    cir.add(Record('fund_terms', entity_id='fund', fields={'fund': 'fund',
        'management_fee': FundTerm('management_fee', Decimal('0.02'), 'percent', 'committed_capital',
                                   'investment_period', None, _PV),
        'carried_interest': FundTerm('carried_interest', Decimal('0.2'), 'percent', 'n/a', 'n/a', None, _PV)}))
    return cir


def test_fund_sheets_are_deterministic_and_leave_mis_byte_identical():
    mis_only = _cells(assemble.build(_det_cir()))
    a = _cells(assemble.build(_fund_det_cir()))
    b = _cells(assemble.build(_fund_det_cir()))
    assert a == b                                          # same CIR + config → identical fund region
    assert 'Fund_Summary' in a and 'LP_Register' in a and 'Fund_Terms' in a   # new surface present
    # the MIS-DATA sheets are byte-identical to the fund-less build — fund data never
    # touches them (the cross-cutting audit sheets _Provenance/_Disclosures CORRECTLY
    # grow with fund figures, so they are not in this set).
    for sn in ('Cover', 'Portfolio_KPI'):
        assert a[sn] == mis_only[sn], f'fund sections perturbed MIS-data sheet {sn!r}'


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'ok  {name}')
    print('ALL PASS — cost 448 / fv 827; held & unknown-period → INCOMPLETE not 0; '
          'guard nature-scoped; workbook data region deterministic')
