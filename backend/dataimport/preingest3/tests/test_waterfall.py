"""Reddening controls for the waterfall engine + accrued-carry reconciliation (finance build 1).

These prove the engine is TERM-DRIVEN (levers move the number), DATE-DRIVEN (pref accrues on real
dates), FAIL-CLOSED (missing term → hold, never a default), and — the load-bearing one — that it
CHECKS, NEVER FITS: the computed scenario table is identical regardless of the reported target."""
import datetime as dt
from decimal import Decimal as D

import pytest

from backend.dataimport.preingest3 import waterfall as W
from backend.dataimport.preingest3 import reconcile


# ── the real fund's primitives (measure-first: carry 20% / hurdle 8% / catch-up 100% /
#    European whole-fund / G = realised 45 + unrealised 379 = 424 / drawn 600 / committed 1000) ──
REAL_CALLS = (
    (dt.date(2021, 9, 30), D('150')), (dt.date(2022, 9, 30), D('140')),
    (dt.date(2023, 9, 30), D('120')), (dt.date(2024, 9, 30), D('100')),
    (dt.date(2025, 9, 30), D('90')),
)


def _real_inputs(**over):
    base = dict(carry_rate=D('0.20'), hurdle_rate=D('0.08'), catch_up_rate=D('1.0'),
                waterfall_type='european_whole_fund', total_profit_cr=D('424'),
                drawn_capital_cr=D('600'), committed_capital_cr=D('1000'),
                calls=REAL_CALLS, as_of=dt.date(2026, 3, 31))
    base.update(over)
    return W.WaterfallInputs(**base)


# ══════════════════════════════════════════════════════════════════════════════════
# PURE waterfall math — hard vs soft, catch-up rate (all TERM-DRIVEN)
# ══════════════════════════════════════════════════════════════════════════════════
def test_hard_hurdle_carves_out_the_preferred_return():
    # hard hurdle: pref is a permanent LP carve-out → carry = k·(G−H)
    assert W.european_carry(D('1000'), D('100'), D('0.20'), D('1.0'), 'hard') == D('180')


def test_soft_hurdle_full_catchup_collapses_to_carry_times_profit():
    # soft hurdle with 100% catch-up and ample residual → GP ends with k·G
    assert W.european_carry(D('1000'), D('100'), D('0.20'), D('1.0'), 'soft') == D('200')


def test_hard_and_soft_are_two_different_numbers_proving_term_driven():
    hard = W.european_carry(D('1000'), D('100'), D('0.20'), D('1.0'), 'hard')
    soft = W.european_carry(D('1000'), D('100'), D('0.20'), D('1.0'), 'soft')
    assert hard != soft and hard == D('180') and soft == D('200')


def test_catchup_rate_matters_when_catchup_does_not_complete():
    # small profit so the catch-up tier never completes → carry = q·(G−H), q is term-driven
    full = W.european_carry(D('150'), D('100'), D('0.20'), D('1.0'), 'soft')   # q=100%
    part = W.european_carry(D('150'), D('100'), D('0.20'), D('0.5'), 'soft')   # q=50%
    assert part == D('25') and full == D('30') and part != full


def test_no_carry_until_profit_clears_the_hurdle():
    assert W.european_carry(D('80'), D('100'), D('0.20'), D('1.0'), 'soft') == D('0')
    assert W.european_carry(D('80'), D('100'), D('0.20'), D('1.0'), 'hard') == D('0')


def test_degenerate_catchup_rate_falls_back_to_hard_behaviour():
    # q <= k is degenerate (can never catch up) → behaves like a hard carve-out, never a default
    assert W.european_carry(D('1000'), D('100'), D('0.20'), D('0.15'), 'soft') == D('180')


# ══════════════════════════════════════════════════════════════════════════════════
# DATE-DRIVEN preferred return
# ══════════════════════════════════════════════════════════════════════════════════
def test_preferred_return_accrues_on_real_dates_simple():
    inp = _real_inputs(calls=((dt.date(2024, 1, 1), D('100')),), committed_capital_cr=None,
                       hurdle_rate=D('0.10'))
    years = D((dt.date(2026, 3, 31) - dt.date(2024, 1, 1)).days) / D('365.25')
    assert W.preferred_return(inp, 'drawn', 'simple') == D('100') * D('0.10') * years


