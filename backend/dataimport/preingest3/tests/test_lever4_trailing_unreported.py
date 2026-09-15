"""Lever 4 — drop trailing NOT-YET-REPORTED period columns (extract._drop_trailing_unreported).

A partial-year monthly file carries future months as 0-filled columns ('January'…'December', data
only through May). Leaving them in makes the as-of pick an empty future month — a WRONG-PERIOD bind
(the engine nearly shipped CSS's January cash as May's). The fix drops the trailing run of columns
that are UNREPORTED — every data cell in the WHOLE column is 0/empty — so a genuine latest-period 0
(whose column still carries other populated lines) is KEPT, never stripped.

DISCIPLINE: principle-based synthetic axes, not the CSS cells. Must-handle (trailing all-zero months
dropped) AND must-not-misfire (genuine latest-0 kept because its column is populated; an intermediate
gap kept; no-trailing-zeros unchanged; all-unreported falls back, never empty; TOTAL/YTD never dropped).
"""
from django.test import SimpleTestCase

from dataimport.preingest3.extract import _drop_trailing_unreported
from dataimport.preingest3.periods import PeriodColumn, PeriodAxis, MONTH, TOTAL


def _ax(cols):
    return PeriodAxis(columns=cols, axis_rows=[0], is_comparison_grid=False, is_time_series=True)


def _months(n, start_col=1):
    return [PeriodColumn(start_col + i, f'm{i+1}', MONTH, 1, (2025, i + 1)) for i in range(n)]


def _kept_cols(rows, cols):
    return [c.col for c in _drop_trailing_unreported(rows, _ax(cols), cols)]


class MustHandle(SimpleTestCase):
    def test_trailing_all_zero_months_are_dropped(self):
        cols = _months(4)                                  # cols 1..4 = Jan..Apr
        rows = [['hdr', 'm1', 'm2', 'm3', 'm4'],
                ['Assets', 100, 90, 0, 0],
                ['Cash', 5, 3, 0, 0]]                      # Mar/Apr wholly unreported
        self.assertEqual(_kept_cols(rows, cols), [1, 2])   # Mar, Apr dropped

    def test_only_the_trailing_run_is_dropped_intermediate_zero_kept(self):
        cols = _months(4)
        rows = [['hdr', 'm1', 'm2', 'm3', 'm4'],
                ['Cash', 5, 0, 3, 0]]                      # Feb=0 (intermediate) stays; only Apr drops
        self.assertEqual(_kept_cols(rows, cols), [1, 2, 3])  # last reported is Mar → Apr dropped


class MustNotMisfire(SimpleTestCase):
    def test_genuine_latest_zero_cash_is_kept_when_its_column_is_populated(self):
        cols = _months(3)
        rows = [['hdr', 'm1', 'm2', 'm3'],
                ['Assets', 100, 90, 80],                   # Mar column IS reported (Assets 80)
                ['Cash', 5, 3, 0]]                         # cash genuinely 0 at latest → MUST stay
        self.assertEqual(_kept_cols(rows, cols), [1, 2, 3])

    def test_no_trailing_zeros_unchanged(self):
        cols = _months(3)
        rows = [['hdr', 'm1', 'm2', 'm3'], ['Cash', 5, 3, 7]]
        self.assertEqual(_kept_cols(rows, cols), [1, 2, 3])

    def test_all_unreported_falls_back_never_empty(self):
        cols = _months(3)
        rows = [['hdr', 'm1', 'm2', 'm3'], ['Cash', 0, 0, 0]]
        self.assertEqual(_kept_cols(rows, cols), [1, 2, 3])   # input returned, not []

    def test_total_column_is_never_dropped(self):
        cols = _months(2) + [PeriodColumn(3, 'Total', TOTAL, 12, (9999, 12))]
        rows = [['hdr', 'm1', 'm2', 'Total'],
                ['Cash', 5, 0, 0]]                          # Feb month unreported, Total 0
        kept = _kept_cols(rows, cols)
        self.assertIn(3, kept)                              # TOTAL stays regardless
