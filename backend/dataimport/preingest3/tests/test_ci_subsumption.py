"""Concept-Identity Increment 3 — SUBSUMPTION controls (route-through adapters, behavior-preserving).

Each subsumed site keeps its public entry point but sources its DETECTION from the one mechanism/taxonomy,
so two detectors of the same thing can never drift apart (spec §0.1 centralization). Two controls per site:
  • behavior-preserving — the subsumed result is BYTE-IDENTICAL to the pre-subsumption behaviour (reddens
    on any drift in the derivation).
  • sole-detection-path — the site has NO independent detector left; it genuinely reads the shared source,
    proven by editing the source (DATA) and watching the behaviour move (single brain, cannot hide a shadow
    detector). This closes the only knock on route-through ("slightly less pure").

INC3a — negation/inclusion: lexicon._NEGATORS single-sourced from contract.CI_QUALIFIER_DIMENSIONS['inclusion'].
All pure Python (no model, no I/O)."""
import inspect
from decimal import Decimal

import pytest

from backend.dataimport.preingest3 import lexicon
from backend.dataimport.preingest3 import contract
from backend.dataimport.preingest3 import concept_nets
from backend.dataimport.preingest3 import concept_identity as ci
from backend.dataimport.preingest3 import fund_terms as ft
from backend.dataimport.preingest3 import fund_anchor as fa
from backend.dataimport.preingest3.cir import Record, Provenance


# ── behavior-preserving: the derived negator set is EXACTLY the historical hand-built set ──
def test_inc3a_negators_behavior_preserving():
    assert set(lexicon._NEGATORS) == {'non', 'excl', 'excluding', 'ex', 'less', 'before'}


# ── sole-detection-path: _NEGATORS IS the derivation output — no independent literal left ──
def test_inc3a_negators_are_derived_not_literal():
    assert lexicon._NEGATORS == lexicon._derive_negators()
    # only the negating inclusion VALUES feed it ('including' is additive, never a negator)
    including = contract.CI_QUALIFIER_DIMENSIONS['inclusion']['including']
    assert not (set(including) & set(lexicon._NEGATORS))


# ── single-source proof: a negator added to the TAXONOMY DATA flows to lexicon negation, no code change ──
def test_inc3a_taxonomy_edit_moves_negation(monkeypatch):
    inc = {k: v for k, v in contract.CI_QUALIFIER_DIMENSIONS['inclusion'].items()}
    inc['excluding'] = inc['excluding'] + ('sans',)          # a brand-new negator, DATA only
    extended = dict(contract.CI_QUALIFIER_DIMENSIONS, inclusion=inc)
    monkeypatch.setattr(lexicon, 'CI_QUALIFIER_DIMENSIONS', extended)
    assert 'sans' in lexicon._derive_negators()              # derivation reads the taxonomy live
    # and the negation RULE honours it end-to-end (a concept negated by 'sans' no longer matches)
    monkeypatch.setattr(lexicon, '_NEGATORS', lexicon._derive_negators())
    assert lexicon.label_matches_any('operating income', ['operating income']) is True
    assert lexicon.label_matches_any('sans operating income', ['operating income']) is False


# ── the negation RULE itself is intact on the derived vocabulary (each historical negator still bites) ──
def test_inc3a_negation_rule_intact_on_derived_vocab():
    assert lexicon.label_matches_any('operating income', ['operating income']) is True
    for neg in ('non', 'excl', 'excluding', 'ex', 'less', 'before'):
        assert lexicon.label_matches_any(f'{neg} operating income', ['operating income']) is False, neg
    # a synonym that legitimately BEGINS with a negator still matches (negator inside the synonym)
    assert lexicon.label_matches_any('profit before tax', ['profit before tax']) is True


# ══════════════════════════════════════════════════════════════════════════════════════════════
# INC3b — fund_terms Net 1: reconcile_fund_terms routes agreement DETECTION through net1_collision,
# with _RATE_TOL passed as data; corroborate/hold resolution unchanged.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _pv(cell):
    return Provenance(source_file='s', content_fingerprint='', sheet='S', cell=cell)


def _term(concept, value, cell='C1'):
    return ft.FundTerm(concept, value, 'percent', 'n/a', 'n/a', None, _pv(cell), verdict='confirmed')


