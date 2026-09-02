"""Concept-Identity data-nets — Phase 1.5 §3 reddening controls (concept_identity_build_spec v1.1 §7).

The three nets gate a MERGE (never an emit). Each planted fixture is RED before the net exists and GREEN
after — and green only when correct. Controls #1-5 and #11 (§7) land here as synthetic fixtures because
the marquee NAV/IRR/capital wrong-merges are corpus-sparse (§10 census) — the danger is established, so it
is proven by fixture, not by hoping the corpus contains it. Two extra locks: resolution-declared (Net 1
hold_both vs max on identical detection — the anti-flatten guard, §0.1) and the Net-2 universality twin of
CI-12 (a new identity-bearing base gets Net 2 by DATA, no code). All pure Python — no model, no I/O."""
from decimal import Decimal

from backend.dataimport.preingest3 import concept_identity as ci
from backend.dataimport.preingest3 import concept_nets as cn


def _o(key, period, value, source='', fp=None, flows=()):
    return cn.Observation(key, period=period, value=Decimal(str(value)), source=source,
                          layout_fp=fp, flows=flows)


# ── Control #1: known-wrong synonym planted, same file/period, different values → Net 1 HOLD + flag ──
def test_control1_known_wrong_synonym_same_period_collides():
    k = ci.ConceptKey('nav', ())          # fund_nav ≡ terminal_nav wrongly merged under one bare key
    obs = [_o(k, '2024', 100, 'A!B2'), _o(k, '2024', 250, 'A!C2')]
    v = cn.net1_collision(obs)
    assert v.outcome == cn.COLLIDE
    assert v.resolution == 'hold_both'
    assert not cn.promote(v, cn.net2_movement(obs), cn.net3_context(obs))


# ── Control #2: cross-file wrong merge, NO same-period overlap → Net 2 flags the unreconciled jump ──
def test_control2_cross_file_unreconciled_jump():
    k = ci.ConceptKey('nav', ())
    obs = [_o(k, '2023', 100, 'A'), _o(k, '2024', 250, 'B')]   # +150 with no explaining flows
    n1, n2 = cn.net1_collision(obs), cn.net2_movement(obs)
    assert n1.outcome == cn.CLEAN                              # no same-period overlap for Net 1
    assert n2.outcome == cn.UNRECONCILED and n2.resolution == 'hold'
    assert not cn.promote(n1, n2, cn.net3_context(obs))


def test_net2_roll_forward_reconciles_with_flows():          # no false hold when the roll-forward closes
    k = ci.ConceptKey('nav', ())
    obs = [_o(k, '2023', 100), _o(k, '2024', 150, flows=(('gain', Decimal('50')),))]
    assert cn.net2_movement(obs).outcome == cn.RECONCILED


# ── Control #3: true synonym, equal across ALL periods, one layout class → MUST promote (no false hold) ─
def test_control3_true_synonym_promotes():
    k = ci.ConceptKey('revenue', ())      # revenue = free flow → Net 2 declared-inactive; rests on 1+3
    obs = [_o(k, '2023', 500, 'A', 'L'), _o(k, '2023', 500, 'B', 'L'),
           _o(k, '2024', 600, 'A', 'L'), _o(k, '2024', 600, 'B', 'L')]
    n1, n2, n3, ok = cn.run_nets(obs)
    assert n1.outcome == cn.AGREE and len(n1.overlap_periods) == 2
    assert n2.outcome == cn.INACTIVE      # declared-inactive, NOT silently absent
    assert n3.outcome == cn.HOMOGENEOUS
    assert ok is True


# ── Control #4: coincidental single-period equality on two different concepts → MUST NOT promote ──
def test_control4_single_period_coincidence_does_not_promote():
    k = ci.ConceptKey('revenue', ())
    hetero = [_o(k, '2024', 500, 'A', 'L1'), _o(k, '2024', 500, 'B', 'L2')]   # different contexts
    assert cn.net3_context(hetero).outcome == cn.HETEROGENEOUS
    assert cn.run_nets(hetero)[3] is False
    # even same context: one coincidental period + free-flow (no law closure) is not enough
    same = [_o(k, '2024', 500, 'A', 'L'), _o(k, '2024', 500, 'B', 'L')]
    assert cn.run_nets(same)[3] is False


# ── Control #5: silent qualifier, no convention → UNRESOLVED (doubt), never auto-same (match rule) ──
def test_control5_silent_qualifier_unresolved():
    assert ci.same_concept(ci.decompose('IRR'), ci.decompose('Gross IRR')) == ci.DOUBT


