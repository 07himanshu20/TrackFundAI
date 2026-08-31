"""U6 Phase 4 — FX_UNCOVERED (benign, a rate fixes it) is held CLEANLY and distinctly from
UNEXPECTED_ERROR (a genuine code fault), so a real bug can never hide inside a benign FX hold; and the
ledger FX crash found in Phase 2 (a foreign-resolved-but-unrated figure RAISED at conversion) can never
recur for ANY file.

Root, universal fix (not a per-site patch): a resolved FOREIGN currency the card does not cover escalates
the whole monetary FRAME (resolve_monetary_frame) — fail-closed and UNIFORM. Every emit path is gated on
the frame (frame_ok / frame.escalate), so `to_inr` is NEVER called on an uncovered currency; it cannot
raise for a missing rate anywhere. The remaining `to_inr` sites are all either frame-gated or wrapped:
the two statement-scale magnitude checks (units 230/284) and the dead per-figure resolve() (units 377,
now wrapped too) fail-closed rather than crash.

Run:
  pytest backend/dataimport/preingest3/tests/test_fx_uncovered_split.py -p no:cacheprovider -o addopts="" -m ""
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from backend.dataimport.preingest3 import units, ledger, extract as _extract
from backend.dataimport.preingest3.cir import Provenance
from backend.dataimport.preingest3.ledger import CAPITAL_CALLS
from backend.dataimport.preingest3.ratecard import default_inr_card
from backend.dataimport.preingest3.ratecard_intake import card_from_manual

RC = default_inr_card('2026-06-30')                       # INR-only, no foreign rate


# ── FX_UNCOVERED is a clean FRAME-level hold, with a machine-readable flag ────────────────────────────
def test_frame_holds_foreign_without_a_rate_as_fx_uncovered():
    fr = units.resolve_monetary_frame(stmt_currency='MYR', geo_currency='MYR', inr_mentioned=False,
                                      declared_unit='crore', sample_values=[Decimal('5')],
                                      anchor_cr=None, ratecard=RC)
    assert fr.escalate and fr.currency == 'MYR' and fr.scale == 'crore'
    assert 'fx_uncovered_MYR' in fr.flags and 'FX_UNCOVERED' in fr.reason


def test_frame_resolves_the_same_foreign_WITH_a_rate():
    # negative control: identical statement, but the card carries MYR → the frame RESOLVES (not FX-held).
    card = card_from_manual([{'currency': 'MYR', 'rate': '18.6', 'date': '2026-06-30', 'source': 'x'}],
                            as_of='2026-06-30')
    fr = units.resolve_monetary_frame(stmt_currency='MYR', geo_currency='MYR', inr_mentioned=False,
                                      declared_unit='crore', sample_values=[Decimal('5')],
                                      anchor_cr=None, ratecard=card)
    assert not fr.escalate and fr.currency == 'MYR' and fr.scale == 'crore'
    assert not any('fx_uncovered' in f for f in fr.flags)


# ── the Phase-2 ledger crash: a foreign-resolved-but-unrated ledger block HOLDS, never RAISES ─────────
_USD_CR_NO_RATE = [['Capital call ledger (USD Cr)'], ['Call no', 'date', 'amount'],
                   ['C1', 'a', 150], ['C2', 'b', 150], ['Total called', '', 300]]


def test_ledger_foreign_resolved_no_rate_HOLDS_not_raises():
    # currency (USD) AND scale (Cr) both resolve, but the card has no USD rate. Pre-fix this reached
    # _to_cr → to_inr → RateCardError (uncaught crash). Now the frame escalates FX_UNCOVERED → frame_ok
    # False → held via the else branch → _to_cr is never called. Must not raise.
    lr = ledger.extract_from_sheet(_USD_CR_NO_RATE, 'S1', CAPITAL_CALLS, anchor_cr=None,
                                   label='t', content_fp='fp', rate_card=RC)
    assert lr.held is True
    assert all(f.held for rec in lr.records for f in rec.figures())


def test_ledger_foreign_with_a_rate_CONVERTS():
    # negative control: the SAME block with a USD rate converts and ties (150+150 USD-Cr → ×83). Proves
    # the fix is a targeted FX hold, not 'hold everything'.
    card = card_from_manual([{'currency': 'USD', 'rate': '83', 'date': '2026-06-30', 'source': 'x'}],
                            as_of='2026-06-30')
    lr = ledger.extract_from_sheet(_USD_CR_NO_RATE, 'S1', CAPITAL_CALLS, anchor_cr=None,
                                   label='t', content_fp='fp', rate_card=card)
    assert lr.held is False
    assert sum((f.value_cr for f in lr.amount_figures), Decimal('0')) == Decimal('24900')  # 300 × 83


# ── a genuine (non-FX) fault surfaces as UNEXPECTED_ERROR, distinct from an FX hold (reddening) ───────
class _RaisingCard:
    """A card whose to_inr raises a NON-FX error — stands in for a real code fault on the emit path."""
    rates: dict = {}
    as_of = '2026-06-30'
    card_id = 'rc_stub'

    def to_inr(self, amount, currency):
        raise ValueError('synthetic non-FX fault')


def test_unexpected_error_surfaces_distinct_from_fx_uncovered():
    col = SimpleNamespace(value=Decimal('100'), basis='point_in_time', months=0,
                          axis='SINGLE_TOTAL', escalate=False, flags=[], reason='')
    prov = Provenance(source_file='t', content_fingerprint='fp', sheet='S', cell='A1', row_label='Revenue')
    frame = units.MonetaryFrame('INR', 'absolute', False, 'resolved', [])   # INR frame → reaches to_inr
    fig = _extract._emit_from_collapse('revenue', col, prov, stmt_kind=None, frame=frame, rows=[],
                                       ax=None, label_col=0, ebitda_row=None, anchor_cr=None,
                                       rate_card=_RaisingCard())
    assert fig.held and fig.hold_reason.startswith('UNEXPECTED_ERROR')
    assert 'ValueError' in fig.hold_reason and 'FX_UNCOVERED' not in fig.hold_reason


# ── the last unwrapped to_inr (dead per-figure resolve(), units 377) can no longer crash on revival ──
def test_dead_resolve_path_no_longer_raises_on_foreign_without_a_rate():
    # resolve() is superseded (only dead normalize.py called it); wrapping its magnitude cross-check means
    # even a future revival cannot re-introduce the crash class — foreign + anchor + no rate → no raise.
    ur = units.resolve(value=Decimal('100'), declared_unit='crore', declared_ccy='MYR',
                       anchor_cr=Decimal('80'), ratecard=RC)
    assert ur is not None                                  # did NOT raise
    assert 'magnitude_unchecked_no_fx' in ur.flags


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'ok  {name}')
    print('ALL PASS')
