"""Correctness gate for the ONE preingest3 formula module (formulas.py).

TWO TIERS, and the dependency direction matters (the user's concern: preingest3
must never become coupled to preingest2):

  TIER 1 — AUTHORITATIVE, SELF-CONTAINED. Pins the formula.html-exact values on the
           real fixture primitives. Depends on NOTHING outside preingest3.formulas.
           This is the gate that governs; it reddens if any formula drifts.

  TIER 2 — CORROBORATION, SKIPPABLE. Cross-checks against the validated legacy
           preingest2/formulas.py to catch drift, but is guarded by importorskip:
           if preingest2 is ever removed, this tier skips and TIER 1 still stands.
           Where preingest2 and formula.html DIVERGE, formula.html governs and the
           divergence is asserted to be fully explained by ONE documented choice
           (preingest2 uses NET exit proceeds; formula.html §2.1 + the fixture's
           stated carry 61.6 use GROSS) — never an unexplained drift.

Run:
  pytest backend/dataimport/preingest3/tests/test_formula_parity.py -p no:cacheprovider -o addopts="" -m ""
"""
from decimal import Decimal as D

import pytest

from backend.dataimport.preingest3 import formulas as F

# ── the real fixture's primitives (cited cells in comments) ──────────────────
CALLED = D('600')          # Drawdowns!D11
DIST = D('70')             # Distributions!F9  (total_net_amount)
REALISED_GROSS = D('81')   # Exits!H9   (Σ gross proceeds)  — formula.html §2.1 basis
REALISED_NET = D('77')     # Exits!I9   (Σ net proceeds)
REALISED_GAIN = D('45')    # accounts!F14 (Σ realized_gain_loss = 81 − 36 cost realised)
FV = D('827')              # Valuation!E17 (Σ fair_value_of_holding)
INVESTED = D('448')        # Deployment!F18
CASH = D('22')             # accounts!F6
RECV = D('6')              # accounts!F7
MGMT_PAY = D('4')          # accounts!F8
CARRY_PAY = D('61.6')      # accounts!C20 note (and computes: 0.2×((81+827)−600))
OTHER_LIAB = D('8')        # accounts!F9
CARRY_RATE = D('0.2')      # Fees!C14
PNL_ITD = D('337.85')      # accounts F12:F19 net


def _q(v, p='0.0001'):
    return None if v is None else v.quantize(D(p))


# ══════════════════════════ TIER 1 — AUTHORITATIVE ══════════════════════════

def test_carry_computes_to_stated_61_6():
    """The fixture STATES carry ≈61.6; formula.html §3.2 must reproduce it from
    gross proceeds — this is the corroboration that pins the gross-proceeds basis."""
    base = F.carry_base(REALISED_GROSS, FV, CALLED)
    assert base == D('308')                                   # (81+827)−600
    assert F.accrued_carry(CARRY_RATE, base) == D('61.6')     # 0.2×308 → matches C20 note


def test_residual_nav_765_4():
    assert F.residual_nav(FV, D('61.6')) == D('765.4')        # §2.5  827−61.6


def test_carry_base_collision_net_residual_breaks_the_tie():
    """COLLISION 1 reddening control: 'Residual NAV' is net-of-carry in §2.5 but
    the §3.1 carry base needs GROSS ΣFV. If anyone swaps the net figure (765.4)
    into the carry base, carry drops to 49.28 and no longer matches the stated 61.6
    — the tie to the stated note is the guard that fires."""
    gross_base_carry = F.accrued_carry(CARRY_RATE, F.carry_base(REALISED_GROSS, FV, CALLED))
    assert gross_base_carry == D('61.6')                       # gross ΣFV 827 → matches stated
    net_base_carry = F.accrued_carry(CARRY_RATE, F.carry_base(REALISED_GROSS, D('765.4'), CALLED))
    assert net_base_carry.quantize(D('0.01')) == D('49.28')    # net residual → BREAKS the tie
    assert net_base_carry != D('61.6')


def test_moic_fv_basis_pinned_to_holding():
    """COLLISION 2 pinned choice: Portfolio MOIC uses HOLDING-basis FV (§2.1), not
    §8.3's equity basis. Documented on the module so a reader can't re-open it."""
    assert F.MOIC_FV_BASIS == F.FV_HOLDING


def test_require_basis_enforces_gross_net_tag_on_multiples():
    """The universal net: a multiple/return figure with no value_basis is a bug."""
    with pytest.raises(ValueError):
        F.require_basis('moic', None)
    with pytest.raises(ValueError):
        F.require_basis('tvpi', '')
    F.require_basis('moic', F.BASIS_GROSS_INVESTED)            # tagged → OK
    F.require_basis('tvpi', F.BASIS_NET_CALLED)                # tagged → OK
    F.require_basis('cost', None)                              # non-multiple → not required


