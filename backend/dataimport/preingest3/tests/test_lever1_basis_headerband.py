"""Lever 1 (increment 1a) — header-band parsing: resolve a same-period value COLLISION using an
EXPLICIT reporting-basis label (Actual vs Budget/Forecast/Plan/Revised/Prior-year), instead of holding.

DISCIPLINE (anti-overfit): these tests assert the PRINCIPLE, never the 19 real cells. Every case is a
synthetic shape the skill must handle, plus the shapes it must NOT misfire on. The principle in one line:
"when two columns collide on the same period with different values, an explicit ACTUAL label is the
figure and Budget/Forecast/Plan/Revised/Prior-year yield to it; if the basis cannot make the actual
UNIQUE, do not guess — hold."

REDDENING: the must-handle cases (emit the actual) FAIL against the pre-Lever-1 engine, which held on
any collision — they prove the new skill. The must-NOT-misfire cases (still hold / unchanged) also pass
on the old engine — they prove conservatism and the byte-identical guarantee (the resolver only ever
touches a would-hold collision, never an already-emitted figure).
"""
from decimal import Decimal

from django.test import SimpleTestCase

from dataimport.preingest3.periods import (
    collapse, detect_period_axis, parse_basis, PeriodColumn,
    ACTUAL, BUDGET, FORECAST, PLAN, PRIOR_YEAR, VARIANCE, AMBIGUOUS, MONTH,
)
from dataimport.preingest3.quantity import STOCK, FLOW


def _c(col, order, basis='', kind=MONTH, label=None):
    return PeriodColumn(col, label or f'p{col}', kind, 1, order, basis)


class ParseBasisTest(SimpleTestCase):
    def test_recognises_each_basis_word(self):
        self.assertEqual(parse_basis('Actual'), ACTUAL)
        self.assertEqual(parse_basis('Actuals'), ACTUAL)
        self.assertEqual(parse_basis('Reported'), ACTUAL)
        self.assertEqual(parse_basis('Budget'), BUDGET)
        self.assertEqual(parse_basis('FY25 Budget'), BUDGET)
        self.assertEqual(parse_basis('Forecast'), FORECAST)
        self.assertEqual(parse_basis('Projected'), FORECAST)
        self.assertEqual(parse_basis('Plan'), PLAN)
        self.assertEqual(parse_basis('Prior Year'), PRIOR_YEAR)
        self.assertEqual(parse_basis('PY'), PRIOR_YEAR)
        self.assertEqual(parse_basis('MoM Growth'), VARIANCE)
        self.assertEqual(parse_basis('Variance %'), VARIANCE)

    def test_ambiguous_terms_are_recognised_but_never_auto_yield(self):
        # revised/restated/provisional/unaudited/management/proforma/normalised → AMBIGUOUS (hold, not yield)
        for t in ('Revised', 'Restated', 'Provisional', 'Unaudited', 'Management', 'Proforma', 'Normalised'):
            self.assertEqual(parse_basis(t), AMBIGUOUS, f'{t!r} should be AMBIGUOUS')

    def test_actual_wins_over_year_so_prior_year_actual_reads_actual(self):
        # 'Actual 2024' is an ACTUAL (its prior-year-ness is carried by the PERIOD, not the basis).
        self.assertEqual(parse_basis('Actual 2024'), ACTUAL)

    def test_plain_period_or_number_has_no_basis(self):
        for t in ('Feb-25', '2025', 'Jan', '', None, 1234, 'Cash and bank'):
            self.assertEqual(parse_basis(t), '', f'{t!r} should carry no basis')


class BasisResolutionMustHandleTest(SimpleTestCase):
    """The skill: an explicit basis label breaks the collision → emit the ACTUAL."""

    def test_stock_actual_vs_budget_emits_actual(self):
        cols = [_c(1, (2025, 2), ACTUAL), _c(2, (2025, 2), BUDGET)]
        r = collapse('cash', STOCK, cols, {1: 100, 2: 80})
        self.assertFalse(r.escalate)
        self.assertEqual(r.value, Decimal('100'))

    def test_actual_on_the_right_still_picked(self):
        # order-independent: actual is the higher column index here
        cols = [_c(1, (2025, 2), BUDGET), _c(2, (2025, 2), ACTUAL)]
        r = collapse('cash', STOCK, cols, {1: 80, 2: 100})
        self.assertFalse(r.escalate)
        self.assertEqual(r.value, Decimal('100'))

    def test_actual_against_multiple_non_actuals(self):
        cols = [_c(1, (2025, 2), ACTUAL), _c(2, (2025, 2), BUDGET),
                _c(3, (2025, 2), FORECAST), _c(4, (2025, 2), PRIOR_YEAR)]
        r = collapse('cash', STOCK, cols, {1: 100, 2: 80, 3: 90, 4: 70})
        self.assertFalse(r.escalate)
        self.assertEqual(r.value, Decimal('100'))

    def test_flow_actual_vs_budget_emits_actual_not_hold(self):
        cols = [_c(1, (2025, 2), ACTUAL), _c(2, (2025, 2), BUDGET)]
        r = collapse('revenue', FLOW, cols, {1: 100, 2: 80})
        self.assertFalse(r.escalate)
        self.assertEqual(r.value, Decimal('100'))