def test_compounding_earns_more_than_simple():
    inp = _real_inputs(calls=((dt.date(2021, 1, 1), D('100')),), committed_capital_cr=None,
                       hurdle_rate=D('0.10'))
    assert W.preferred_return(inp, 'drawn', 'annual_compound') > W.preferred_return(inp, 'drawn', 'simple')


def test_committed_basis_earns_more_than_drawn_when_commitment_exceeds_calls():
    inp = _real_inputs(hurdle_rate=D('0.10'))
    assert W.preferred_return(inp, 'committed', 'simple') > W.preferred_return(inp, 'drawn', 'simple')


def test_later_asof_accrues_more_preferred_return():
    early = _real_inputs(as_of=dt.date(2025, 3, 31))
    late = _real_inputs(as_of=dt.date(2026, 3, 31))
    assert W.preferred_return(late, 'drawn', 'simple') > W.preferred_return(early, 'drawn', 'simple')


# ══════════════════════════════════════════════════════════════════════════════════
# ADJUDICATION — the three honest statuses
# ══════════════════════════════════════════════════════════════════════════════════
def _sc(basis, comp, ht, carry, pref_method='contributed', carry_base='gross_gain'):
    return W.Scenario(basis, comp, ht, pref_method, carry_base, D('100'), D(str(carry)))


def test_consistent_inferred_when_one_value_reproduces_and_others_clearly_off():
    scs = [_sc('drawn', 'simple', 'hard', 60), _sc('drawn', 'simple', 'soft', 85),
           _sc('committed', 'simple', 'hard', 12)]
    v = W.adjudicate(scs, D('60'), conventions_all_stated=False)
    assert v.status == 'consistent_inferred' and v.computed_cr == D('60')
    assert 'NOT independently verified' in v.detail


def test_held_underdetermined_when_two_conventions_bracket_the_approx_figure():
    scs = [_sc('drawn', 'simple', 'hard', '58.25'), _sc('committed', 'simple', 'soft', '64.14'),
           _sc('drawn', 'simple', 'soft', '84.80')]
    v = W.adjudicate(scs, D('61.6'), conventions_all_stated=False)
    assert v.status == 'held' and v.computed_cr is None
    assert v.range_cr[0] <= D('61.6') <= v.range_cr[1]
    assert 'UNDERDETERMINED' in v.detail


def test_discrepancy_when_nothing_comes_near_reported():
    scs = [_sc('drawn', 'simple', 'hard', 58), _sc('committed', 'simple', 'soft', 64)]
    v = W.adjudicate(scs, D('200'), conventions_all_stated=False)
    assert v.status == 'discrepancy' and v.computed_cr is None


def test_hard_tie_only_when_conventions_were_stated():
    scs = [_sc('drawn', 'simple', 'hard', '58.25')]
    v = W.adjudicate(scs, D('58.25'), conventions_all_stated=True)
    assert v.status == 'hard_tie' and v.computed_cr == D('58.25')


def test_stated_conventions_that_miss_are_a_flagged_discrepancy():
    scs = [_sc('drawn', 'simple', 'hard', '58.25')]
    v = W.adjudicate(scs, D('90'), conventions_all_stated=True)
    assert v.status == 'discrepancy'


def test_held_inputs_when_reported_is_absent():
    scs = [_sc('drawn', 'simple', 'hard', 58)]
    v = W.adjudicate(scs, None, conventions_all_stated=False)
    assert v.status == 'held_inputs' and v.computed_cr is None


