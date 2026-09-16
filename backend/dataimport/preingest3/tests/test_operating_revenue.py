"""Operating-revenue DISPOSITION (UNIVERSAL, structure-keyed — keyed on label semantics + sibling rows,
never a file name). A 'Total Income'/'Total Revenue' AGGREGATE folds Revenue-from-operations together
with non-operating Other Income (the Ind-AS identity), so emitting the aggregate AS revenue is a silent
relabel. Dispositions:
  • relocate  — aggregate WITH a stated 'Revenue from operations' row  → emit that row (clean, citable).
  • keep      — an aggregate PROVEN pure operating revenue, or a plain (non-aggregate) row → emit.
  • construct — (Lever 5) aggregate folding in non-op income, BUT whose leaf components are COMPLETE
                (Σ == total, every column) → emit Σ(operating leaves) = total − non-op (clean operating
                revenue, cited as a construction). The accounting identity, only where fully citable.
  • hold      — anything else (the DEFAULT for an aggregate with no RfO row that cannot be constructed).

THE INVARIANT (closes the fail-open class): an aggregate with no RfO DEFAULTS TO HOLD; `keep` is reachable
ONLY through the positive purity proof `_aggregate_is_purely_operating`, which requires, in EVERY summed
column: (a) total ≤ Σ(components) [no unlabelled line folded in — DIRECTIONAL, a total that EXCLUDES an
operating line is fine], (b) every non-op component ≈0, (c) a real operating component carries value.
EVERY gap → HOLD: acts absent, components unreadable, proof False, proof raises. One vocabulary
(_NONOP_INCOME_RE) is used by both the proof and the hold-reason, so detection and proof cannot diverge.

This is the semantic half of never-a-wrong-number: even a perfectly CITABLE number (Total Income is a
real cell) must not ship under the WRONG concept label — and it must never emit merely because we could
not check it (the old fail-open, when the finder call site passed no columns)."""
from backend.dataimport.preingest3 import extract
from backend.dataimport.preingest3.periods import PeriodColumn, MONTH

# two data columns at grid indices 1 and 2 (the fixtures put period values there)
ACTS = [PeriodColumn(1, 'c1', MONTH, 1, (2025, 1)), PeriodColumn(2, 'c2', MONTH, 1, (2025, 2))]


