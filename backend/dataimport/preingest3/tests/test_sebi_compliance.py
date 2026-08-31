"""Reddening controls for the SEBI compliance-reconciliation (finance build 3).

Proves the engine is DECLARATIVE + category-aware (P2), CHECKS not fits (P4 — investable_funds
never back-solved), BASIS-declared (P3 — cost÷investable, never FV÷totalFV), and FAIL-CLOSED (P1/P5).
Includes the NEGATIVE CONTROL for the reg-ref collision bug the parenthetical-preserving matcher fixes."""
from decimal import Decimal as D

from backend.dataimport.preingest3 import sebi_compliance as S
from backend.dataimport.preingest3 import reconcile, lexicon
from backend.dataimport.preingest3.cir import Record, Figure, Provenance


# ── a real-shaped compliance checklist grid (position-independent; located by header + ref) ──
def _grid(remarks=None):
    remarks = remarks or {}
    def rk(ref, default):
        return remarks.get(ref, default)
    rows = [
        [None, 'SEBI (AIF) Regn 2012 - Cat II checklist'],
        [],
        ['#', 'requirement', 'ref', 'norm', 'status', 'remark'],
        ['1', 'Registration', 'Reg 3(4)(b)', 'Registered', 'Compliant', 'cert valid'],
        ['2', 'Minimum corpus', 'Reg 10(b)', '>= Rs 20 Cr', 'Compliant', rk('10(b)', 'corpus Rs 1000 Cr')],
        ['3', 'Min investor commitment', 'Reg 10(c)', '>= Rs 1 Cr', 'Compliant', rk('10(c)', 'smallest LP Rs 25 Cr')],
        ['4', 'Max investors', 'Reg 10(f)', '<= 1000', 'Compliant', rk('10(f)', '10 LPs')],
        ['5', 'Sponsor', 'Reg 10(d)', '>= 2.5% or Rs 5 Cr', 'Compliant', rk('10(d)', 'GP Rs 25 Cr = 2.5%')],
        ['6', 'Concentration', 'Reg 15(1)(c)', '<= 25% investable funds', 'Compliant', rk('15(1)(c)', 'largest ~11.6%')],
        ['7', 'Min tenure', 'Reg 13(a)', '>= 3 yrs', 'Compliant', rk('13(a)', '8+2 yr')],
    ]
    return [{'SEBI compliance': rows}]


def _prim(**over):
    base = dict(category='II', corpus_cr=D('1000'),
                commitments_cr=(D('180'), D('150'), D('140'), D('120'), D('100'),
                                D('80'), D('70'), D('75'), D('60'), D('25')),
                lp_count=10, sponsor_commitment_cr=D('25'), largest_cost_cr=D('110'),
                investable_funds_cr=None, tenure_years=D('8'))
    base.update(over)
    return S.CompliancePrimitives(**base)


def _by_id(checks):
    return {c['id']: c for c in checks}


# ══════════════════════════════════════════════════════════════════════════════════
# NEGATIVE CONTROL — the reg-ref collision bug the parenthetical-preserving matcher fixes
# ══════════════════════════════════════════════════════════════════════════════════
def test_reg_ref_matcher_preserves_the_parenthetical_discriminator():
    # the label normaliser COLLAPSES 10(b)/(c)/(d)/(f) → all '10' (the bug: wrong-row matches)…
    assert (lexicon.normalise_label('Reg 10(b)') == lexicon.normalise_label('Reg 10(c)')
            == lexicon.normalise_label('Reg 10(d)') == lexicon.normalise_label('Reg 10(f)'))
    # …the ref normaliser KEEPS them distinct (the fix), so each tie reads its OWN row
    refs = {S._norm_ref(r) for r in ('Reg 10(b)', 'Reg 10(c)', 'Reg 10(d)', 'Reg 10(f)', 'Reg 15(1)(c)', 'Reg 13(a)')}
    assert len(refs) == 6
    assert S._norm_ref('Reg 10(b)') == '10b' and S._norm_ref('15(1)(c)') == '151c'


def test_each_tie_reads_its_own_row_not_a_collided_one():
    # with the bug, min_corpus/max_investors would read the 10(d) GP row (25); the fix reads 1000 / 10
    checks = _by_id(S.reconcile_compliance(_prim(), _grid())[0])
    assert checks['sebi_tie_min_corpus']['reported'] == '1000'
    assert checks['sebi_tie_max_investors']['reported'] == '10'


# ══════════════════════════════════════════════════════════════════════════════════
# THE SIX TIES
# ══════════════════════════════════════════════════════════════════════════════════
def test_five_ties_pass_and_concentration_holds_without_investable_funds():
    checks = _by_id(S.reconcile_compliance(_prim(), _grid())[0])
    for tie in ('min_corpus', 'min_investor_commitment', 'max_investors', 'sponsor_interest', 'min_tenure'):
        assert checks[f'sebi_tie_{tie}']['status'] == reconcile.PASS, tie
    conc = checks['sebi_tie_concentration']
    assert conc['status'] == reconcile.INDETERMINATE and conc['verdict'] == 'HELD'


def test_concentration_activates_and_passes_when_investable_funds_supplied():
    checks = _by_id(S.reconcile_compliance(_prim(investable_funds_cr=D('948')), _grid())[0])
    conc = checks['sebi_tie_concentration']
    assert conc['status'] == reconcile.PASS
    assert conc['independent'].startswith('0.116')      # 110 / 948 = 11.6% — cost basis, ties reported


def test_planted_corpus_remark_mismatch_flags():
    # P4-adjacent: reported remark 900 ≠ independent Σcommitments 1000 → FLAG (tie mismatch)
    checks = _by_id(S.reconcile_compliance(_prim(), _grid({'10(b)': 'corpus Rs 900 Cr'}))[0])
    mc = checks['sebi_tie_min_corpus']
    assert mc['status'] == reconcile.FAIL and mc['verdict'] == 'FLAG'
    assert 'independent 1000 ≠ reported 900' in mc['detail']


