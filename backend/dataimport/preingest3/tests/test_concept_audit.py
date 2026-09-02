"""Concept-Identity audit replay + ledger — Phase 1.5 §5/§6 controls (concept_identity_build_spec v1.1 §7).

Control #7 (audit replay reddens on planted wrong beliefs, spares correct ones), #8 (a taxonomy bump
surfaces every verdict flip), #9 (the load-bearing cache-still-verifies invariant — a confirmed cache hit
NEVER skips the nets), plus the never-serve-a-contradicted-row ledger rule and the three-state honesty
(UNTESTED is not CORROBORATED — the anti-over-claim). All pure Python — no model, no I/O."""
from decimal import Decimal

from backend.dataimport.preingest3 import concept_audit as ca
from backend.dataimport.preingest3 import concept_identity as ci
from backend.dataimport.preingest3 import concept_nets


def _o(key, period, value, source='', fp=None, flows=()):
    return concept_nets.Observation(key, period=period, value=Decimal(str(value)), source=source,
                                    layout_fp=fp, flows=flows)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Control #7 — the audit replay QUARANTINES exactly the wrong beliefs, spares the correct ones.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_control7_replay_quarantines_wrong_beliefs_only():
    nav = ci.ConceptKey('nav')
    committed = ci.ConceptKey('capital', (('capital_status', 'committed'),))
    distributed = ci.ConceptKey('capital', (('capital_status', 'distributed'),))
    rev = ci.ConceptKey('revenue')

    w1 = ca.Belief('synonym', 'nav', detail='fund_nav ≡ terminal_nav (collide)')
    w2 = ca.Belief('synonym', 'capital', detail='distributed merged over committed')
    w3 = ca.Belief('synonym', 'nav', detail='cross-file NAV jump, no flows')
    c1 = ca.Belief('synonym', 'revenue', detail='true synonym, equal across periods')
    c2 = ca.Belief('synonym', 'nav', detail='roll-forward that reconciles')

    obs = {
        w1: [_o(nav, '2024', 100, 'A'), _o(nav, '2024', 250, 'B')],                    # Net1 collision
        w2: [_o(committed, '2024', 100), _o(distributed, '2024', 250)],                # Net2 ladder violation
        w3: [_o(nav, '2023', 100, 'A'), _o(nav, '2024', 250, 'B')],                    # Net2 unexplained jump
        c1: [_o(rev, '2023', 500, 'A', 'L'), _o(rev, '2023', 500, 'B', 'L'),
             _o(rev, '2024', 600, 'A', 'L'), _o(rev, '2024', 600, 'B', 'L')],          # promotes
        c2: [_o(nav, '2023', 100, 'A', 'L'), _o(nav, '2023', 100, 'B', 'L'),
             _o(nav, '2024', 150, 'A', 'L', flows=(('gain', Decimal('50')),)),
             _o(nav, '2024', 150, 'B', 'L')],                                          # roll-forward closes
    }
    report = ca.replay([w1, w2, w3, c1, c2], obs)
    assert {f.belief for f in report.quarantined()} == {w1, w2, w3}     # exactly the wrong ones
    assert report.summary() == {ca.CORROBORATED: 2, ca.UNTESTED: 0, ca.CONTRADICTED: 3}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Control #8 — a taxonomy/contract bump surfaces every state flip via the dual-run diff.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_control8_taxonomy_bump_surfaces_state_flips(monkeypatch):
    rev = ci.ConceptKey('revenue')
    b = ca.Belief('synonym', 'revenue', detail='revenue merge with an unexplained +100')
    obs = {b: [_o(rev, '2023', 500, 'A', 'L'), _o(rev, '2023', 500, 'B', 'L'),
               _o(rev, '2024', 600, 'A', 'L'), _o(rev, '2024', 600, 'B', 'L')]}        # +100, NO flows

    before = ca.replay([b], obs)                                        # revenue idclass 'none' → Net2 inactive
    assert before.findings[0].verdict == ca.CORROBORATED

    # the bump: declare revenue a roll_forward base (DATA edit) → Net2 now checks the movement
    monkeypatch.setattr(ci, 'CI_BASE_IDENTITY_CLASS',
                        dict(ci.CI_BASE_IDENTITY_CLASS, revenue='roll_forward'))
    after = ca.replay([b], obs)                                         # +100 unexplained → contradicted
    assert after.findings[0].verdict == ca.CONTRADICTED

    flips = ca.diff_reports(before, after)
    assert len(flips) == 1
    assert flips[0]['from'] == ca.CORROBORATED and flips[0]['to'] == ca.CONTRADICTED


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Control #9 — cache-still-verifies (the load-bearing invariant): a confirmed cache hit NEVER skips the
# nets. Red-before/green-after in one test: a cache-trusting shortcut ships a stale merge; verify_merge
# holds because it re-runs the nets on the mutated data.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_control9_cache_still_verifies_reddens():
    key = ci.ConceptKey('nav')
    ledger = ca.IdentityLedger()
    ledger.put(ca.IdentityLedgerRow(source_name='nav', concept_key=key, identity_class='roll_forward',
                                    asserter=ci.Asserter.DETERMINISTIC.value,
                                    confidence_state=ci.State.CONFIRMED.value))
    assert ledger.get(key) is not None and ledger.get(key).confidence_state == 'confirmed'   # a real hit

    obs = [_o(key, '2024', 100, 'A'), _o(key, '2024', 250, 'B')]        # data now COLLIDES under the row

    # RED: a cache-trusting shortcut would ship the stale 'merge OK'
    def _naive_trusts_cache():
        row = ledger.get(key)
        if row and row.confidence_state == 'confirmed':
            return True                                                 # the bug: skips the nets on a hit
        return concept_nets.run_nets(obs)[3]
    assert _naive_trusts_cache() is True                                # proves the hazard is real (RED)

    # GREEN: verify_merge runs the nets regardless of the confirmed hit → the collision is caught, held
    promoted, (n1, n2, n3) = ca.verify_merge(obs, ledger=ledger)
    assert n1.outcome == concept_nets.COLLIDE and promoted is False