# ── Control #11: a convention-defaulted qualifier the DATA contradicts → nets catch it, not emitted ──
def test_control11_convention_default_is_net_verified():
    bare, gross = ci.decompose('IRR'), ci.decompose('Gross IRR')
    # company-context convention promotes bare IRR → gross, so both assert the SAME key…
    assert ci.same_concept(bare, gross, ctx_a='company', ctx_b='company') == ci.SAME
    merged = ci.ConceptKey('irr', (('basis', 'gross'),))
    # …but if the underlying cells disagree, Net 1 collides — the convention assert is data-checked
    obs = [_o(merged, '2024', '0.20', 'A'), _o(merged, '2024', '0.35', 'B')]
    v = cn.net1_collision(obs, tol_abs=Decimal('0.0005'))
    assert v.outcome == cn.COLLIDE
    assert not cn.promote(v, cn.net2_movement(obs), cn.net3_context(obs))


# ── the LIVE capital ladder (monotone_ladder): a lower rung exceeding a higher rung is a wrong-merge ──
def test_net2_capital_ladder_violation():
    committed = ci.ConceptKey('capital', (('capital_status', 'committed'),))
    distributed = ci.ConceptKey('capital', (('capital_status', 'distributed'),))
    obs = [_o(committed, '2024', 100), _o(distributed, '2024', 250)]   # distributed > committed: impossible
    v = cn.net2_movement(obs)
    assert v.outcome == cn.UNRECONCILED and v.resolution == 'hold'


def test_net2_capital_ladder_ok():
    rungs = [('committed', 100), ('called', 60), ('distributed', 20)]
    obs = [_o(ci.ConceptKey('capital', (('capital_status', s),)), '2024', v) for s, v in rungs]
    assert cn.net2_movement(obs).outcome == cn.RECONCILED


# ── flow_balance law: opening + flows = closing within a period (same law engine as roll_forward) ──
def test_net2_flow_balance():
    opening = ci.ConceptKey('cash', (('lifecycle', 'opening'),))
    closing = ci.ConceptKey('cash', (('lifecycle', 'closing'),))
    ok = [_o(opening, '2024', 100),
          _o(closing, '2024', 120, flows=(('receipts', Decimal('50')), ('payments', Decimal('-30'))))]
    assert cn.net2_movement(ok).outcome == cn.RECONCILED
    bad = [_o(opening, '2024', 100), _o(closing, '2024', 200, flows=(('receipts', Decimal('50')),))]
    assert cn.net2_movement(bad).outcome == cn.UNRECONCILED


# ── RESOLUTION-DECLARED (§0.1 anti-flatten): identical detection, DIFFERENT declared disposition ──
def test_resolution_declared_hold_vs_max():
    def collide(base_key):
        return cn.net1_collision([_o(base_key, '2024', 10, 'A'), _o(base_key, '2024', 12, 'B')])
    assert collide(ci.ConceptKey('irr', (('basis', 'gross'),))).resolution == 'hold_both'  # fund_terms
    assert collide(ci.ConceptKey('cost', ())).resolution == 'max'                          # fund_anchor


# ── Net-2 UNIVERSALITY (CI-12 twin): a brand-new identity-bearing base gets Net 2 by DATA, no code ──
def test_net2_universality_new_base_is_data_only(monkeypatch):
    monkeypatch.setattr(ci, 'CI_BASE_IDENTITY_CLASS', dict(ci.CI_BASE_IDENTITY_CLASS, aum='roll_forward'))
    k = ci.ConceptKey('aum', ())
    obs = [_o(k, '2023', 100), _o(k, '2024', 300)]            # +200 unexplained
    v = cn.net2_movement(obs)
    assert v.outcome == cn.UNRECONCILED                       # the new base got the roll_forward law…
    assert v.resolution == 'hold'                             # …and the ('*','net2_movement') declared default


# ── promotion on HARD law closure is allowed even single-period (arithmetic identity ≠ coincidence) ──
def test_promote_on_law_closure_single_period():
    opening = ci.ConceptKey('cash', (('lifecycle', 'opening'),))
    closing = ci.ConceptKey('cash', (('lifecycle', 'closing'),))
    obs = [_o(closing, '2024', 120, 'A', 'L', flows=(('r', Decimal('50')), ('p', Decimal('-30')))),
           _o(closing, '2024', 120, 'B', 'L'),
           _o(opening, '2024', 100, 'A', 'L')]
    n1, n2, n3, ok = cn.run_nets(obs)
    assert n1.outcome == cn.AGREE                             # closing agrees across A,B at 2024
    assert n2.outcome == cn.RECONCILED and n3.outcome == cn.HOMOGENEOUS
    assert ok is True


# ── the Undrawn-Commitment rung-precedence fix (compound capital phrase): status modifier wins ──
def test_undrawn_commitment_rung_precedence():
    assert ci.decompose('Undrawn Commitment').q()['capital_status'] == 'undrawn'
    assert ci.decompose('Uncalled Commitment').q()['capital_status'] == 'undrawn'
    assert ci.decompose('Paid-in Capital').q().get('capital_status') == 'paid_in'
    assert ci.decompose('Commitment').q()['capital_status'] == 'committed'   # bare base unchanged