# located revenue = 'Total revenue' aggregate, with a directly-stated operating-revenue row above it
AGG_WITH_RFO = [
    ['Particulars', 'Apr-2025', 'May-2025'],
    ['Revenue from operations', 100, 200],
    ['Other income', 5, 6],
    ['Total revenue', 105, 206],
]
# aggregate + non-op Other Income (non-zero), NO operating-revenue row, but the leaf components are
# COMPLETE (Σ == total every column) → CONSTRUCT (Lever 5): clean operating = Σ(operating leaves) =
# total − Other Income = Product. InstaAstro-class. (Pre-Lever-5 this HELD; the construction is the
# intended upgrade — a citable operating figure from citable leaves, not a manufactured number.)
AGG_OTHER_NO_RFO = [
    ['Particulars', 'Apr-2025', 'May-2025'],
    ['Product', 100, 200],
    ['Other income', 5, 6],
    ['Total Income', 105, 206],
]
# aggregate + non-op + an UNLABELLED EXCESS (total > Σ components) → CANNOT construct cleanly (a hidden
# line could be non-operating) → HOLD. This is the Agnikul-class fail-closed: construction requires the
# components to reconcile to the total; they don't here.
AGG_OTHER_EXCESS = [
    ['Particulars', 'Apr-2025', 'May-2025'],
    ['Product', 100, 200],
    ['Other income', 5, 6],
    ['Total Income', 130, 250],
]
# aggregate whose ONLY components are non-operating (interest + dividend) → no operating leaf carries
# value → construction has nothing to build → HOLD.
AGG_ALL_NONOP = [
    ['Particulars', 'Apr-2025', 'May-2025'],
    ['Interest income', 60, 70],
    ['Dividend income', 40, 60],
    ['Total Income', 100, 130],
]
# aggregate, NO operating-revenue row, all-operating components that reconcile → KEEP (Aliste-class:
# 'Total Revenue' is a Σ of pure operating sub-lines; no non-op → emit)
AGG_NO_OTHER = [
    ['Particulars', 'Apr-2025', 'May-2025'],
    ['Product sales', 60, 70],
    ['Service revenue', 40, 60],
    ['Total Revenue', 100, 130],
]
# aggregate whose only non-product income sibling is 'Other OPERATING Income' (which IS operating
# revenue, NOT the non-operating 'Other Income' hold-signal); components reconcile → KEEP. Guards the
# substring edge: 'other … income' must not over-match an operating line.
AGG_OTHER_OPERATING = [
    ['Particulars', 'Apr-2025', 'May-2025'],
    ['Other Operating Income', 60, 70],
    ['Product sales', 40, 60],
    ['Total Revenue', 100, 130],
]
# aggregate whose total EXCEEDS the Σ of its stated components → an unlabelled line is folded in (could
# be non-operating) → HOLD (Case C: the directional reconciliation leg, which a bare label-scan misses).
AGG_EXCESS = [
    ['Particulars', 'Apr-2025', 'May-2025'],
    ['Product', 100, 120],
    ['Total Revenue', 130, 150],
]
# aggregate whose total is LESS than Σ(components) because a real OPERATING line ('Installation') is kept
# OUT of the subtotal (the Aliste shape, measured: Total = Σ(subscription lines), Installation excluded).
# total < Σ is NOT contamination → KEEP. A two-sided '==' reconciliation would wrongly HOLD this and
# regress Aliste's verified ₹5.3052 Cr emit — this control reddens if the '≤' is reverted to '=='.
AGG_EXCLUDES_OP = [
    ['Particulars', 'Apr-2025', 'May-2025'],
    ['Subscriptions', 100, 120],
    ['Installation', 40, 50],
    ['Total Revenue', 100, 120],
]
# the located row is ALREADY operating revenue → KEEP (not an aggregate for this purpose)
ALREADY_RFO = [
    ['Particulars', 'Apr-2025', 'May-2025'],
    ['Revenue from operations', 100, 200],
]


# ── _operating_revenue_row (the relocate primitive) ──────────────────────────────────────────────
def test_relocates_aggregate_to_operating_revenue_row():
    assert extract._operating_revenue_row(AGG_WITH_RFO, 0, 3) == 1     # located 'Total revenue' → RfO row 1


def test_row_primitive_none_without_rfo():
    # the relocate primitive returns None whenever there is no stated RfO row (the HOLD decision is the
    # disposition's job, below) — it must NEVER fabricate a relocation target.
    assert extract._operating_revenue_row(AGG_OTHER_NO_RFO, 0, 3) is None
    assert extract._operating_revenue_row(ALREADY_RFO, 0, 1) is None


# ── _operating_revenue_disposition (the universal decision) ───────────────────────────────────────
def test_disposition_relocate_when_rfo_present():
    assert extract._operating_revenue_disposition(AGG_WITH_RFO, 0, 3, acts=ACTS) == ('relocate', 1)


def test_disposition_CONSTRUCTS_clean_operating_from_reconciling_aggregate():
    # LEVER 5 (InstaAstro-class): an aggregate that folds in non-operating income (non-zero), no RfO row,
    # but whose leaf components are COMPLETE (Σ == total every column) → CONSTRUCT clean operating revenue
    # = Σ(operating leaves) = total − Other Income. The operating leaf is 'Product' (row 1); 'Other income'
    # (row 2) is stripped. NOT a hold: the figure is citable from citable leaves. (Reddening: neutralizing
    # the construction reverts this to the pre-Lever-5 HOLD.)
    assert extract._operating_revenue_disposition(AGG_OTHER_NO_RFO, 0, 3, acts=ACTS) == ('construct', [1])


