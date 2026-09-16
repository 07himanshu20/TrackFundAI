"""Lever 3 reddening — ① reconciliation-proof operating-revenue disposition, ② headcount count-type gate.

Each fix SMARTENS a guard behind a proof (never relaxes it), so each ships with must-handle AND the named
must-not-misfire controls, on principle-based synthetic grids (not the corpus cells):

① `_aggregate_is_purely_operating` / `_operating_revenue_disposition`: accept a 'Total Income/Revenue'
   aggregate as operating revenue ONLY when, in every column, (a) total ≤ Σ(components) [DIRECTIONAL — a
   total that EXCLUDES a visible operating line is fine; only a total that EXCEEDS its components has an
   unlabelled line folded in], (b) every non-op line (Other Income / interest / …) is ~0, and (c) a real
   operating component carries value. Every gap (no acts, no components, proof raises) → HOLD (default).
   MUST-HANDLE: a pure reconciling total, and an Aliste-class total that excludes an operating line (< Σ).
   The PURITY PROOF must return False for any contaminated total (a non-op line carrying value, Other
   Income non-zero in a NON-emit summed column, an excess/hidden line, an all-non-op total, no columns) —
   so the total is never emitted as-is. Lever 5 then adds `construct`: where a contaminated total's leaf
   components RECONCILE (Σ == total), the disposition builds clean operating = Σ(operating leaves); where
   they do NOT reconcile (excess/hidden line), or no operating leaf carries value, or no columns → HOLD.

② `_count_gate_hold`: a count (headcount) must be positive and of count magnitude — a currency/scale
   value (a monetary 'Staff Cost') can NEVER be a headcount. MUST-NOT-MISFIRE: ≤0 and monetary-magnitude
   values are rejected; a real count passes.
"""
from decimal import Decimal

from django.test import SimpleTestCase

from backend.dataimport.preingest3.extract import (
    _aggregate_is_purely_operating, _operating_revenue_disposition, _count_gate_hold)
from backend.dataimport.preingest3.periods import PeriodColumn, MONTH

# two data columns at grid indices 1 and 2
ACTS = [PeriodColumn(1, 'c1', MONTH, 1, (2025, 1)), PeriodColumn(2, 'c2', MONTH, 1, (2025, 2))]


def _grid(*rows):
    return list(rows)


class OperatingRevenueProof(SimpleTestCase):
    def test_pure_operating_total_passes(self):
        # products + zero Other Income, total == Σ in both columns → operating
        rows = _grid(['Revenue', None, None], ['Product A', 100, 120], ['Product B', 50, 60],
                     ['Other Income', 0, 0], ['Total Revenue', 150, 180])
        self.assertTrue(_aggregate_is_purely_operating(rows, 0, 4, ACTS))
        self.assertEqual(_operating_revenue_disposition(rows, 0, 4, acts=ACTS), ('keep', None))

    def test_interest_line_nonzero_total_not_pure_but_reconciles_constructs(self):
        # 'Interest on FDs' is non-op + non-zero → the TOTAL is NOT purely operating (proof False, so the
        # contaminated total is never emitted). But the leaf components RECONCILE to the total (130=100+30,
        # 160=120+40) → Lever 5 CONSTRUCTS clean operating = Σ(operating leaves) = Product A (row 1),
        # stripping the interest. (Pre-Lever-5 this HELD; the construction is the intended upgrade — the
        # clean operating figure is citable from citable leaves. Where components do NOT reconcile it HOLDS,
        # see test_total_exceeds_components_holds.)
        rows = _grid(['Income', None, None], ['Product A', 100, 120], ['Interest on FDs', 30, 40],
                     ['Total Income', 130, 160])
        self.assertFalse(_aggregate_is_purely_operating(rows, 0, 3, ACTS))     # total not emittable as-is
        self.assertEqual(_operating_revenue_disposition(rows, 0, 3, acts=ACTS), ('construct', [1]))

    def test_other_income_nonzero_in_a_nonemit_column_holds(self):
        # InstaAstro TTM shape: Other Income 0 in the max-total (emit) column but non-zero in the OTHER
        # summed column → the multi-column sum folds in non-op → must HOLD (single-column check would miss)
        rows = _grid(['Rev', None, None], ['Product', 100, 120], ['Other Income', 5, 0],
                     ['Total Revenue', 105, 120])
        self.assertFalse(_aggregate_is_purely_operating(rows, 0, 3, ACTS))

    def test_total_exceeds_components_holds(self):
        # total EXCEEDS Σ(found components) in a column → an unlabelled/hidden line is folded in (could be
        # non-operating) → not provably pure → hold (Case C, the directional reconciliation leg)
        rows = _grid(['Rev', None, None], ['Product', 100, 120], ['Total Revenue', 150, 180])
        self.assertFalse(_aggregate_is_purely_operating(rows, 0, 2, ACTS))

    def test_total_excludes_an_operating_line_is_still_pure(self):
        # Aliste MUST-HANDLE: 'Total Revenue' == Σ(operating SUBSET); an operating line ('Installation')
        # is EXCLUDED from the subtotal → total < Σ(all comps). Directional reconciliation (total ≤ Σ, not
        # a two-sided '==') recognises this as pure operating → True. A '==' proof would falsely reject it
        # and regress Aliste's verified ₹5.3052 Cr emit.
        rows = _grid(['Rev', None, None], ['Subscriptions', 100, 120], ['Installation', 40, 50],
                     ['Total Revenue', 100, 120])
        self.assertTrue(_aggregate_is_purely_operating(rows, 0, 3, ACTS))

    def test_cannot_prove_without_acts_holds(self):
        # #2 invariant leg: no period columns → the purity proof cannot reconcile → False (→ the
        # disposition defaults to HOLD). This is the fail-open the fix closes.
        rows = _grid(['Rev', None, None], ['Product', 100, 120], ['Total Revenue', 100, 120])
        self.assertFalse(_aggregate_is_purely_operating(rows, 0, 2, None))

    def test_all_nonop_total_holds(self):
        # no operating component at all (only Other Income) → never emit a total with no operating line
        rows = _grid(['Income', None, None], ['Other Income', 0, 0], ['Total Income', 0, 0])
        self.assertFalse(_aggregate_is_purely_operating(rows, 0, 2, ACTS))

    def test_non_aggregate_row_is_kept_untouched(self):
        # a row that is not a 'Total Income/Revenue' aggregate is never gated by this proof
        rows = _grid(['Revenue from operations', 100, 120])
        self.assertEqual(_operating_revenue_disposition(rows, 0, 0, acts=ACTS), ('keep', None))


class CountGate(SimpleTestCase):
    def test_real_count_passes(self):
        self.assertIsNone(_count_gate_hold('headcount', Decimal('111')))
        self.assertIsNone(_count_gate_hold('headcount', 148))

    def test_zero_or_negative_holds(self):
        self.assertIsNotNone(_count_gate_hold('headcount', 0))
        self.assertIsNotNone(_count_gate_hold('headcount', Decimal('-5')))
        self.assertIsNotNone(_count_gate_hold('headcount', None))

    def test_monetary_magnitude_is_not_a_headcount(self):
        # a 'Staff Cost' in raw rupees read as a count must be rejected (currency-scale ≠ person count)
        self.assertIsNotNone(_count_gate_hold('headcount', Decimal('2900000')))
        self.assertIsNotNone(_count_gate_hold('headcount', 5000000))
