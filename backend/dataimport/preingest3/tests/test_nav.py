"""Phase D — fund NAV struck by the capital-account ROLL-FORWARD, emitted as TWO labeled
bases (LP NAV net-of-carry / gross NAV before-carry) per the NAV design ruling.

Proves on synthetic negative controls + the real fund files:
  • the roll-forward computes both bases from cited components (806.25 / 867.85);
  • the AS-OF guard HOLDS when an input flow is dated after the NAV as-of (period mix);
  • the CARRY cross-check REDDENS when accrued carry exceeds the verified-rate ceiling,
    and retracts only LP NAV (gross still emits) — proportionate;
  • the EMIT-vs-HOLD rule HOLDS when a hard component is missing;
  • the fee-vs-actual NAV LEG rules NAV out as the fee base (committed uniquely confirmed);
  • the single-source status is disclosed as PERMANENT (not 'pending data').
"""
import datetime as dt
import os
from decimal import Decimal as D

import pytest

from backend.dataimport.preingest3 import nav, assemble, pipeline
from backend.dataimport.preingest3.cir import CIR, Record, Figure, Provenance
from backend.dataimport.preingest3.ratecard import default_inr_card

IN = 'backend/media/preingest/trivesta/100e86d5/in'
_real = pytest.mark.skipif(not os.path.isdir(IN), reason='real fixture files not present')


def _pv(sheet='S', cell='A1'):
    return Provenance(source_file='f', content_fingerprint='', sheet=sheet, cell=cell)


def _pnl_inputs():
    """The real accounts P&L, as an accounts-file NAVInputs (net ITD P&L before carry = 337.85)."""
    return nav.NAVInputs(
        source='acc', sheet='fund ac and PnL',
        pnl=[('Interest income', D('18'), 'F12'), ('Dividend income', D('5.5'), 'F13'),
             ('Realised gain on investments', D('45'), 'F14'),
             ('Net change in unrealised FV of investments', D('379'), 'F15'),
             ('Mgmt fee (ITD)', D('-94.95'), 'F16'), ('Trustee & custodian', D('-4.2'), 'F17'),
             ('Audit & legal', D('-3.1'), 'F18'), ('Fund operating', D('-7.4'), 'F19')],
        carry=(D('61.6'), ''),
        realised=(D('45'), 'F14'), unrealised=(D('379'), 'F15'),
        nav_asof_label=('balances as on 30-Jun-26 for NAV', 'fund ac and PnL!B3'), flows=[])


def _flows(last_call_date=dt.date(2025, 9, 30)):
    """Dated call + distribution ledgers on separate sheets. Drawdowns Σ=600, Dist Σ=70."""
    return nav.NAVInputs(source='cc', flows=[
        (dt.date(2021, 9, 30), D('150'), 'D6', 'Drawdowns'), (dt.date(2022, 6, 30), D('140'), 'D7', 'Drawdowns'),
        (dt.date(2023, 6, 30), D('120'), 'D8', 'Drawdowns'), (dt.date(2024, 6, 30), D('100'), 'D9', 'Drawdowns'),
        (last_call_date, D('90'), 'D10', 'Drawdowns'),
        (dt.date(2025, 7, 15), D('40'), 'F6', 'Distributions'), (dt.date(2025, 10, 20), D('18'), 'F7', 'Distributions'),
        (dt.date(2026, 1, 20), D('12'), 'F8', 'Distributions')])


def _capital(called=D('600'), distributed=D('70'), called_held=False):
    return Record('fund_financials', 'fund', {'fund': 'fund',
        'called': Figure('called', called, None, _pv('Drawdowns', 'D11'), basis='cumulative', held=called_held),
        'distributed': Figure('distributed', distributed, None, _pv('Distributions', 'G9'), basis='cumulative')})