def test_disposition_HOLDS_when_total_exceeds_components_even_with_non_op():
    # THE Agnikul-class fail-closed: an aggregate folding in non-op AND with an unlabelled excess
    # (total > Σ components) CANNOT be cleanly constructed (a hidden line could be non-operating) → HOLD,
    # never a manufactured operating figure. The hold-reason discloses the non-op contamination.
    disp, reason = extract._operating_revenue_disposition(AGG_OTHER_EXCESS, 0, 3, acts=ACTS)
    assert disp == 'hold', 'unlabelled excess → components do not reconcile → cannot construct → HOLD'
    assert reason and 'operating revenue' in reason.lower()


def test_disposition_HOLDS_all_non_operating_aggregate():
    # no operating leaf carries value (interest + dividend only) → construction has nothing to build → HOLD.
    assert extract._operating_revenue_disposition(AGG_ALL_NONOP, 0, 3, acts=ACTS)[0] == 'hold'


def test_disposition_HOLDS_when_acts_absent():
    # #2 invariant (the fail-open this fix closes): an aggregate with no RfO DEFAULTS to HOLD; `keep` is
    # reachable ONLY through the purity proof, which needs period columns. Absent acts (the finder call
    # site used to pass none), the aggregate CANNOT be proven pure → fail-closed HOLD, never a blind keep
    # — even for a clean-looking aggregate whose components would reconcile if we could see the columns.
    disp, reason = extract._operating_revenue_disposition(AGG_NO_OTHER, 0, 3)      # no acts
    assert disp == 'hold', 'no columns to prove purity → must HOLD (was fail-open keep before the fix)'
    assert reason


def test_disposition_HOLDS_when_total_exceeds_components():
    # Case C: total > Σ(visible components) → an unlabelled line is folded in (could be non-operating) →
    # HOLD, even with acts present. A pure label-scan for 'Other Income' would MISS this; the directional
    # reconciliation leg catches it.
    assert extract._operating_revenue_disposition(AGG_EXCESS, 0, 2, acts=ACTS)[0] == 'hold'


def test_disposition_keeps_aggregate_with_no_other_income():
    # Aliste-class: 'Total Revenue' = Σ of pure operating sub-lines, no non-op → emit (with columns).
    assert extract._operating_revenue_disposition(AGG_NO_OTHER, 0, 3, acts=ACTS) == ('keep', None)


def test_disposition_keeps_aggregate_that_excludes_an_operating_line():
    # THE Aliste must-handle: the total is LESS than Σ(components) because an operating line
    # ('Installation') is excluded from the subtotal. total < Σ is not contamination → KEEP. Reverting
    # the reconciliation to a two-sided '==' would falsely HOLD this (RED) and lose a verified emit.
    assert extract._operating_revenue_disposition(AGG_EXCLUDES_OP, 0, 3, acts=ACTS) == ('keep', None)


def test_disposition_other_operating_income_is_not_the_hold_signal():
    # NEGATIVE CONTROL (universality edge): 'Other Operating Income' is OPERATING revenue, not the
    # non-operating 'Other Income' signal. Re-verified directly against the SINGLE shared vocabulary
    # (_NONOP_INCOME_RE must not over-match it), then end-to-end: a reconciling aggregate whose only
    # 'other … income' line is operating must KEEP, never hold.
    assert extract._NONOP_INCOME_RE.search('Other Operating Income') is None
    assert extract._operating_revenue_disposition(AGG_OTHER_OPERATING, 0, 3, acts=ACTS) == ('keep', None)


def test_disposition_keeps_non_aggregate_row():
    rows = [['P', 'Apr-2025', 'May-2025'], ['Sales', 100, 200]]      # plain operating row (not aggregate)
    assert extract._operating_revenue_disposition(rows, 0, 1, acts=ACTS) == ('keep', None)
    assert extract._operating_revenue_disposition(ALREADY_RFO, 0, 1, acts=ACTS) == ('keep', None)


def test_disposition_keeps_when_row_is_none():
    # revenue not located at all → no disposition to make (not an aggregate).
    assert extract._operating_revenue_disposition(AGG_OTHER_NO_RFO, 0, None, acts=ACTS) == ('keep', None)