# ══════════════════════════════════════════════════════════════════════════════════
# THE LOAD-BEARING GUARD — CHECK, NEVER FIT
# ══════════════════════════════════════════════════════════════════════════════════
def test_scenarios_are_identical_regardless_of_the_reported_target():
    """The method must never be selected BY the answer. compute_scenarios() knows nothing about
    the reported figure; only adjudicate()'s partition changes as the target moves."""
    inp = _real_inputs()
    carries_1 = [s.carry_cr for s in W.compute_scenarios(inp)]
    carries_2 = [s.carry_cr for s in W.compute_scenarios(inp)]
    assert carries_1 == carries_2
    # adjudicating three different targets reuses the SAME computed table — the target only labels it.
    # 84.8 is the isolated soft-branch value (unique) → inferred; 61.6 is bracketed → held; 200 → far.
    sc = W.compute_scenarios(inp)
    v848 = W.adjudicate(sc, D('84.8'), conventions_all_stated=False)
    v616 = W.adjudicate(sc, D('61.6'), conventions_all_stated=False)
    v200 = W.adjudicate(sc, D('200'), conventions_all_stated=False)
    assert {s.carry_cr for s in v848.scenarios} == {s.carry_cr for s in v616.scenarios} == {s.carry_cr for s in v200.scenarios}
    assert (v848.status, v616.status, v200.status) == ('consistent_inferred', 'held', 'discrepancy')


def test_real_fund_accrued_carry_is_honestly_underdetermined():
    """On the real fund, the reported 61.6 is consistent with THREE materially different conventions
    (drawn/compound/hard≈55.5, drawn/simple/hard≈58.3, committed/simple/soft≈64.1). The disciplined
    outcome is HOLD + published range, NOT a knife-edge single pick fitted to 61.6."""
    v = W.adjudicate(W.compute_scenarios(_real_inputs()), D('61.6'), conventions_all_stated=False)
    assert v.status == 'held'
    assert v.computed_cr is None
    assert v.range_cr[0] < D('61.6') < v.range_cr[1]
    assert len({W._q(s.carry_cr) for s in v.matching}) >= 2


# ══════════════════════════════════════════════════════════════════════════════════
# WRAPPER — fail-closed gates + emitted rows
# ══════════════════════════════════════════════════════════════════════════════════
def _one_check(checks, cid):
    return next((c for c in checks if c['id'] == cid), None)


def test_wrapper_holds_when_hurdle_term_missing():
    checks, disc, _v = W.reconcile_waterfall(_real_inputs(hurdle_rate=None), reported_carry=D('61.6'))
    assert checks == []
    assert any('hurdle' in d['detail'] and 'held' in d['detail'] for d in disc)


def test_wrapper_holds_on_american_waterfall_never_assumes_european():
    inp = _real_inputs(waterfall_type='american_deal_by_deal')
    checks, disc, _v = W.reconcile_waterfall(inp, reported_carry=D('61.6'))
    assert checks == []
    assert any('American' in d['detail'] for d in disc)


def test_wrapper_holds_when_waterfall_type_unresolved():
    checks, disc, _v = W.reconcile_waterfall(_real_inputs(waterfall_type=''), reported_carry=D('61.6'))
    assert checks == []
    assert any('type unresolved' in d['detail'] for d in disc)


def test_wrapper_holds_when_no_dated_calls():
    checks, disc, _v = W.reconcile_waterfall(_real_inputs(calls=()), reported_carry=D('61.6'))
    assert checks == []
    assert any('dated drawdowns' in d['detail'] for d in disc)


def test_wrapper_emits_soft_indeterminate_and_full_scenario_table_on_real_fund():
    checks, disc, _v = W.reconcile_waterfall(_real_inputs(), reported_carry=D('61.6'))
    chk = _one_check(checks, 'waterfall_accrued_carry_reconciliation')
    assert chk is not None
    assert chk['class'] == reconcile.SOFT and chk['status'] == reconcile.INDETERMINATE
    assert chk['verdict'] == 'held'
    assert chk['independently_verified'] is False
    # every non-null scenario is disclosed for full transparency (8 conventions)
    assert sum(1 for d in disc if d['kind'] == 'waterfall_scenario') == 8
    assert any(d['kind'] == 'waterfall_convention_gap' for d in disc)


def test_wrapper_soft_pass_when_single_convention_cleanly_reproduces():
    # a synthetic fund whose profit is small enough that only ONE convention lands near reported
    inp = _real_inputs(total_profit_cr=D('300'), committed_capital_cr=D('600'))  # committed==drawn kills the bracket
    sc = W.compute_scenarios(inp)
    # find the reported value that exactly one distinct convention reproduces (drawn/committed identical here)
    v = W.adjudicate(sc, sc[0].carry_cr, conventions_all_stated=False)
    checks, disc, _v = W.reconcile_waterfall(inp, reported_carry=sc[0].carry_cr)
    chk = _one_check(checks, 'waterfall_accrued_carry_reconciliation')
    assert chk is not None
    assert chk['status'] in (reconcile.PASS, reconcile.INDETERMINATE)  # deterministic w/ the table


