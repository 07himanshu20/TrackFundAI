"""§4 cross-statement PENNY-TIE anchor (extract._cross_statement_tie_rebind) — reddening.

The anchor recovers a held stock by re-sourcing from an INDEPENDENT statement whose value
penny-matches the primary sheet's own concept row (balance-sheet cash == cash-flow closing, to the
paisa). It is fail-closed: no carrier, no raw penny-tie, or a scale-gap (power-of-ten, not penny) →
None (the hold stays).

STATUS: this proves the ENGINE hosts a second, different anchor type and that its discriminator is
correct on synthetic data. It does NOT prove the anchor's real-world correctness — no cell in the
10-file corpus forces it (CPC=within-statement, Aliste=scope-held, CSS=period) — so it is kept
fail-closed / no-op on the corpus (dual-run changes=0), AWAITING real-data validation on the messy
corpus. Principle-based synthetic sheets, not corpus cells.
"""
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from dataimport.preingest3.extract import _cross_statement_tie_rebind, _sheet_label_col
from dataimport.preingest3 import periods
from dataimport.preingest3.ratecard import default_inr_card

_IDENT = SimpleNamespace(content_fp='testfp')
_RC = default_inr_card('2025-02-28')


def _prof(bs_cash_row, cf_cash_row):
    """Two-sheet profile: primary 'BS' + carrier 'Cash Flow Statement', each with two dated months
    and a Total Assets line so the axis + frame resolve. `*_cash_row` = [Jan, Feb] cash values."""
    bs = [['', 'Jan-2025', 'Feb-2025'],
          ['Total Assets', 300000000, 300000000],
          ['Cash', *bs_cash_row]]
    cf = [['', 'Jan-2025', 'Feb-2025'],
          ['Net cash from operating activities', 1000000, 1000000],
          ['Closing cash balance', *cf_cash_row]]
    sheets = [SimpleNamespace(sheet='BS'), SimpleNamespace(sheet='Cash Flow Statement')]
    return {'sheets': sheets, 'grid': {'BS': bs, 'Cash Flow Statement': cf}}, bs


def _run(bs_cash_row, cf_cash_row):
    prof, bs = _prof(bs_cash_row, cf_cash_row)
    bs_ax = periods.detect_period_axis(bs)
    bs_lc = _sheet_label_col(bs, bs_ax.axis_rows[0])
    return _cross_statement_tie_rebind(
        'cash', prof, 'BS', bs, bs_lc, bs_ax, ident=_IDENT, label='T', geo_ccy=None,
        inr_mentioned=True, anchor_cr=Decimal('8.5'), rate_card=_RC, as_of=(2025, 2),
        require_bound=False, base_currency='INR')


class MustHandle(SimpleTestCase):
    def test_penny_perfect_tie_recovers_the_value(self):
        # BS cash row and CFS closing both carry 85,000,000 to the paisa → tie → emit 8.5 Cr
        fig = _run([85000000, 85000000], [85000000, 85000000])
        self.assertIsNotNone(fig)
        self.assertTrue(fig.confirmed)
        self.assertEqual(fig.value_cr, Decimal('8.5000'))


class MustNotMisfire(SimpleTestCase):
    def test_values_differ_no_tie_holds(self):
        # CFS closing 85M does not appear on the BS cash row (50k/60k) → no tie → None
        self.assertIsNone(_run([50000, 60000], [85000000, 85000000]))

    def test_power_of_ten_scale_gap_is_not_a_penny_tie(self):
        # BS 8.5M vs CFS 85M — a 10x gap is NOT penny-perfect → must not tie
        self.assertIsNone(_run([8500000, 8500000], [85000000, 85000000]))

    def test_no_carrier_holds(self):
        # a lone BS with no independent carrier → None
        prof = {'sheets': [SimpleNamespace(sheet='BS')],
                'grid': {'BS': [['', 'Jan-2025', 'Feb-2025'],
                                ['Total Assets', 300000000, 300000000],
                                ['Cash', 85000000, 85000000]]}}
        bs = prof['grid']['BS']; ax = periods.detect_period_axis(bs)
        lc = _sheet_label_col(bs, ax.axis_rows[0])
        self.assertIsNone(_cross_statement_tie_rebind(
            'cash', prof, 'BS', bs, lc, ax, ident=_IDENT, label='T', geo_ccy=None,
            inr_mentioned=True, anchor_cr=Decimal('300'), rate_card=_RC, as_of=(2025, 2),
            require_bound=False, base_currency='INR'))
