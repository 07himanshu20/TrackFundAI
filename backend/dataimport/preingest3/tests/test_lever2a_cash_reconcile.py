"""Lever 2a — reconciliation-guided cash rebind (extract._reconciled_cash_rebind).

A cash STOCK held as "wrong row / implausibly small" is often the tiny bank-only sub-line that
out-scored the true TOTAL on label keywords alone — and the label can LIE ('Closing Cash Balance
including Fixed deposits' on a value that EXCLUDES them). Names propose the candidate rows; TWO
INDEPENDENT identities on the SAME statement & column dispose:
    • roll-forward   opening_cash + net change in cash = closing
    • component Σ     Σ(the contiguous component rows above a total) = that total

DISCIPLINE (anti-overfit): the cases below are PRINCIPLE-based synthetic statements, not the CPC
cells — must-handle (both identities agree → rebind to the confirmed total) AND must-NOT-misfire
(exactly one identity, disagreeing identities, or a broken subtotal → NO rebind, the hold stays).
The ≥2-agreeing-anchors requirement is the reddening spec: a single anchor must NEVER rebind, so a
relaxation to one anchor is caught by test_only_subtotal_* / test_only_rollforward_*. RealBugShape
locks the exact CPC labels/values that produced the original wrong-row hold.
"""
from decimal import Decimal

from django.test import SimpleTestCase

from dataimport.preingest3.extract import _reconciled_cash_rebind
from dataimport.preingest3.periods import PeriodColumn, MONTH

# three monthly point-in-time columns; the mechanism must resolve on the LATEST (May, col 3)
ACTS = [PeriodColumn(1, 'Mar-24', MONTH, 1, (2024, 3)),
        PeriodColumn(2, 'Apr-24', MONTH, 1, (2024, 4)),
        PeriodColumn(3, 'May-24', MONTH, 1, (2024, 5))]
ASOF = (2024, 5)


def _rows(net=(10, 12, 20), opening=(50, 60, 66), closing=(60, 72, 86),
          bank=(1, 2, 2), fd=(58, 70, 84), total=(59, 72, 86),
          drop=()):  # drop: row keys to omit (simulate a missing part)
    """A CPC-shaped cash-flow statement. Defaults reconcile: opening+net=closing (66+20=86) AND
    bank+fd=total (2+84=86). Override a tuple to break an identity; `drop` removes a row entirely."""
    spec = [
        ('header', ['Cash Flow Statement', None, None, None]),
        ('net', ['Net Increase in Cash & Cash Equivalents [A+B+C]', *net]),
        ('opening', ['Cash & Cash Equivalents - Opening Balance', *opening]),
        ('closing', ['Cash & Cash Equivalents - Closing Balance', *closing]),
        ('subhdr', ['Cash and Bank Balance as on 31.05.2024', None, None, None]),
        ('bank', ['Closing Cash Balance including Fixed deposits', *bank]),
        ('fd', ['Fixed Deposits', *fd]),
        ('total', ['TOTAL CASH AND CASH EQUIVALENT', *total]),
    ]
    return [r for k, r in spec if k not in drop]


class MustHandle(SimpleTestCase):
    def test_both_identities_agree_rebinds_to_the_confirmed_total(self):
        rb = _reconciled_cash_rebind(_rows(), 0, ACTS, ASOF, False)
        self.assertIsNotNone(rb)
        row, col = rb
        self.assertEqual(_rows()[row][0], 'TOTAL CASH AND CASH EQUIVALENT')
        self.assertEqual(Decimal(str(col.value)), Decimal('86'))
        self.assertEqual(col.source_cols, [3])                # resolved on the latest month

    def test_resolves_on_latest_period_not_an_earlier_one(self):
        # earlier months also reconcile (Mar 50+10=60, 1+58=59≈… ) but the answer must be May's 86
        row, col = _reconciled_cash_rebind(_rows(), 0, ACTS, ASOF, False)
        self.assertEqual(Decimal(str(col.value)), Decimal('86'))


