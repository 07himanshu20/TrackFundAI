"""Unit guard on the portfolio-vs-fund soft correspondence (U7 flagship). Synthetic CIRs — fast,
deterministic, no file I/O. Proves the TWO axes and their order: COVERAGE-first (a held/absent
company contributes 0 and the row reads as a completeness meter, not a bare variance) and
BASIS-second (a part-year flow is annualised; an un-annualisable one is excluded, never guessed).
Plus fire-when-present/disclose-when-absent and the reference-only segregation."""
from decimal import Decimal

from backend.dataimport.preingest3.cir import CIR, Record, Figure
from backend.dataimport.preingest3 import portfolio_correspondence as pc


def _fig(concept, value, *, months=None, basis=None, held=False, hold_reason='', stale=False):
    return Figure(concept, (None if held else Decimal(str(value))), None, None,
                  held=held, basis=basis, months=months, hold_reason=hold_reason, stale=stale)


def _roster(cir, *names):
    for n in names:
        cir.add(Record('portfolio_investments', entity_id=n, fields={'company': n}))


def _mis(cir, name, **figs):
    cir.add(Record('mis', entity_id=name, fields=figs))


def _comp(concept, budget, actual):
    return {'concept': concept, 'budget': str(budget), 'actual': str(actual),
            'basis': 'annualised', 'actual_cell': 'S!D1', 'reference_only': True}


# ── _annualise_cr: the basis axis ────────────────────────────────────────────
def test_annualise_partial_scales_by_months():
    ann, ok, _ = pc._annualise_cr(_fig('revenue', '5.5', months=11, basis='partial'))
    assert ok and ann == Decimal('6.0')                        # 5.5 × 12/11


def test_annualise_full_year_is_left_as_is():
    for basis, m in (('FY', None), ('TTM', None), (None, 12)):
        ann, ok, _ = pc._annualise_cr(_fig('revenue', '4.0', months=m, basis=basis))
        assert ok and ann == Decimal('4.0')


def test_annualise_refuses_when_not_annualisable():
    ann, ok, _ = pc._annualise_cr(_fig('revenue', '4.0', months=None, basis='partial'))
    assert not ok and ann is None                              # no months, not full-year → exclude+disclose


# ── build(): coverage-first framing ──────────────────────────────────────────
def _low_cov_cir():
    cir = CIR(as_of='2026-06-30')
    _roster(cir, 'A', 'B', 'C', 'D')                           # 4 portfolio companies
    _mis(cir, 'A', revenue=_fig('revenue', '5.5', months=11, basis='partial'),
                   ebitda=_fig('ebitda', '1.0', months=12, basis='FY'))
    _mis(cir, 'B', revenue=_fig('revenue', '4.0', months=12, basis='FY'))
    _mis(cir, 'C', revenue=_fig('revenue', '9', held=True, hold_reason='MYR rate uncovered'))
    # D: no MIS at all
    cir.add_comparator(_comp('portfolio_revenue', 100, 120))
    cir.add_comparator(_comp('portfolio_ebitda', 10, 12))
    return cir


def test_flagship_row_is_coverage_led_completeness_meter():
    by = {c['id']: c for c in pc.build(_low_cov_cir())}
    rev = by['portfolio_revenue_vs_fund_actual']
    assert rev['class'] == 'soft'
    assert rev['coverage_n'] == 2 and rev['coverage_total'] == 4     # A,B contribute; C held; D no-MIS
    assert Decimal(rev['a']) == Decimal('10.0')                      # Σ = annualised(5.5,11mo)=6.0 + 4.0
    assert Decimal(rev['b']) == Decimal('120')                       # vs fund ACTUAL (completeness anchor)
    assert rev['detail'].startswith('COVERAGE 2/4')                  # coverage LEADS the explanation
    assert rev['excluded_held'] == 1 and rev['excluded_no_mis'] == 1


def test_budget_row_is_context_only_below_coverage_floor():
    by = {c['id']: c for c in pc.build(_low_cov_cir())}
    bud = by['portfolio_revenue_vs_fund_budget']
    assert bud['performance_meaningful'] is False                    # 10/120 = 8% coverage
    assert 'CONTEXT ONLY' in bud['detail'] and 'NOT a performance verdict' in bud['detail']


def test_budget_row_becomes_a_performance_verdict_at_high_coverage():
    cir = CIR(as_of='2026-06-30')
    _roster(cir, 'A', 'B')
    _mis(cir, 'A', revenue=_fig('revenue', '60', months=12, basis='FY'))
    _mis(cir, 'B', revenue=_fig('revenue', '50', months=12, basis='FY'))
    cir.add_comparator(_comp('portfolio_revenue', 100, 115))        # Σ=110 vs actual 115 → 95.6% coverage
    bud = {c['id']: c for c in pc.build(cir)}['portfolio_revenue_vs_fund_budget']
    assert bud['performance_meaningful'] is True
    assert 'PERFORMANCE-vs-PLAN' in bud['detail']