def _run(pnl=None, flows=None, capital=None, carry_rate=D('0.20'),
         fee_actual=(D('20'), None, []), committed_base=D('1000')):
    pnl = pnl if pnl is not None else _pnl_inputs()
    flows = flows if flows is not None else _flows()
    fa = (fee_actual[0], _pv('Budget vs Act', 'D9'), []) if fee_actual else None
    return nav.reconcile_nav([pnl, flows], capital=capital or _capital(), carry_rate=carry_rate,
                             carry_rate_prov=_pv('terms', 'C14'), as_of='2026-06-30',
                             fee_actual=fa, committed_base=committed_base)


def _check(checks, cid):
    return next((c for c in checks if c['id'] == cid), None)


# ── happy path: two bases from cited components ──────────────────────────────
def test_rollforward_two_bases_from_components():
    rec, checks, disc = _run()
    assert rec.fields['gross_nav'].value_cr == D('867.85')     # 600 − 70 + 337.85
    assert rec.fields['lp_nav'].value_cr == D('806.25')        # gross − carry 61.6
    assert not rec.fields['gross_nav'].held and not rec.fields['lp_nav'].held
    assert _check(checks, 'nav_as_of_consistency')['status'] == 'pass'
    assert _check(checks, 'nav_carry_plausibility_ceiling')['status'] == 'pass'
    # both bases cite their component cells (derived-from provenance)
    assert 'Drawdowns!D11' in rec.fields['gross_nav'].provenance.derived_from
    assert 'fund ac and PnL!F15' in rec.fields['gross_nav'].provenance.derived_from


# ── AS-OF guard: a flow dated after the NAV as-of holds the roll-forward ──────
def test_as_of_guard_holds_on_flow_after_asof_NEGATIVE_CONTROL():
    # keep Drawdowns Σ=600 but date the last call 30-Sep-2026 (> as-of) — the group that ties
    # to called now straddles the cutoff → the roll-forward would mix periods → HOLD.
    rec, checks, _ = _run(flows=_flows(last_call_date=dt.date(2026, 9, 30)))
    assert _check(checks, 'nav_as_of_consistency')['status'] == 'fail'
    assert rec.fields['gross_nav'].held and rec.fields['lp_nav'].held
    assert 'as-of' in rec.fields['gross_nav'].hold_reason


# ── CARRY cross-check: carry above the verified-rate ceiling reddens, LP-only ─
def test_carry_cross_check_reddens_when_carry_exceeds_ceiling_NEGATIVE_CONTROL():
    # ceiling = 20%×(45+379)=84.8; a carry of 90 is inconsistent with the verified rate → the
    # cross-check FAILS and LP NAV (which subtracts carry) is held — but GROSS NAV still emits
    # (the mismatch impugns the carry, not the whole roll-forward). Proportionate retraction.
    pnl = _pnl_inputs(); pnl.carry = (D('90'), '')
    rec, checks, _ = _run(pnl=pnl)
    assert _check(checks, 'nav_carry_plausibility_ceiling')['status'] == 'fail'
    assert rec.fields['lp_nav'].held and 'ceiling' in rec.fields['lp_nav'].hold_reason
    assert not rec.fields['gross_nav'].held and rec.fields['gross_nav'].value_cr == D('867.85')


# ── EMIT-vs-HOLD rule: a missing HARD component holds BOTH bases ──────────────
def test_emit_vs_hold_holds_on_missing_hard_component_NEGATIVE_CONTROL():
    # called capital HELD (a hard component) → the roll-forward is incomplete → both bases
    # INCOMPLETE, never a number that silently drops the hole.
    rec, checks, _ = _run(capital=_capital(called_held=True))
    assert rec.fields['gross_nav'].held and rec.fields['lp_nav'].held
    assert 'called' in rec.fields['gross_nav'].hold_reason