class MustNotMisfire(SimpleTestCase):
    def test_only_subtotal_present_no_rollforward_holds(self):
        # remove BOTH roll-forward inputs → only the component subtotal remains → must NOT rebind
        self.assertIsNone(_reconciled_cash_rebind(_rows(drop=('net', 'opening')), 0, ACTS, ASOF, False))

    def test_only_rollforward_present_no_subtotal_holds(self):
        # remove the two component lines → only the roll-forward remains → must NOT rebind
        self.assertIsNone(_reconciled_cash_rebind(_rows(drop=('bank', 'fd')), 0, ACTS, ASOF, False))

    def test_rollforward_and_subtotal_disagree_holds(self):
        # subtotal ties to 86, but opening+net = 79+20 = 99 ≠ 86 → identities disagree → hold
        self.assertIsNone(_reconciled_cash_rebind(_rows(opening=(50, 60, 79)), 0, ACTS, ASOF, False))

    def test_components_do_not_sum_to_total_holds(self):
        # total says 200 but bank+fd = 86 → subtotal broken (and roll-forward ties to 86 ≠ 200) → hold
        self.assertIsNone(_reconciled_cash_rebind(_rows(total=(59, 72, 200)), 0, ACTS, ASOF, False))

    def test_no_reconciliation_structure_holds(self):
        # a bare balance-sheet cash line with no opening/net/component structure → hold
        rows = [['Assets', None, None, None],
                ['Cash & Bank balances', 5, 6, 8],
                ['Total', 100, 110, 120]]
        self.assertIsNone(_reconciled_cash_rebind(rows, 0, ACTS, ASOF, False))

    def test_single_component_is_not_a_subtotal(self):
        # one component above the total is not evidence of a sum → hold (needs ≥2 parts)
        self.assertIsNone(_reconciled_cash_rebind(_rows(drop=('bank',)), 0, ACTS, ASOF, False))


class RealBugShape(SimpleTestCase):
    """The exact CPC 'CFS EL' rows that produced the wrong-row hold (values in Mn, May-25 column)."""

    def test_cpc_cfs_rebinds_to_total_cash_and_cash_equivalent(self):
        rows = [
            ['Cash Flow Statement for the period ended 31.05.2025', None, None, None],
            ['Net Increase in Cash & Cash Equivalents [A+B+C]', 23.662514348743066, -29.75668012999988, 8.492238920000048],
            ['Cash & Cash Equivalents -Opening Balance', 62.67061769000001, 92.42729782, 92.42729782],
            ['Cash & Cash Equivalents -Closing Balance', 86.33313203874307, 62.67061769000013, 100.91953674000005],
            ['CASH AND BANK BALANCE AS ON 31/03/2025', None, None, None],
            ['Closing Cash Balance including Fixed deposits', 2.2289604700000027, 0.2578951000000029, 0.48384015],
            ['Fixed Deposits', 84.10309059000001, 62.41272259, 100.43569659000003],
            ['TOTAL CASH AND CASH EQUIVALENT', 86.33205106000001, 62.67061769000001, 100.91953674000004],
        ]
        acts = [PeriodColumn(1, 'May 2025', MONTH, 1, (2025, 5)),
                PeriodColumn(2, 'Dec 2024', MONTH, 1, (2024, 12)),
                PeriodColumn(3, 'May 2024', MONTH, 1, (2024, 5))]
        rb = _reconciled_cash_rebind(rows, 0, acts, (2025, 5), False)
        self.assertIsNotNone(rb)
        row, col = rb
        self.assertEqual(rows[row][0], 'TOTAL CASH AND CASH EQUIVALENT')
        self.assertEqual(col.source_cols, [1])                          # May-2025 column
        self.assertAlmostEqual(float(col.value), 86.33205106, places=4)  # → ₹8.6332 Cr after mn scale