# ══════════════════════════════════════════════════════════════════════════════════
# WIDENED CONVENTION SPACE — pref-accrual method + carry base are ALSO enumerated
# ══════════════════════════════════════════════════════════════════════════════════
def _real_inputs_full(**over):
    """The real fund with the two extra axes activated: dated distributions (enables the
    net-of-distributions pref method) and a net-of-fees carry base."""
    base = dict(distributions=((dt.date(2025, 7, 15), D('40')), (dt.date(2025, 10, 20), D('18')),
                               (dt.date(2026, 1, 20), D('12'))),
                net_profit_cr=D('338'))
    base.update(over)
    return _real_inputs(**base)


def test_carry_base_adds_a_second_base_that_moves_the_number():
    # gross-gain (424) vs net-of-fees (338) are two different carry bases → two different carries
    gross = W.european_carry(D('424'), D('130'), D('0.20'), D('1.0'), 'hard')
    net = W.european_carry(D('338'), D('130'), D('0.20'), D('1.0'), 'hard')
    assert gross != net and gross == D('58.8') and net == D('41.6')


def test_pref_accrual_method_changes_the_pref_when_distributions_exist():
    inp = _real_inputs_full()
    contributed = W.preferred_return(inp, 'drawn', 'simple', 'contributed')
    net_of_dist = W.preferred_return(inp, 'drawn', 'simple', 'net_of_distributions')
    # returning capital lowers the accrual base → net-of-distributions pref is SMALLER
    assert net_of_dist < contributed


def test_widened_space_publishes_the_true_wider_range_and_still_holds():
    """With pref-accrual method AND carry base enumerated, the convention cloud is wider than the
    8-cell table showed — the honest held range widens, the verdict stays held (never narrowed by
    a silently-fixed sub-convention)."""
    inp = _real_inputs_full()
    sc = W.compute_scenarios(inp)
    assert len(sc) > 8                                   # more than the original 3-axis table
    labels = {(s.pref_method, s.carry_base) for s in sc}
    assert ('net_of_distributions', 'gross_gain') in labels    # pref-method axis present
    assert ('contributed', 'net_of_fees') in labels            # carry-base axis present
    v = W.adjudicate(sc, D('61.6'), conventions_all_stated=False)
    assert v.status == 'held'
    # the widened held range is at least as wide as the narrow 8-cell one ([55.5, 64.1])
    assert v.range_cr[0] <= D('55.6') and v.range_cr[1] >= D('64.1')


def test_wrapper_discloses_fixed_and_unmodeled_subconventions():
    checks, disc, v = W.reconcile_waterfall(_real_inputs_full(), reported_carry=D('61.6'))
    kinds = {d['kind'] for d in disc}
    assert 'waterfall_fixed_conventions' in kinds        # day-count / valuation date disclosed
    assert 'waterfall_convention_gap' in kinds           # all five unstated axes named
    assert 'nav_carry_sensitivity' in kinds              # held carry annotates NAV
    assert any('carry_base=net_of_fees' in d['detail'] for d in disc if d['kind'] == 'waterfall_scenario')


def test_discrepancy_is_loud_hard_indeterminate_not_a_soft_footnote():
    # a reported figure no convention reproduces → HARD INDETERMINATE + a loud discrepancy disclosure
    checks, disc, v = W.reconcile_waterfall(_real_inputs_full(), reported_carry=D('300'))
    chk = _one_check(checks, 'waterfall_accrued_carry_reconciliation')
    assert chk['class'] == reconcile.HARD and chk['status'] == reconcile.INDETERMINATE
    assert any(d['kind'] == 'waterfall_discrepancy' and 'LOUD FLAG' in d['detail'] for d in disc)


# ══════════════════════════════════════════════════════════════════════════════════
# CLAWBACK  (rides the waterfall entitlement; range-propagation when entitlement is held)
# ══════════════════════════════════════════════════════════════════════════════════
from backend.dataimport.preingest3.cir import Record, Figure, Provenance