def test_portfolio_moic_gross_2_027():
    # §2.1 (Σ proceeds GROSS + Σ FV) / Σ invested = (81+827)/448
    assert _q(F.portfolio_moic(REALISED_GROSS, FV, INVESTED)) == _q(D('908') / D('448'))
    assert _q(F.portfolio_moic(REALISED_GROSS, FV, INVESTED)) == D('2.0268')


def test_tvpi_net_moic_1_392():
    # §2.2 (dist + Residual NAV 765.4) / called
    assert _q(F.tvpi(DIST, D('765.4'), CALLED)) == D('1.3923')


def test_rvpi_and_dpi():
    assert _q(F.rvpi(D('765.4'), CALLED)) == D('1.2757')      # §2.4
    assert _q(F.dpi(DIST, CALLED)) == D('0.1167')             # §2.3


def test_ilpa_identity_tvpi_equals_dpi_plus_rvpi():
    # §2.2 note — the identity holds by construction on the SAME residual basis
    t = F.tvpi(DIST, D('765.4'), CALLED)
    d = F.dpi(DIST, CALLED)
    r = F.rvpi(D('765.4'), CALLED)
    assert _q(t) == _q(d + r)


def test_fund_nav_51_is_826_4_seven_components():
    nav = F.fund_nav_51(REALISED_GAIN, FV, CASH, RECV, MGMT_PAY, CARRY_PAY, OTHER_LIAB)
    assert nav == D('826.4')                                  # §5.1 seven-term


def test_fund_nav_51_reddens_if_a_component_is_dropped():
    """Reddening control for the exact bug that shipped (781.4 dropped Realised
    Gains): omit the realised-gains term and the NAV must move by exactly 45."""
    six_term = (FV + CASH + RECV - MGMT_PAY - CARRY_PAY - OTHER_LIAB)   # no realised gains
    assert six_term == D('781.4')
    assert F.fund_nav_51(REALISED_GAIN, FV, CASH, RECV, MGMT_PAY, CARRY_PAY, OTHER_LIAB) - six_term == REALISED_GAIN


def test_nav_51_component_list_has_exactly_seven_terms():
    comps = F.fund_nav_51_components(REALISED_GAIN, FV, CASH, RECV, MGMT_PAY, CARRY_PAY, OTHER_LIAB)
    assert len(comps) == 7
    assert sum(v for _, v in comps) == D('826.4')


def test_nav_emits_per_document_with_logged_rollforward_discrepancy():
    """§5.1 + §6.3, as written: 826.4 passes the ONLY gate (≤5× invested) and is
    EMITTED; the roll-forward 806.25 is a Stage-4 LOGGED cross-check (advisory
    divergence), NOT a hold. Document-faithful — no extra two-method HOLD."""
    rf = F.rollforward_nav(CALLED, DIST, PNL_ITD, CARRY_PAY)
    assert rf == D('806.25')                                  # model-free identity
    assert F.nav_sanity_ok(D('826.4'), INVESTED) is True      # §6.3 ≤5×448 → EMITS
    cc = F.nav_rollforward_crosscheck(D('826.4'), rf)
    assert _q(cc.gap) == D('20.1500')                         # discrepancy captured for the log
    assert _q(cc.gap_pct, '0.01') == D('2.44')
    assert cc.diverges is True                                # ADVISORY only — drives the note
    assert 'review before relying' in cc.note                 # actionable reason logged


def test_nav_sanity_rejects_above_5x_invested():
    """§6.3 reddening control: the one real NAV gate — a NAV > 5× invested is rejected
    (extraction error), while a normal NAV passes."""
    assert F.nav_sanity_ok(D('826.4'), INVESTED) is True      # 1.85× → OK
    assert F.nav_sanity_ok(D('2500'), INVESTED) is False      # 5.58× → rejected
    assert F.nav_sanity_ok(D('826.4'), None) is False         # no invested → cannot gate


def test_nav_crosscheck_is_advisory_not_a_gate():
    """The cross-check NEVER holds: a sub-1% gap is 'within band', a missing method is
    'unavailable' — neither blanks the emitted NAV (that's what §5.1/§6.3 decide)."""
    assert F.nav_rollforward_crosscheck(D('826.4'), D('822.0')).diverges is False   # 0.53%
    assert F.nav_rollforward_crosscheck(D('826.4'), None).diverges is False         # missing → not a hold


# ── xirr reddening controls (the single pipeline solver) ──────────────────────
def test_xirr_known_answer():
    from datetime import date
    o = lambda y, m, d: date(y, m, d).toordinal()
    # −100 at t0, +150 exactly one year later → ~50%
    r = F.xirr([(o(2024, 1, 1), D('-100')), (o(2025, 1, 1), D('150'))])
    assert r is not None and abs(r - D('0.5')) < D('0.01')


def test_xirr_no_sign_change_returns_none():
    from datetime import date
    o = lambda y, m, d: date(y, m, d).toordinal()
    assert F.xirr([(o(2024, 1, 1), D('-100')), (o(2025, 1, 1), D('-50'))]) is None