def _reconcile_two(rate_a, rate_b):
    a = Record('fund_terms', 'fund', {'fund': 'fund', 'hurdle_rate': _term('hurdle_rate', rate_a, 'C20')})
    b = Record('fund_terms', 'fund', {'fund': 'fund', 'hurdle_rate': _term('hurdle_rate', rate_b, 'C15')})
    canonical, checks = ft.reconcile_fund_terms([a, b])
    status = {c['id']: c['status'] for c in checks}['term_agrees_hurdle_rate']
    return status, canonical.fields['hurdle_rate']


# ── sole-detection-path: the hand-rolled rate comparison is GONE; detection goes through the net ──
def test_inc3b_fund_terms_routes_through_net1_no_shadow_detector():
    src = inspect.getsource(ft.reconcile_fund_terms)
    assert 'net1_collision' in src
    assert 'abs(pt.value - ot.value)' not in src and 'abs(pt.value-ot.value)' not in src


# ── #10 TOLERANCE BOUNDARY (exact reproduction, read from data): the pass/hold flip is AT _RATE_TOL ──
def test_inc3b_fund_terms_tolerance_boundary_is_rate_tol():
    base, tol = Decimal('0.08'), ft._RATE_TOL
    # exactly at tolerance → still AGREES → corroborated, verdict stays confirmed
    st_on, term_on = _reconcile_two(base, base + tol)
    assert st_on == 'pass' and term_on.verdict == 'confirmed'
    assert term_on.qualifiers.get('corroborated_by') == 'S!C15'
    # one ulp beyond tolerance → COLLIDES → both HELD, fail (the boundary is _RATE_TOL, not a default)
    st_off, term_off = _reconcile_two(base, base + tol + Decimal('0.0000001'))
    assert st_off == 'fail' and term_off.verdict == 'held'


# ── the net verdict fund_terms consumes carries the DECLARED resolution policy (corroborate on agree) ──
def test_inc3b_fund_terms_declared_resolution_matches_policy():
    key = ci.ConceptKey('hurdle_rate')
    v = concept_nets.net1_collision(
        [concept_nets.Observation(key, value=Decimal('0.08')),
         concept_nets.Observation(key, value=Decimal('0.08'))], tol_abs=ft._RATE_TOL)
    assert v.outcome == concept_nets.AGREE and v.resolution == 'corroborate'


# ══════════════════════════════════════════════════════════════════════════════════════════════
# INC3c — fund_anchor cost/FV: the max DECISION is single-sourced from the declared policy (cost/FV is
# an aggregation, not a detector — so this single-sources the POLICY, fail-closed if it is absent).
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_inc3c_fund_anchor_max_behavior_preserving():
    assert fa._resolve_numeric_collision('cost', [Decimal('80'), Decimal('60')]) == Decimal('80')
    assert fa._resolve_numeric_collision('fair_value', [Decimal('100'), Decimal('90')]) == Decimal('100')
    assert fa._resolve_numeric_collision('cost', []) is None


# ── the max DECISION is the DECLARED resolution the net names on collide — force it non-max → fail-closed
#    (INC5: the policy is now looked up inside net1; the guard sits on the collide path via verdict.resolution) ──
def test_inc3c_max_decision_is_policy_sourced_fail_closed(monkeypatch):
    monkeypatch.setattr(concept_nets, 'resolution_policy_of',
                        lambda base, ev: 'hold_both' if ev == 'net1_collision' else None)
    with pytest.raises(ValueError):
        fa._resolve_numeric_collision('cost', [Decimal('22'), Decimal('6')])   # a real collide, non-max policy


# ── sole-source: no hardcoded max() left in the merge; it routes through the policy helper ──
def test_inc3c_no_hardcoded_max_in_anchor_merge():
    src = inspect.getsource(fa.build_fund_anchors)
    assert "max(a['cost'])" not in src and "max(a['fair_value'])" not in src
    assert "_resolve_numeric_collision" in src


# ══════════════════════════════════════════════════════════════════════════════════════════════
# INC3 CLOSE — fail-closed through decompose (§3-requirement 2) + the new general test (#10).
# ══════════════════════════════════════════════════════════════════════════════════════════════