# ── §6 rule: a CONTRADICTED row is never served as usable (caching a wrong belief is worse than none) ──
def test_ledger_never_serves_contradicted_row():
    key = ci.ConceptKey('nav')
    ledger = ca.IdentityLedger()
    ledger.put(ca.IdentityLedgerRow('nav', key, 'roll_forward', ci.Asserter.DETERMINISTIC.value,
                                    ci.State.CONTRADICTED.value))
    assert ledger.get(key) is None


# ── the three-state HONESTY: an untestable belief is UNTESTED, never silently 'corroborated' ──
def test_untested_belief_is_not_corroborated():
    b = ca.Belief('synonym', 'revenue')
    single = [_o(ci.ConceptKey('revenue'), '2024', 500, 'A', 'L')]      # only ONE source
    assert ca.audit_belief(b, single).verdict == ca.UNTESTED           # NOT corroborated (nothing to check)


# ── classify_run reads the PRODUCTION run's own output into the three-state report (no reconstruction) ──
def test_classify_run_three_state_from_production_output():
    class _Fig:
        def __init__(self, v): self.value_cr = v
    class _Rec:
        def __init__(self, fields): self.fields = fields
    class _Cir:
        checks = [{'id': 'term_agrees_hurdle_rate', 'status': 'pass', 'detail': 'agrees'},
                  {'id': 'term_agrees_carried_interest', 'status': 'fail', 'detail': 'disagree'}]
        records = [_Rec({'company': 'X', 'management_fee': _Fig(Decimal('0.02')),
                         'hurdle_rate': _Fig(Decimal('0.08'))})]
    rep = ca.classify_run(_Cir())
    s = rep.summary()
    assert s[ca.CORROBORATED] == 1 and s[ca.CONTRADICTED] == 1          # hurdle agrees / carry disagrees
    assert s[ca.UNTESTED] >= 1                                          # management_fee has no agreement check


# ── belief enumerators surface the real existing beliefs (legacy synonyms + convention defaults) ──
def test_belief_enumerators_nonempty_and_typed():
    syn = ca.synonym_beliefs()
    conv = ca.convention_beliefs()
    assert syn and all(b.kind == 'synonym' for b in syn)
    assert conv and all(b.kind == 'convention' for b in conv)          # the 'company IRR is gross' seed at least
    assert any(b.concept == 'irr' for b in conv)