# ── LP NAV holds when carry absent; gross still emits ────────────────────────
def test_lp_nav_holds_when_carry_absent():
    pnl = _pnl_inputs(); pnl.carry = None
    rec, checks, _ = _run(pnl=pnl)
    assert not rec.fields['gross_nav'].held and rec.fields['lp_nav'].held
    assert 'carry' in rec.fields['lp_nav'].hold_reason


# ── FOLD 3a: P&L 8-line sum carries hole-discipline (error / missing value → hole) ──
def test_pnl_hole_detection_on_error_and_missing_value():
    from backend.dataimport.preingest3.nav import _read_pnl_section
    clean = [['Fund P&L (inception to date)'], ['Interest', 18], ['Realised gain', 45],
             ['Unrealised FV', 379], ['Mgmt fee', -94.95]]
    err = [['Fund P&L'], ['Interest', 18], ['Realised gain', '#REF!'], ['Unrealised FV', 379], ['Fee', -94.95]]
    miss = [['Fund P&L'], ['Interest', 18], ['Realised gain', None], ['Unrealised FV', 379], ['Fee', -94.95]]
    trail = [['Fund P&L'], ['Interest', 18], ['Fee', -94.95], ['see notes tab']]      # trailing label ≠ hole
    assert _read_pnl_section(clean)[5] is False
    assert _read_pnl_section(err)[5] is True and 'REF' in _read_pnl_section(err)[6]
    assert _read_pnl_section(miss)[5] is True and 'no value' in _read_pnl_section(miss)[6]
    assert _read_pnl_section(trail)[5] is False        # a named row AFTER the last value is section-end


def test_pnl_hole_holds_both_bases_NEGATIVE_CONTROL():
    # a hole in the 8-line P&L sum must make net P&L INCOMPLETE and HOLD both bases — never a
    # silent short-sum that ships a wrong NAV.
    pnl = _pnl_inputs(); pnl.pnl_hole = True; pnl.pnl_hole_reason = 'error #REF! at row 4'
    rec, _, _ = _run(pnl=pnl)
    assert rec.fields['gross_nav'].held and rec.fields['lp_nav'].held
    assert 'hole' in rec.fields['gross_nav'].hold_reason


# ── FOLD 2: garbled/absent carry note → LP NAV holds, gross emits, disclosed ──
def test_garbled_carry_note_holds_lp_nav_only_NEGATIVE_CONTROL():
    from backend.dataimport.preingest3.nav import _read_pnl_section
    # carry note PRESENT but no parseable number in its prose → carry unreadable
    garble = [['Fund P&L'], ['Interest', 18], ['Realised gain', 45], ['Unrealised FV', 379],
              ['note: carry provision accrued — see waterfall tab', None]]
    pnl_out = _read_pnl_section(garble)
    assert pnl_out[3] is None and pnl_out[4] is True   # carry None, carry_unreadable True
    # in reconcile: LP NAV held (unknown carry), gross NAV still emits, and it is DISCLOSED
    pnl = _pnl_inputs(); pnl.carry = None; pnl.carry_unreadable = True
    rec, _, disc = _run(pnl=pnl)
    assert rec.fields['lp_nav'].held and 'unreadable' in rec.fields['lp_nav'].hold_reason
    assert not rec.fields['gross_nav'].held
    assert any(d['kind'] == 'nav_carry_unreadable' for d in disc)


# ── FOLD 1: carry check is a one-sided plausibility ceiling, basis deferred ───
def test_carry_framing_is_plausibility_ceiling_not_validation():
    _, checks, disc = _run()
    ceil = _check(checks, 'nav_carry_plausibility_ceiling')
    assert ceil['status'] == 'pass' and 'ONE-SIDED' in ceil['detail'] and 'waterfall' in ceil['detail']
    basis = next((d for d in disc if d['kind'] == 'nav_carry_basis'), None)
    assert basis is not None and 'UNREALISED' in basis['detail'] and 'waterfall slice' in basis['detail']