def test_xirr_out_of_sanity_returns_none():
    from datetime import date
    o = lambda y, m, d: date(y, m, d).toordinal()
    # +1 in, +10000 next day → astronomically high rate, must be rejected by sanity band
    assert F.xirr([(o(2024, 1, 1), D('-1')), (o(2024, 1, 2), D('10000'))]) is None


# ══════════════════════════ TIER 2 — CORROBORATION (skippable) ══════════════

def _preingest2_metrics():
    p2 = pytest.importorskip('backend.dataimport.preingest2.formulas',
                             reason='preingest2 removed — TIER 1 remains authoritative')
    dom = {
        'capital_calls': [
            {'call_date': '2021-09-30', 'total_call_amount': 150},
            {'call_date': '2022-06-30', 'total_call_amount': 140},
            {'call_date': '2023-06-30', 'total_call_amount': 120},
            {'call_date': '2024-06-30', 'total_call_amount': 100},
            {'call_date': '2025-09-30', 'total_call_amount': 90},
        ],
        'portfolio_investments': [{'company_name': f'C{i}', 'total_invested': v}
                                  for i, v in enumerate([110, 65, 16, 22, 18, 12, 80, 45, 55, 25])],
        'valuations_kpis': [{'fair_value_of_holding': v}
                            for v in [231, 150, 29, 40, 30, 15, 148, 66, 88, 30]],
        'exits_distributions': [
            {'distribution_number': 'D1', 'distribution_date': '2025-07-15', 'total_net_amount': 40},
            {'distribution_number': 'D2', 'distribution_date': '2025-10-20', 'total_net_amount': 18},
            {'distribution_number': 'D3', 'distribution_date': '2026-01-20', 'total_net_amount': 12},
            {'exit_date': '2025-06-30', 'proceeds': 45, 'net_exit_proceeds': 43, 'realized_gain_loss': 25},
            {'exit_date': '2025-09-30', 'proceeds': 22, 'net_exit_proceeds': 21, 'realized_gain_loss': 12},
            {'exit_date': '2025-12-15', 'proceeds': 14, 'net_exit_proceeds': 13, 'realized_gain_loss': 8},
        ],
        'fund_scheme_master': [
            {'line_item': 'Carried interest', 'value': 20},
            {'line_item': 'Cash & cash equivalents', 'value': 22},
            {'line_item': 'Receivables', 'value': 6},
            {'line_item': 'Management fee payable', 'value': 4},
            {'line_item': 'Other liabilities', 'value': 8},
        ],
    }
    return p2.FundMetrics(dom)


def test_parity_proceeds_independent_metrics_match_exactly():
    """Where no realised-proceeds basis choice is involved, the new module and
    preingest2 must agree to the rupee — genuine regression safety."""
    m = _preingest2_metrics()
    assert D(str(m.dpi())).quantize(D('0.0001')) == _q(F.dpi(DIST, CALLED))          # 0.1167
    assert D(str(m.total_invested())) == INVESTED                                    # 448
    assert D(str(m.residual_fv())) == FV                                             # 827
    assert D(str(m.total_called())) == CALLED                                        # 600


def test_parity_divergence_is_fully_explained_by_gross_vs_net_proceeds():
    """preingest2 uses NET exit proceeds (77); formula.html §2.1 + the fixture's
    stated carry (61.6) use GROSS (81). EVERY divergence must trace to exactly this
    one choice — proving it is a documented deviation, not unexplained drift.
    formula.html governs; the new module is authoritative.

    Comparisons use a float-noise tolerance because preingest2 is float-based
    (e.g. it returns carry as 60.800000000000004) — which is itself a reason it can
    only CORROBORATE and never be the authoritative oracle: preingest3 is Decimal-exact."""
    FLOAT_NOISE = D('0.0001')
    m = _preingest2_metrics()

    # preingest2 carry uses net proceeds → 0.2×((77+827)−600) = 60.8, vs ours 61.6
    p2_carry = D(str(m.gp_carry_gross()))
    assert abs(p2_carry - D('60.8')) < FLOAT_NOISE
    ours_carry = F.accrued_carry(CARRY_RATE, F.carry_base(REALISED_GROSS, FV, CALLED))
    expected_carry_gap = CARRY_RATE * (REALISED_GROSS - REALISED_NET)                # 0.2×4 = 0.8
    assert abs((ours_carry - p2_carry) - expected_carry_gap) < FLOAT_NOISE

    # residual NAV diverges by exactly that carry delta (0.8, ours LOWER)
    p2_resid = D(str(m.residual_nav()))
    assert abs((F.residual_nav(FV, ours_carry) - p2_resid) - (-expected_carry_gap)) < FLOAT_NOISE

    # portfolio MOIC diverges by exactly (gross−net proceeds)/invested
    p2_moic = D(str(m.moic()))
    ours_moic = F.portfolio_moic(REALISED_GROSS, FV, INVESTED)
    assert abs((ours_moic - p2_moic) - (REALISED_GROSS - REALISED_NET) / INVESTED) < FLOAT_NOISE