class BasisResolutionMustNotMisfireTest(SimpleTestCase):
    """Conservatism: if the basis cannot make the actual UNIQUE, keep holding — never guess."""

    def test_no_basis_labels_still_holds(self):
        # the existing fail-closed guard must remain intact (byte-identical behaviour)
        cols = [_c(1, (2025, 2)), _c(2, (2025, 2))]
        r = collapse('cash', STOCK, cols, {1: 100, 2: 80})
        self.assertTrue(r.escalate)
        self.assertIn('unlabeled_period_overlap', r.flags)

    def test_two_actuals_colliding_holds(self):
        cols = [_c(1, (2025, 2), ACTUAL), _c(2, (2025, 2), ACTUAL)]
        r = collapse('cash', STOCK, cols, {1: 100, 2: 90})
        self.assertTrue(r.escalate)

    def test_actual_plus_unknown_basis_holds(self):
        # an UNLABELED column in the collision means we cannot be sure → hold
        cols = [_c(1, (2025, 2), ACTUAL), _c(2, (2025, 2), '')]
        r = collapse('cash', STOCK, cols, {1: 100, 2: 90})
        self.assertTrue(r.escalate)

    def test_actual_plus_variance_holds(self):
        # a variance/% column is not a non-actual VALUE basis → do not resolve
        cols = [_c(1, (2025, 2), ACTUAL), _c(2, (2025, 2), VARIANCE)]
        r = collapse('cash', STOCK, cols, {1: 100, 2: 15})
        self.assertTrue(r.escalate)

    def test_actual_plus_ambiguous_revised_holds(self):
        # a revised/restated ACTUAL may be the truer figure → AMBIGUOUS must NOT auto-yield → hold
        cols = [_c(1, (2025, 2), ACTUAL), _c(2, (2025, 2), AMBIGUOUS)]
        r = collapse('cash', STOCK, cols, {1: 100, 2: 105})
        self.assertTrue(r.escalate)


class NonConflictUnchangedTest(SimpleTestCase):
    """A figure with no collision is emitted exactly as before — the resolver never runs on it."""

    def test_single_actual_column_emits(self):
        cols = [_c(1, (2025, 2), ACTUAL)]
        r = collapse('cash', STOCK, cols, {1: 100})
        self.assertFalse(r.escalate)
        self.assertEqual(r.value, Decimal('100'))

    def test_basis_present_but_different_periods_no_collision(self):
        # actual Feb + budget Jan are different periods → no collision → latest actual emitted
        cols = [_c(1, (2025, 1), BUDGET), _c(2, (2025, 2), ACTUAL)]
        r = collapse('cash', STOCK, cols, {1: 80, 2: 100})
        self.assertFalse(r.escalate)
        self.assertEqual(r.value, Decimal('100'))


class DetectAxisSubRowBasisTest(SimpleTestCase):
    """detect_period_axis reads the basis sub-row directly beneath the period row (increment 1a)."""

    def test_sub_row_actual_budget_populates_basis(self):
        rows = [
            [None, 'May-25', 'May-25'],   # period row
            [None, 'Actual', 'Budget'],   # basis sub-row
            ['EBITDA', 239848, -24956],
        ]
        ax = detect_period_axis(rows)
        by_col = {c.col: c.basis for c in ax.columns}
        self.assertEqual(by_col.get(1), ACTUAL)
        self.assertEqual(by_col.get(2), BUDGET)

    def test_second_period_row_is_not_mistaken_for_basis(self):
        rows = [
            [None, 'FY2025', 'FY2025'],
            [None, 'May-25', 'Jun-25'],   # another period row, no basis tokens
            ['Revenue', 10, 20],
        ]
        ax = detect_period_axis(rows)
        self.assertTrue(all(c.basis == '' for c in ax.columns))

    def test_basis_combined_in_period_cell_resolves_end_to_end(self):
        # the ProfitLoss(2) shape: period AND basis in ONE header cell ('May-25 Actual' / 'May-25 BUD')
        rows = [
            ['Total Revenue', None, None],
            ['Particulars', 'May-25 Actual', 'May-25 BUD'],   # combined period+basis header
            ['Sales', 1656081.38, 1904000],
        ]
        ax = detect_period_axis(rows)
        by_col = {c.col: c.basis for c in ax.columns}
        self.assertEqual(by_col.get(1), ACTUAL)
        self.assertEqual(by_col.get(2), BUDGET)
        # and the collision collapses to the actual
        vals = {1: 1656081.38, 2: 1904000}
        r = collapse('revenue', FLOW, ax.columns, vals)
        self.assertFalse(r.escalate)
        self.assertEqual(r.value, Decimal('1656081.38'))