# ── FOLD 3b: unparseable/short-tying flow → cutoff unconfirmed → HOLD; boundary disclosed ──
def test_unconfirmed_cutoff_holds_and_boundary_disclosed_NEGATIVE_CONTROL():
    # drop a call (Drawdowns now sums to 510, not 600) → no group ties to called → cutoff
    # cannot be confirmed → HOLD (the shape an unparsed date takes: a dropped flow).
    short = nav.NAVInputs(source='cc', flows=[
        (dt.date(2021, 9, 30), D('150'), 'D6', 'Drawdowns'), (dt.date(2022, 6, 30), D('140'), 'D7', 'Drawdowns'),
        (dt.date(2023, 6, 30), D('120'), 'D8', 'Drawdowns'), (dt.date(2024, 6, 30), D('100'), 'D9', 'Drawdowns'),
        (dt.date(2025, 7, 15), D('40'), 'F6', 'Distributions'), (dt.date(2025, 10, 20), D('18'), 'F7', 'Distributions'),
        (dt.date(2026, 1, 20), D('12'), 'F8', 'Distributions')])
    rec, checks, disc = _run(flows=short)
    assert _check(checks, 'nav_as_of_consistency')['status'] == 'fail'
    assert rec.fields['gross_nav'].held and 'unconfirmed' in _check(checks, 'nav_as_of_consistency')['detail']
    assert any(d['kind'] == 'nav_date_format_boundary' for d in disc)   # boundary named, not a surprise


# ── the fee-vs-actual NAV LEG rules NAV out as the fee base ───────────────────
def test_fee_base_vs_nav_rules_out_nav():
    _, checks, _ = _run()
    leg = _check(checks, 'fee_base_vs_nav')
    assert leg['status'] == 'pass' and 'ruled out' in leg['detail']    # 2%×NAV ≠ actual 20


# ── single-source disclosure is PERMANENT ────────────────────────────────────
def test_single_source_disclosure_is_permanent():
    _, _, disc = _run()
    ss = next((d for d in disc if d['kind'] == 'nav_single_source'), None)
    assert ss is not None and 'permanent' in ss['detail'] and 'no independent second path' in ss['detail']


# ── real files (production path) ─────────────────────────────────────────────
@_real
def test_nav_on_real_files():
    FUND = ['TFAI_Fund_Terms_and_LP_Register_wip.xlsx',
            'TFAI_Fund_Accounts_Fees_Budget_Compliance.xlsx',
            'TFAI_Capital_Calls_and_Distributions_draft.xlsx']
    res = pipeline.run([(f, os.path.join(IN, f)) for f in FUND], as_of='2026-06-30', org='t',
                       rate_card=default_inr_card('2026-06-30'))
    rec = next((r for r in res.cir.records if r.domain == 'nav'), None)
    assert rec is not None
    assert rec.fields['gross_nav'].value_cr == D('867.85') and rec.fields['lp_nav'].value_cr == D('806.25')
    assert rec.fields['carry_corroborated'] is True and rec.fields['single_source'] is True
    ids = {c['id']: c['status'] for c in res.cir.checks}
    assert ids['nav_as_of_consistency'] == 'pass'
    assert ids['nav_carry_plausibility_ceiling'] == 'pass'
    assert ids['fee_base_vs_nav'] == 'pass'
    # Fund_Summary shows the two labeled bases
    wb = assemble.build(res.cir, rate_card=default_inr_card('2026-06-30'))
    rows = [tuple(r) for r in wb['Fund_Summary'].iter_rows(values_only=True)]
    assert any(r and r[0] and 'LP (net of accrued carry)' in str(r[0]) and r[1] == 806.25 for r in rows)
    assert any(r and r[0] and 'Gross (before carry)' in str(r[0]) and r[1] == 867.85 for r in rows)


if __name__ == '__main__':
    for n, fn in sorted(globals().items()):
        if n.startswith('test_') and callable(fn) and 'real' not in n:
            fn(); print(f'ok  {n}')
