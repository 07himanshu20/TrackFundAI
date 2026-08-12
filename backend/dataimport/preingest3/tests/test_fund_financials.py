"""Phase D — fund-financials MVP slice: router + capital-account flow extraction.

Proves, on the REAL fund files and with synthetic negative controls:
  • the 3 fund-level files route to 'fund_financials' (not held 'unknown'), while MIS
    and investment-schedule files are UNAFFECTED (no MIS regression by routing);
  • `called`/`distributed` extract to the right value AND source cell (600 @ Drawdowns,
    70 @ Distributions net) — value-audited, cumulative basis;
  • the Σ(events)=stated-total identity is the fail-closed verifier — a mismatch HOLDS
    (never ships an unreconciled total), and the total row is excluded from the event
    sum (the amount column is never double-counted);
  • a budget/forecast sheet and the LP register are NOT mined for actuals (scope guards).
"""
import os
from decimal import Decimal

from backend.dataimport.preingest3 import pipeline, fund_extract
from backend.dataimport.preingest3.profiler import profile_file
from backend.dataimport.preingest3.ratecard import default_inr_card
from backend.dataimport.preingest3.cir import Figure

IN = 'backend/media/preingest/trivesta/100e86d5/in'
CAPITAL = 'TFAI_Capital_Calls_and_Distributions_draft.xlsx'
ACCOUNTS = 'TFAI_Fund_Accounts_Fees_Budget_Compliance.xlsx'
TERMS = 'TFAI_Fund_Terms_and_LP_Register_wip.xlsx'
SCHEDULE = 'TFAI_Investments_and_Deployment.xlsx'
MIS = 'AVF_2026_03_11_P_Hubler_MIS_Feb26.xlsx'
_RC = default_inr_card('2026-02-28')


def _prof(name):
    return profile_file(name, os.path.join(IN, name))


# ── routing: fund files promoted, MIS/schedule untouched ─────────────────────
def test_fund_level_files_route_fund_financials():
    for f in (CAPITAL, ACCOUNTS, TERMS):
        assert pipeline._route_structural(_prof(f)) == 'fund_financials', f

def test_routing_does_not_disturb_mis_or_schedule():
    assert pipeline._route_structural(_prof(MIS)) == 'mis'         # company MIS still MIS
    assert pipeline._route_structural(_prof(SCHEDULE)) == 'fund'   # investment schedule still fund


# ── extraction: value + source cell, cumulative basis ────────────────────────
def test_capital_account_flows_extracted_to_source_cell():
    rec = fund_extract.extract_fund_financials(CAPITAL, os.path.join(IN, CAPITAL), _prof(CAPITAL), rate_card=_RC)
    called, dist = rec.fields['called'], rec.fields['distributed']
    assert called.confirmed and called.value_cr == Decimal('600')
    assert called.provenance.sheet == 'Drawdowns' and called.basis == 'cumulative'
    assert dist.confirmed and dist.value_cr == Decimal('70')       # NET (not gross), Σ==total
    assert dist.provenance.sheet == 'Distributions' and dist.basis == 'cumulative'

def test_forecast_and_lp_register_are_not_mined_for_flows():
    # Fund_Accounts has a Budget-vs-Act sheet (forecast); Fund_Terms has the LP register
    # (per-LP rows). Neither is an ACTUALS capital-account event sheet → no flows this slice.
    assert fund_extract.extract_fund_financials(ACCOUNTS, os.path.join(IN, ACCOUNTS), _prof(ACCOUNTS), rate_card=_RC) is None
    assert fund_extract.extract_fund_financials(TERMS, os.path.join(IN, TERMS), _prof(TERMS), rate_card=_RC) is None


# ── the Σ(events)=stated-total identity — fail-closed verifier ────────────────
def _grid(total_amount):
    return [
        [None, 'TFAI - Capital call', None, None],
        [None, '(all figures Rs Cr)', None, None],
        [None, 'Call no', 'amount', 'purpose'],
        [None, 'C1', Decimal('100'), 'x'],
        [None, 'C2', Decimal('200'), 'y'],
        [None, 'TOTAL CALLED', total_amount, None],
    ]

def test_sigma_identity_confirms_and_excludes_total_from_event_sum():
    val, info = fund_extract._extract_total(_grid(Decimal('300')), 'called')
    assert val == Decimal('300')                        # the reconciled total
    assert info['sum_events'] == '300'                  # 100+200 — the TOTAL row is NOT summed in
    assert info['confirmed_by'] == 'sum==total'

def test_sigma_identity_holds_on_mismatch_NEGATIVE_CONTROL():
    # events sum to 300 but the stated total lies (999) → must HOLD, never emit either.
    # (Before the identity check this would have shipped a number; it must redden.)
    val, info = fund_extract._extract_total(_grid(Decimal('999')), 'called')
    assert val is None and 'held' in info['reason']

def test_flow_extract_holds_when_unit_unresolved_fail_closed():
    # a capital-account event sheet with NO declared unit and no anchor → frame
    # unresolved → HOLD (never assume crore). Same grid, unit line removed.
    rows = [r for r in _grid(Decimal('300')) if r[1] != '(all figures Rs Cr)']
    prof = {'sheets': [type('S', (), {'sheet': 'Drawdowns'})()], 'grid': {'Drawdowns': rows}}
    fig = fund_extract._extract_concept('called', prof, _RC, source_label='x', content_fp='')
    assert fig is not None and fig.held and 'frame unresolved' in fig.hold_reason


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn(); print(f'ok  {name}')
    print('ALL PASS — fund router + capital-account flows + Σ-identity fail-closed')
