"""Concept-Identity mechanism — Phase 1.5 foundation (concept_identity_build_spec v1.1, Layer A).

Reddening tests for the dimension-agnostic mechanism: decomposition, the three-state match rule, the
per-base law/ladder lookups, and CI-12 — the universality control that proves a new dimension is
addable as DATA with no code change. The three nets, subsumption, and audit replay are later increments;
these lock the foundation they build on. All are pure Python (no model, no I/O)."""
from backend.dataimport.preingest3 import concept_identity as ci
from backend.dataimport.preingest3 import contract


# ── decomposition: the marquee established-danger pairs resolve to base + distinguishing qualifier ──
def test_decompose_marquee_danger_pairs():
    assert ci.decompose('Gross IRR') == ci.ConceptKey('irr', (('basis', 'gross'),))
    assert ci.decompose('Net IRR') == ci.ConceptKey('irr', (('basis', 'net'),))
    assert ci.decompose('Terminal NAV') == ci.ConceptKey('nav', (('lifecycle', 'terminal'),))
    assert ci.decompose('Closing NAV') == ci.ConceptKey('nav', (('lifecycle', 'closing'),))


# ── the LIVE capital ladder: standalone words → base 'capital' + a monotone rung ──
def test_decompose_capital_ladder():
    assert ci.decompose('Commitment').base == 'capital'
    assert ci.decompose('Commitment').q()['capital_status'] == 'committed'
    assert ci.decompose('Distribution').q()['capital_status'] == 'distributed'
    assert ci.decompose('Drawdown').q()['capital_status'] == 'called'
    # the ladder is ORDERED so Net 2's monotone law can compare rungs
    assert ci.ladder_rank('committed') < ci.ladder_rank('called') < ci.ladder_rank('distributed')


# ── base-consume: a base's OWN tokens are never re-read as a qualifier ──
def test_base_consume_prevents_false_qualifier():
    # 'net' in 'net income' is part of the BASE, not a basis qualifier
    assert ci.decompose('Net Income') == ci.ConceptKey('net_income', ())
    # but 'net' standing alone beside a base IS the basis qualifier
    assert ci.decompose('Net Revenue').q().get('basis') == 'net'


# ── word-boundary: the census's substring bug ('net' inside 'cabinet') MUST NOT recur (reddening) ──
def test_word_boundary_no_substring_noise():
    # a substring matcher would set basis=net here; the word-boundary matcher must not
    assert 'basis' not in ci.decompose('Filing Cabinet').q()
    assert 'basis' not in ci.decompose('Internet Charges').q()
    assert 'basis' not in ci.decompose('Assets').q()          # 'ass'..'net'? no token 'net'


# ── the three-state match rule (§1.2): same / different / unresolved ──
def test_match_rule_three_state():
    g, n = ci.decompose('Gross IRR'), ci.decompose('Net IRR')
    assert ci.same_concept(g, n) == ci.DIFFERENT                       # explicit conflict → different
    assert ci.same_concept(ci.decompose('Terminal NAV'),
                           ci.decompose('Closing NAV')) == ci.DIFFERENT
    # bare IRR + company context → convention (company IRR is gross) → matches Gross IRR
    bare = ci.decompose('IRR')
    assert ci.same_concept(bare, g, ctx_a='company', ctx_b='company') == ci.SAME
    # bare IRR (convention=gross) vs explicit Net IRR → convention conflicts with explicit → different
    assert ci.same_concept(bare, n, ctx_a='company', ctx_b='company') == ci.DIFFERENT
    # bare IRR vs Gross IRR with NO context → no convention applies → UNRESOLVED (doubt), never silent-same
    assert ci.same_concept(bare, g) == ci.DOUBT


# ── per-base conservation law + resolution policy (read from taxonomy data) ──
def test_identity_class_and_resolution_policy():
    assert ci.identity_class_of('nav') == 'roll_forward'
    assert ci.identity_class_of('capital') == 'monotone_ladder'
    assert ci.identity_class_of('cash') == 'flow_balance'
    assert ci.identity_class_of('revenue') == 'none'                   # free flow → Net 2 declared-inactive
    # each proto-net's behaviour survives as a declared policy value (detection-universal/resolution-declared)
    assert ci.resolution_policy_of('cost', 'net1_collision') == 'max'  # fund_anchor MAX
    assert ci.resolution_policy_of('nav', 'net2_movement') == 'hold'   # nav roll-forward HOLD
    assert ci.resolution_policy_of('ebitda', 'net1_collision') == 'hold_both'  # '*' default


# ── CI-12 — THE UNIVERSALITY CONTROL: a new dimension is DATA, not code ──
def test_ci12_new_dimension_is_data_only_no_code_change(monkeypatch):
    """Introduce a brand-new qualifier dimension ('vintage') through the taxonomy DATA alone and prove it
    flows through detection (decompose) and the match rule with ZERO change to the mechanism code. If this
    can't pass, the mechanism isn't universal (spec §4A / control CI-12)."""
    extended = dict(ci.CI_QUALIFIER_DIMENSIONS)
    extended['vintage'] = {'v2021': ('vintage 2021',), 'v2022': ('vintage 2022',)}
    monkeypatch.setattr(ci, 'CI_QUALIFIER_DIMENSIONS', extended)       # a data edit, no code touched

    a = ci.decompose('NAV vintage 2021')
    b = ci.decompose('NAV vintage 2022')
    assert a.q().get('vintage') == 'v2021'                             # detection picked up the new dim
    assert b.q().get('vintage') == 'v2022'
    assert ci.same_concept(a, b) == ci.DIFFERENT                       # match rule differentiates on it


# ── the taxonomy is pinned by the contract signature (a data edit re-validates caches, U8) ──
def test_taxonomy_folded_into_contract_signature(monkeypatch):
    before = contract.contract_signature()
    extended = dict(contract.CI_BASE_IDENTITY_CLASS, newbase='flow_balance')
    monkeypatch.setattr(contract, 'CI_BASE_IDENTITY_CLASS', extended)
    assert contract.contract_signature() != before                    # taxonomy edit → signature changes