def test_concentration_uses_cost_over_investable_never_fv_over_total_fv():
    # P3: the engine computes largest COST ÷ investable_funds. Feeding a cost of 110 with
    # investable 948 gives 11.6% — NOT the false 27.9% an FV-basis (231/827) would produce.
    checks = _by_id(S.reconcile_compliance(_prim(largest_cost_cr=D('110'), investable_funds_cr=D('948')), _grid())[0])
    ind = D(checks['sebi_tie_concentration']['independent'])
    assert abs(ind - D('0.116')) < D('0.001')
    assert abs(ind - D('0.279')) > D('0.1')             # provably NOT the FV-basis breach


def test_concentration_holds_when_only_fv_available_never_falls_back():
    # no cost figure AND no investable → HELD, never an FV fallback, never back-solved from 11.6%
    checks = _by_id(S.reconcile_compliance(_prim(largest_cost_cr=None, investable_funds_cr=None), _grid())[0])
    assert checks['sebi_tie_concentration']['verdict'] == 'HELD'


def test_investable_funds_never_backsolved_from_reported():
    # P4: even though reported says 11.6% and corpus is 1000, tie #5 does NOT auto-pass on corpus
    # or on a back-solved 948 — it stays HELD until investable_funds is supplied independently.
    checks = _by_id(S.reconcile_compliance(_prim(investable_funds_cr=None), _grid())[0])
    conc = checks['sebi_tie_concentration']
    assert conc['status'] == reconcile.INDETERMINATE
    assert conc['independent'] == ''                    # nothing computed — not corpus, not 948


# ══════════════════════════════════════════════════════════════════════════════════
# CATEGORY SCOPING (P2) + FAIL-CLOSED (P1/P5)
# ══════════════════════════════════════════════════════════════════════════════════
def test_cat_ii_rules_do_not_fire_for_a_cat_iii_fund():
    checks, disc = S.reconcile_compliance(_prim(category='III'), _grid(), category='III')
    assert not any(c['id'].startswith('sebi_tie_') for c in checks)   # no Cat II tie fires
    assert any('category' in d['detail'].lower() for d in disc)


def test_undetected_category_holds_everything():
    checks, disc = S.reconcile_compliance(_prim(category=None), [{'x': [[None]]}], category=None)
    assert checks == []
    assert any('category not resolved' in d['detail'] for d in disc)


def test_detect_category_reads_cat_ii_from_the_sheet():
    assert S.detect_category(_grid()) == 'II'


# ══════════════════════════════════════════════════════════════════════════════════
# CALENDAR COMPLETENESS
# ══════════════════════════════════════════════════════════════════════════════════
def _cal(activity, status):
    return Record('sebi_calendar', entity_id=activity, fields={'activity': activity, 'status': status})


def test_calendar_filed_passes_inprogress_is_open_not_failed():
    checks, _ = S.check_calendar([_cal('QAR Q4 FY26', 'Filed'), _cal('Annual financials', 'In progress')])
    by = [(c['filing'], c['filing_status'], c['verdict']) for c in checks]
    assert ('QAR Q4 FY26', 'filed', 'PASS') in by
    assert any(f == 'Annual financials' and v == 'OPEN' for f, s, v in by)
    assert not any(c['status'] == reconcile.FAIL for c in checks)   # in-progress never a FAIL


def test_calendar_flags_required_but_missing_filing_absent():
    checks, disc = S.check_calendar([_cal('QAR Q4 FY26', 'Filed')], required=('PPM audit',))
    assert any(c['id'] == 'sebi_filing_absent' and c['verdict'] == 'ABSENT' for c in checks)


def test_calendar_without_required_set_discloses_not_claims_complete():
    checks, disc = S.check_calendar([_cal('QAR Q4 FY26', 'Filed')])
    assert any('required-set' in d['detail'] for d in disc)         # fail-closed: not claimed complete


# ══════════════════════════════════════════════════════════════════════════════════
# PRIMITIVE ASSEMBLY FROM THE CIR
# ══════════════════════════════════════════════════════════════════════════════════
def _lp(name, typ, commitment):
    prov = Provenance(source_file='f', content_fingerprint='fp', sheet='LP', cell='A1')
    return Record('lp_register', entity_id=name,
                  fields={'type': typ, 'commitment': Figure('commitment', D(str(commitment)), None, prov)})


def _inv(name, cost):
    prov = Provenance(source_file='f', content_fingerprint='fp', sheet='I', cell='A1')
    return Record('portfolio_investments', entity_id=name,
                  fields={'cost': Figure('cost', D(str(cost)), None, prov)})


def test_build_primitives_from_cir_finds_sponsor_min_count_and_largest_cost():
    class _Reg:
        corpus_cr = D('1000')
    recs = [_lp('Aditya Pension Trust', 'Pension', 180), _lp('Sequoia Family Office', 'Family Office', 100),
            _lp('TFAI GP LLP (Sponsor)', 'GP Commitment', 25),
            _inv('LDC', 110), _inv('Hubbler', 60)]
    prim = S.build_primitives(lp_registers=[_Reg()], cir_records=recs, grids=_grid(), category='II')
    assert prim.corpus_cr == D('1000')
    assert min(prim.commitments_cr) == D('25') and prim.lp_count == 3
    assert prim.sponsor_commitment_cr == D('25')        # located the GP/sponsor row
    assert prim.largest_cost_cr == D('110')             # largest invested COST
    assert prim.tenure_years is None                    # no terms sheet in this grid → honestly None