# ── FAIL-CLOSED THROUGH decompose (reddening): an ambiguous real label the parser cannot name → base
#    None → HELD, never merged — even when two distinct unknowns coincide across periods in one layout ──
def test_inc3_fail_closed_unresolved_label_never_merges():
    amb = ci.decompose('Sundry Balances Written Back')     # a real-shaped label with no known base
    assert amb.base is None                                # unresolved, not a confident-but-wrong tuple
    # two DISTINCT unknowns, coincidentally equal across 2 periods, same layout class — a merge would be
    # a wrong number. The base-None guard MUST hold (this reddens: pre-guard promote() returned True).
    obs = [concept_nets.Observation(amb, '2023', Decimal('5'), 'A', 'L'),
           concept_nets.Observation(amb, '2023', Decimal('5'), 'B', 'L'),
           concept_nets.Observation(amb, '2024', Decimal('7'), 'A', 'L'),
           concept_nets.Observation(amb, '2024', Decimal('7'), 'B', 'L')]
    n1, n2, n3, promoted = concept_nets.run_nets(obs)
    assert n1.outcome == concept_nets.AGREE and len(n1.overlap_periods) == 2   # they DO coincide…
    assert promoted is False                                                   # …but stay HELD (fail-closed)


# ── control #10 (new general test): the SAME unified Net 1 fund_terms uses also fires on a base that
#    NEVER had a proto-net (revenue) — generalization, not a fund_terms-specific instance ──
def test_inc3_unified_net1_generalizes_to_base_without_proto_net():
    key = ci.ConceptKey('revenue')                         # a free-flow base with no proto-net of its own
    agree = concept_nets.net1_collision([concept_nets.Observation(key, '2024', Decimal('500')),
                                         concept_nets.Observation(key, '2024', Decimal('500'))])
    collide = concept_nets.net1_collision([concept_nets.Observation(key, '2024', Decimal('500')),
                                           concept_nets.Observation(key, '2024', Decimal('600'))])
    assert agree.outcome == concept_nets.AGREE
    assert collide.outcome == concept_nets.COLLIDE and collide.resolution == 'hold_both'


# ══════════════════════════════════════════════════════════════════════════════════════════════
# INC5 — fund_anchor merge-nets LIVE: cost/FV + ownership DETECTION routed through net1 (byte-identical,
# exercised — the corpus has 3 real cost collisions). Domicile is categorical (net1 numeric) → deferred.
# ══════════════════════════════════════════════════════════════════════════════════════════════

# ── sole-detection-path: both resolvers route detection through net1; the hand-rolled shadow is gone ──
def test_inc5_fund_anchor_routes_detection_through_net1():
    src_num = inspect.getsource(fa._resolve_numeric_collision)
    src_own = inspect.getsource(fa._resolve_ownership)
    assert 'net1_collision' in src_num and 'net1_collision' in src_own
    assert 'len(vals) == 1' not in src_own                 # the len>1 shadow detector is removed


# ── the cost net genuinely FIRES on a real cross-sheet collision (the corpus's 22-vs-6 shape) ──
def test_inc5_cost_collision_fires_net1_max_resolves():
    key = ci.ConceptKey('cost')
    v = concept_nets.net1_collision([concept_nets.Observation(key, value=Decimal('22')),
                                     concept_nets.Observation(key, value=Decimal('6'))], tol_abs=Decimal('0'))
    assert v.outcome == concept_nets.COLLIDE and v.resolution == 'max'     # detection live + declared policy
    assert fa._resolve_numeric_collision('cost', [Decimal('22'), Decimal('6')]) == Decimal('22')


# ── behavior-preserving: net1-routed resolvers == the prior logic on collide / agree / single / empty ──
def test_inc5_resolvers_byte_identical_to_prior_logic():
    assert fa._resolve_numeric_collision('cost', [Decimal('22'), Decimal('6')]) == Decimal('22')   # max on collide
    assert fa._resolve_numeric_collision('fair_value', [Decimal('10'), Decimal('10')]) == Decimal('10')  # agree
    assert fa._resolve_numeric_collision('cost', [Decimal('80')]) == Decimal('80')                 # single
    assert fa._resolve_numeric_collision('cost', []) is None                                       # empty
    assert fa._resolve_ownership([(0.26, True)]) == 0.26                                           # single
    assert fa._resolve_ownership([(0.30, False), (0.22, True)]) == 0.22                            # prefer cost sheet
    assert fa._resolve_ownership([(0.30, False), (0.22, False)]) == 0.22                           # det. min
    assert fa._resolve_ownership([]) is None


# ══════════════════════════════════════════════════════════════════════════════════════════════
# INC3a — decompose-at-resolution LIVE at fund_terms: the net's ConceptKey keeps the AUTHORITATIVE base
# (decompose's base lexicon does not cover fund-term rates → base None → re-deriving would misgroup) but
# is ENRICHED with decompose's qualifiers from each source's RAW label, so gross-basis and net-basis terms
# are no longer collapsed to one key. Byte-identical when no basis qualifier is present (the WHOLE corpus —
# proven: master workbook SHA unchanged before/after) — the danger is absent here, so its VALUE shows only
# on the planted fixtures below. fund_anchor stays base-authoritative (grounded no-op — see its docstring).
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _pv_lbl(cell, row_label):
    return Provenance(source_file='s', content_fingerprint='', sheet='S', cell=cell, row_label=row_label)