# ── fire-when-present / disclose-when-absent ────────────────────────────────
def test_absent_fund_comparator_discloses_never_fabricates():
    cir = CIR(as_of='2026-06-30')
    _roster(cir, 'A')
    _mis(cir, 'A', revenue=_fig('revenue', '5', months=12, basis='FY'))
    row = {c['id']: c for c in pc.build(cir)}['portfolio_revenue_vs_fund']
    assert row['class'] == 'disclosure' and row['status'] == 'disclosed'


def test_no_company_figure_emitted_discloses_not_computable():
    cir = CIR(as_of='2026-06-30')
    _roster(cir, 'A')
    _mis(cir, 'A', revenue=_fig('revenue', '9', held=True, hold_reason='held'))
    cir.add_comparator(_comp('portfolio_revenue', 100, 120))
    row = {c['id']: c for c in pc.build(cir)}['portfolio_revenue_vs_fund']
    assert row['class'] == 'disclosure'                             # nothing to sum → not fabricated


# ── segregation — PROVENANCE-based, collision-proof (belt-and-suspenders) ────
# A comparator VALUE-scan false-positives on round numbers: a fund EBITDA budget of 150 collides
# with a company fair_value / LP commitment / capital call that legitimately equal 150 from OTHER
# cells (proven on the real files). The real guarantee is a comparator is never a Figure and no
# emitted figure is SOURCED FROM a comparator's cell — checked on (sheet, cell), never value.
from backend.dataimport.preingest3.cir import Provenance


def _comparator_source_leaks(cir):
    """Emitted figures whose provenance points at a comparator's source cell — a true segregation
    breach (as opposed to a mere value coincidence)."""
    cells = set()
    for c in cir.comparators:
        cells |= {c.get('actual_cell'), c.get('budget_cell')}
    cells.discard('')
    cells.discard(None)
    out = []
    for r in cir.records:
        for f in r.figures():
            p = f.provenance
            if p and p.cell and f'{p.sheet}!{p.cell}' in cells:
                out.append((r.entity_id, f.concept))
    return out


def test_segregation_holds_even_when_a_comparator_value_collides_with_an_actual():
    cir = CIR(as_of='2026-06-30')
    _roster(cir, 'A')
    # an emitted actual that COINCIDENTALLY equals the comparator value 120, from a DIFFERENT cell
    coincident = Figure('fair_value', Decimal('120'), None,
                        Provenance('f', '', 'Portfolio', 'C3'), basis='point_in_time')
    cir.records[-1].fields['fair_value'] = coincident
    cir.add_comparator({'concept': 'portfolio_revenue', 'budget': '100', 'actual': '120',
                        'basis': 'annualised', 'budget_cell': 'Budget vs Act!C11',
                        'actual_cell': 'Budget vs Act!D11', 'reference_only': True})
    assert _comparator_source_leaks(cir) == []          # value collides, PROVENANCE does not → no leak


def test_segregation_detector_reddens_on_an_injected_leak():
    cir = CIR(as_of='2026-06-30')
    _roster(cir, 'A')
    cir.add_comparator({'concept': 'portfolio_revenue', 'budget': '1150', 'actual': '1284',
                        'basis': 'annualised', 'budget_cell': 'Budget vs Act!C11',
                        'actual_cell': 'Budget vs Act!D11', 'reference_only': True})
    # inject the exact breach the construction forbids: an emitted figure sourced from the comparator cell
    leak = Figure('revenue', Decimal('1284'), None,
                  Provenance('f', '', 'Budget vs Act', 'D11'))
    cir.records[-1].fields['leaked'] = leak
    assert _comparator_source_leaks(cir)                # negative control: the detector CATCHES it


# ── #3 realisations-vs-exits: concept-matched gross-to-gross ─────────────────
def _exit(eid, gross):
    return Record('exits', entity_id=eid,
                  fields={'gross_proceeds': Figure('gross_proceeds', Decimal(str(gross)), None, None)})


def test_realisations_vs_exits_ties_gross_to_gross():
    cir = CIR(as_of='2026-06-30')
    for eid, g in (('X1', 45), ('X2', 22), ('X3', 14)):     # Σ = 81
        cir.add(_exit(eid, g))
    cir.add_comparator({'concept': 'realised_gross', 'actual': '81', 'budget': None,
                        'actual_cell': 'real vs unreal!F5', 'reference_only': True})
    row = {c['id']: c for c in pc.build(cir)}['realisations_vs_exits']
    assert row['class'] == 'soft' and row['status'] == 'pass'      # 81 == 81
    assert Decimal(row['a']) == Decimal('81') and Decimal(row['b']) == Decimal('81')
    assert 'gross-to-gross' in row['detail']


def test_realisations_vs_exits_discloses_when_no_fund_gross():
    cir = CIR(as_of='2026-06-30')
    cir.add(_exit('X1', 45))
    row = {c['id']: c for c in pc.build(cir)}['realisations_vs_exits']
    assert row['class'] == 'disclosure'                           # fire-when-present: no comparator → disclose