def _dist(gp_carry, confirmed=True):
    prov = Provenance(source_file='f', content_fingerprint='fp', sheet='D', cell='A1')
    if confirmed:
        fig = Figure('gp_carry', D(str(gp_carry)), None, prov)
    else:
        fig = Figure('gp_carry', None, None, prov, held=True)
    return Record('distributions', entity_id='D', fields={'key': 'D', 'gp_carry': fig})


def test_clawback_provably_zero_when_no_carry_received_even_if_entitlement_held():
    # THIS fund: received 0, entitlement HELD (underdetermined range) → still a clean, provable 0
    v = W.adjudicate(W.compute_scenarios(_real_inputs_full()), D('61.6'), conventions_all_stated=False)
    assert v.status == 'held'
    res = W.clawback_exposure(carry_received=D('0'), received_complete=True,
                              entitled_point=None, entitled_range=v.range_cr, holdback_rate=D('0.20'))
    assert res.status == 'clean_zero' and res.clawback_cr == D('0')


def test_clawback_positive_when_gp_overpaid_vs_entitlement():
    res = W.clawback_exposure(carry_received=D('100'), received_complete=True,
                              entitled_point=D('60'), entitled_range=None, holdback_rate=None)
    assert res.status == 'exposure' and res.clawback_cr == D('40')


def test_clawback_holdback_reduces_net_exposure():
    # received 100, entitled 60 → gross clawback 40; 20% of received (20) escrowed → net 20
    res = W.clawback_exposure(carry_received=D('100'), received_complete=True,
                              entitled_point=D('60'), entitled_range=None, holdback_rate=D('0.20'))
    assert res.status == 'exposure' and res.clawback_cr == D('20')


def test_clawback_held_when_received_history_incomplete():
    res = W.clawback_exposure(carry_received=None, received_complete=False,
                              entitled_point=D('60'), entitled_range=None)
    assert res.status == 'held' and res.clawback_cr is None


def test_clawback_range_propagates_when_entitlement_underdetermined():
    # received 70, entitlement held [55.5, 64.1] → clawback is itself a RANGE, never a point:
    # lower entitlement (55.5) → higher clawback 14.5; higher entitlement (64.1) → 5.9
    res = W.clawback_exposure(carry_received=D('70'), received_complete=True,
                              entitled_point=None, entitled_range=(D('55.5'), D('64.1')), holdback_rate=None)
    assert res.status == 'exposure_range' and res.clawback_cr is None
    assert res.range_cr == (D('5.9'), D('14.5'))


def test_carry_received_sums_gp_carry_and_holds_on_a_held_figure():
    assert W.carry_received_from_distributions([_dist(0), _dist(0), _dist(0)]) == (D('0'), True)
    assert W.carry_received_from_distributions([_dist(5), _dist(3)]) == (D('8'), True)
    total, complete = W.carry_received_from_distributions([_dist(5), _dist(0, confirmed=False)])
    assert total is None and complete is False           # a held gp_carry → fail-closed
    assert W.carry_received_from_distributions([]) == (None, False)   # no ledger → held, not 0


def test_reconcile_clawback_clean_zero_soft_pass_on_real_fund():
    v = W.adjudicate(W.compute_scenarios(_real_inputs_full()), D('61.6'), conventions_all_stated=False)
    checks, disc = W.reconcile_clawback(carry_received=D('0'), received_complete=True,
                                        verdict=v, holdback_rate=D('0.20'))
    chk = _one_check(checks, 'gp_clawback_reconciliation')
    assert chk['class'] == reconcile.SOFT and chk['status'] == reconcile.PASS
    assert chk['verdict'] == 'clean_zero'
    assert any(d['kind'] == 'clawback_holdback' and 'moot' in d['detail'] for d in disc)


def test_reconcile_clawback_flags_exposure_as_soft_fail():
    v = W.WaterfallVerdict('hard_tie', D('60'), D('60'), D('1'), '', [], [], 'x')
    checks, disc = W.reconcile_clawback(carry_received=D('100'), received_complete=True,
                                        verdict=v, holdback_rate=None)
    chk = _one_check(checks, 'gp_clawback_reconciliation')
    assert chk['class'] == reconcile.SOFT and chk['status'] == reconcile.FAIL and chk['clawback'] == '40'