def _term_lbl(concept, value, row_label, cell='C1'):
    return ft.FundTerm(concept, value, 'percent', 'n/a', 'n/a', None, _pv_lbl(cell, row_label),
                       verdict='confirmed')


# ── THE TEETH (reddening pair): decompose splits gross vs net → net1 CLEAN (different concepts); the SAME
#    two values under the pre-3a BARE key false-COLLIDE. With the qualifier the nets stop conflating them. ──
def test_inc3a_decompose_splits_gross_net_at_net1():
    kp = ci.ConceptKey('carried_interest', ci.decompose('Gross Carried Interest').qualifiers)
    ko = ci.ConceptKey('carried_interest', ci.decompose('Net Carried Interest').qualifiers)
    assert kp.qualifiers == (('basis', 'gross'),) and ko.qualifiers == (('basis', 'net'),)
    split = concept_nets.net1_collision([concept_nets.Observation(kp, value=Decimal('0.20')),
                                         concept_nets.Observation(ko, value=Decimal('0.15'))],
                                        tol_abs=ft._RATE_TOL)
    assert split.outcome == concept_nets.CLEAN                     # different concepts → NOT a collision
    # NEGATIVE CONTROL: the SAME two values under a BARE key (pre-3a behaviour) DO false-collide → held
    bare = ci.ConceptKey('carried_interest')
    coll = concept_nets.net1_collision([concept_nets.Observation(bare, value=Decimal('0.20')),
                                        concept_nets.Observation(bare, value=Decimal('0.15'))],
                                       tol_abs=ft._RATE_TOL)
    assert coll.outcome == concept_nets.COLLIDE                    # base-only key wrongly conflates them


# ── fund_terms INTEGRATION (the value): two sources gross-carry vs net-carry → recognised as DIFFERENT
#    concepts → NOT held as a cross-source disagreement, and the primary term stays confirmed (not merged) ──
def test_inc3a_fund_terms_gross_net_not_falsely_held():
    a = Record('fund_terms', 'fund', {'fund': 'fund',
               'carried_interest': _term_lbl('carried_interest', Decimal('0.20'), 'Gross Carried Interest', 'C1')})
    b = Record('fund_terms', 'fund', {'fund': 'fund',
               'carried_interest': _term_lbl('carried_interest', Decimal('0.15'), 'Net Carried Interest', 'C2')})
    canonical, checks = ft.reconcile_fund_terms([a, b])
    ids = {c['id']: c['status'] for c in checks}
    assert 'term_agrees_carried_interest' not in ids              # CLEAN → skip: no false disagreement check
    assert canonical.fields['carried_interest'].verdict == 'confirmed'   # primary NOT held


# ── CONTROL (the hold path still fires): no distinguishing qualifier (same basis) + disagreeing values →
#    same key → COLLIDE → both held. Proves 3a did not silence real cross-source disagreements. ──
def test_inc3a_fund_terms_same_concept_disagreement_still_holds():
    a = Record('fund_terms', 'fund', {'fund': 'fund',
               'carried_interest': _term_lbl('carried_interest', Decimal('0.20'), 'Carried Interest', 'C1')})
    b = Record('fund_terms', 'fund', {'fund': 'fund',
               'carried_interest': _term_lbl('carried_interest', Decimal('0.15'), 'Carried Interest', 'C2')})
    canonical, checks = ft.reconcile_fund_terms([a, b])
    ids = {c['id']: c['status'] for c in checks}
    assert ids.get('term_agrees_carried_interest') == 'fail'      # real disagreement → held
    assert canonical.fields['carried_interest'].verdict == 'held'


# ── byte-identical-key miniature: an unqualified label keys IDENTICALLY to the bare key, so the corpus
#    (no basis-qualified labels) is unaffected — the non-regression guarantee in the small. ──
def test_inc3a_empty_qualifier_is_byte_identical_key():
    assert ci.decompose('Carried Interest').qualifiers == ()
    enriched = ci.ConceptKey('carried_interest', ci.decompose('Carried Interest').qualifiers)
    assert enriched.canonical() == ci.ConceptKey('carried_interest').canonical()