# ══ BAR 3 — SYNTHETIC-ADVERSARIAL GENERALIZATION (Lever 5 clean-operating construction) ══════════════
# Byte-DISTINCT statements that vary the incidentals the construction must be INVARIANT to (row order,
# label wording/synonyms, scale, decoy ratio rows) while preserving the one structural signal: a
# non-operating leaf inside a COMPLETE, reconciling component set. Every POSITIVE must construct the
# correct operating leaves; every look-alike NEGATIVE must HOLD. Generalization beyond the corpus shapes.

def _disp(rows, total_row):
    return extract._operating_revenue_disposition(rows, 0, total_row, acts=ACTS)


def test_bar3_construct_operating_below_nonop_with_synonyms():
    # row order varied (operating AFTER non-op), synonym labels → construct the operating leaf (row 2).
    rows = [['Particulars', 'Apr-2025', 'May-2025'],
            ['Interest income', 5, 6],                    # non-op, listed first
            ['Consulting fees', 100, 200],               # operating (synonym)
            ['Total Income', 105, 206]]
    assert _disp(rows, 3) == ('construct', [2])


def test_bar3_construct_multi_operating_scaled_with_ratio_decoy():
    # scale ×1000, two operating leaves + one non-op, a ratio decoy interleaved (must be skipped by
    # _leaf_components) → construct BOTH operating leaves, excluding the ratio row and the non-op.
    rows = [['Particulars', 'Apr-2025', 'May-2025'],
            ['Product sales', 60000, 70000],             # operating (row 1)
            ['% of revenue', 0.6, 0.54],                 # ratio decoy → skipped
            ['Service revenue', 35000, 55000],           # operating (row 3)
            ['Dividend received', 5000, 5000],           # non-op (row 4)
            ['Total Revenue', 100000, 130000]]
    disp, op = _disp(rows, 5)
    assert disp == 'construct' and set(op) == {1, 3}


def test_bar3_construct_forex_gain_is_non_operating():
    # 'Foreign exchange gain' is non-operating (single-sourced vocab) → stripped; reconciling → construct.
    rows = [['Particulars', 'Apr-2025', 'May-2025'],
            ['Subscription revenue', 90, 120],
            ['Foreign exchange gain', 10, 10],
            ['Total Income', 100, 130]]
    assert _disp(rows, 3) == ('construct', [1])


def test_bar3_negative_unlabelled_excess_holds():
    # look-alike: total > Σ(components) → a hidden line (could be non-op) → HOLD, never construct.
    rows = [['Particulars', 'Apr-2025', 'May-2025'],
            ['Product', 100, 200],
            ['Interest income', 5, 6],
            ['Total Income', 140, 260]]
    assert _disp(rows, 3)[0] == 'hold'


def test_bar3_negative_total_below_sigma_with_nonop_holds():
    # total < Σ AND a non-op present → cannot tell which line the total excludes → ambiguous → HOLD.
    rows = [['Particulars', 'Apr-2025', 'May-2025'],
            ['Product', 100, 200],
            ['Interest income', 5, 6],
            ['Total Income', 90, 180]]
    assert _disp(rows, 3)[0] == 'hold'


def test_bar3_negative_all_non_operating_holds():
    rows = [['Particulars', 'Apr-2025', 'May-2025'],
            ['Interest income', 60, 70],
            ['Dividend income', 40, 60],
            ['Total Income', 100, 130]]
    assert _disp(rows, 3)[0] == 'hold'


def test_bar3_pure_operating_reconciling_is_keep_not_construct():
    # must-not-misfire on the keep/construct boundary: a COMPLETE all-operating set (no non-op) is proven
    # PURE → 'keep' the total, NOT 'construct' (construct is only for a non-op-contaminated complete set).
    rows = [['Particulars', 'Apr-2025', 'May-2025'],
            ['Product sales', 60, 70],
            ['Service revenue', 40, 60],
            ['Total Revenue', 100, 130]]
    assert _disp(rows, 3) == ('keep', None)
