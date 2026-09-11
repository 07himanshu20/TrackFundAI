"""Bug fix: a month name inside a CUMULATIVE/QUARTER frame ('CY 2024 Act May', 'For QE 30th June')
was mis-classified as a discrete MONTH, taking the same (year,month) order as the true monthly column
and colliding with it → a phantom 'unlabeled plan/actual overlap' hold. Two defects, two fixes:

  Fix A — parse_period_label reads granularity from the strongest FRAME marker, not the mere presence
          of a month name: CY/FY/calendar-year/YTD/cumulative → YTD; QE/quarter-end → QUARTER.
  Fix B — _conflicting_periods is KIND-AWARE: only SAME-kind columns at the same order can be rival
          actuals; a month vs a quarter/YTD at the same order is different granularity, not a conflict.

DISCIPLINE: principle-based synthetic cases (must-handle + must-not-misfire), plus the two REAL bug
labels. Reddening: 'cross-kind is not a conflict' and 'framed month is not a discrete month' both FAIL
against the pre-fix engine (which grouped conflicts by order alone and matched the bare month).
"""
from decimal import Decimal

from django.test import SimpleTestCase

from dataimport.preingest3.periods import (
    parse_period_label, _conflicting_periods, collapse, PeriodColumn,
    MONTH, QUARTER, YTD, ACTUAL,
)
from dataimport.preingest3.quantity import FLOW


class FrameClassificationTest(SimpleTestCase):
    def test_cumulative_frame_month_is_ytd_not_discrete_month(self):
        pc = parse_period_label('CY 2024 Act May')
        self.assertEqual(pc.kind, YTD)
        self.assertNotEqual(pc.order, (2024, 5))   # must NOT take the real May-24 order

    def test_cy_budget_month_is_ytd(self):
        self.assertEqual(parse_period_label('CY 2025 BUD May').kind, YTD)

    def test_quarter_end_month_is_quarter(self):
        pc = parse_period_label('For QE 30th June, 2025')
        self.assertEqual(pc.kind, QUARTER)
        self.assertEqual(pc.order, (2025, 6))

    def test_quarter_ended_phrase_is_quarter(self):
        self.assertEqual(parse_period_label('Quarter ended March 2025').kind, QUARTER)

    def test_plain_month_unchanged(self):
        pc = parse_period_label('May-24 Actual')
        self.assertEqual(pc.kind, MONTH)
        self.assertEqual(pc.order, (2024, 5))

    def test_plain_bare_month_unchanged(self):
        self.assertEqual(parse_period_label('May-25').kind, MONTH)


class KindAwareConflictTest(SimpleTestCase):
    def _c(self, col, kind, order, basis=''):
        return PeriodColumn(col, f'p{col}', kind, 1, order, basis)

    def test_month_vs_ytd_same_order_is_not_a_conflict(self):
        cols = [self._c(1, MONTH, (2024, 5)), self._c(2, YTD, (2024, 5))]
        self.assertEqual(_conflicting_periods(cols, {1: 100, 2: 250}), set())

    def test_month_vs_quarter_same_order_is_not_a_conflict(self):
        cols = [self._c(1, MONTH, (2025, 6)), self._c(2, QUARTER, (2025, 6))]
        self.assertEqual(_conflicting_periods(cols, {1: 100, 2: 300}), set())

    def test_two_months_same_order_is_still_a_conflict(self):
        cols = [self._c(1, MONTH, (2025, 5)), self._c(2, MONTH, (2025, 5))]
        self.assertEqual(_conflicting_periods(cols, {1: 100, 2: 80}), {(MONTH, (2025, 5))})


class RealBugLabelsTest(SimpleTestCase):
    """The exact Analisa revenue shape that produced the residual hold."""

    def test_cy_cumulative_no_longer_collides_with_the_real_month(self):
        a = parse_period_label('May-24 Actual'); a.col = 1        # MONTH (2024,5)
        b = parse_period_label('CY 2024 Act May'); b.col = 2      # was MONTH (2024,5) → now YTD
        self.assertEqual(_conflicting_periods([a, b], {1: 1139425.8, 2: 2361325.94}), set())

    def test_month_and_cumulative_both_actual_no_phantom_overlap_hold(self):
        a = parse_period_label('May-24 Actual'); a.col = 1; a.basis = ACTUAL
        b = parse_period_label('CY 2024 Act May'); b.col = 2; b.basis = ACTUAL
        r = collapse('revenue', FLOW, [a, b], {1: 1139425.8, 2: 2361325.94})
        self.assertNotIn('unlabeled_period_overlap', (r.flags or []))
