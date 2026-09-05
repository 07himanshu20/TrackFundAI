"""Operating-revenue DISPOSITION (UNIVERSAL, structure-keyed — keyed on label semantics + sibling rows,
never a file name). A 'Total Income'/'Total Revenue' AGGREGATE folds Revenue-from-operations together
with non-operating Other Income (the Ind-AS identity), so emitting the aggregate AS revenue is a silent
relabel. Three dispositions:
  • relocate — aggregate WITH a stated 'Revenue from operations' row  → emit that row (clean, citable).
  • hold     — aggregate, NO stated RfO row, but an 'Other Income' row IS present → operating revenue is
               a not-yet-citable subtraction (TI − OI) → HOLD (fail-closed; never a silent relabel).
  • keep     — not an aggregate, or an aggregate with no Other-Income sibling (a Σ of pure operating
               lines) → emit the located row.
This is the semantic half of never-a-wrong-number: even a perfectly CITABLE number (Total Income is a
real cell) must not ship under the WRONG concept label."""
from backend.dataimport.preingest3 import extract


# located revenue = 'Total revenue' aggregate, with a directly-stated operating-revenue row above it
AGG_WITH_RFO = [
    ['Particulars', 'Apr-2025', 'May-2025'],
    ['Revenue from operations', 100, 200],
    ['Other income', 5, 6],
    ['Total revenue', 105, 206],
]
# aggregate + Other Income, NO operating-revenue row → HOLD (Agnikul-class: the aggregate folds in
# non-operating income and operating revenue = TI − OI cannot be cited)
AGG_OTHER_NO_RFO = [
    ['Particulars', 'Apr-2025', 'May-2025'],
    ['Other income', 5, 6],
    ['Total Income', 105, 206],
]
# aggregate, NO operating-revenue row, NO Other-Income sibling → KEEP (Aliste-class: 'Total Revenue' is a
# Σ of pure operating sub-lines; no evidence of contamination → emit)
AGG_NO_OTHER = [
    ['Particulars', 'Apr-2025', 'May-2025'],
    ['Product sales', 60, 70],
    ['Service revenue', 40, 60],
    ['Total Revenue', 100, 130],
]
# aggregate whose only income sibling is 'Other OPERATING Income' (which IS operating revenue, NOT the
# non-operating 'Other Income' hold-signal) + no RfO → KEEP/emit. Guards the substring edge: 'other …
# income' must not over-match an operating line.
AGG_OTHER_OPERATING = [
    ['Particulars', 'Apr-2025', 'May-2025'],
    ['Other Operating Income', 60, 70],
    ['Total Revenue', 100, 130],
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
    assert extract._operating_revenue_row(AGG_OTHER_NO_RFO, 0, 2) is None
    assert extract._operating_revenue_row(ALREADY_RFO, 0, 1) is None


# ── _operating_revenue_disposition (the universal three-way decision) ─────────────────────────────
def test_disposition_relocate_when_rfo_present():
    assert extract._operating_revenue_disposition(AGG_WITH_RFO, 0, 3) == ('relocate', 1)


def test_disposition_HOLDS_aggregate_with_other_income_and_no_rfo():
    # THE Agnikul-class reddening control: an aggregate that folds in non-operating income, with no
    # stated operating-revenue row, must HOLD — never emit Total Income as revenue (silent relabel).
    disp, reason = extract._operating_revenue_disposition(AGG_OTHER_NO_RFO, 0, 2)
    assert disp == 'hold', 'aggregate + Other Income + no RfO must HOLD, not emit the aggregate'
    assert reason and 'operating revenue' in reason and 'Other Income' in reason


def test_disposition_keeps_aggregate_with_no_other_income():
    # Aliste-class: 'Total Revenue' = Σ of pure operating sub-lines, no Other-Income sibling → emit.
    assert extract._operating_revenue_disposition(AGG_NO_OTHER, 0, 3) == ('keep', None)


def test_disposition_other_operating_income_is_not_the_hold_signal():
    # NEGATIVE CONTROL (universality edge): 'Other Operating Income' is OPERATING revenue, not the
    # non-operating 'Other Income' signal — an aggregate with it as the only income sibling and no RfO
    # must KEEP/emit, NEVER hold. Proves the discriminator doesn't over-match 'other … income'.
    assert extract._other_income_present(AGG_OTHER_OPERATING, 0, 2) is False
    assert extract._operating_revenue_disposition(AGG_OTHER_OPERATING, 0, 2) == ('keep', None)


def test_disposition_keeps_non_aggregate_row():
    rows = [['P', 'Apr-2025', 'May-2025'], ['Sales', 100, 200]]      # plain operating row
    assert extract._operating_revenue_disposition(rows, 0, 1) == ('keep', None)
    assert extract._operating_revenue_disposition(ALREADY_RFO, 0, 1) == ('keep', None)


def test_disposition_keeps_when_row_is_none():
    # revenue not located at all → no disposition to make.
    assert extract._operating_revenue_disposition(AGG_OTHER_NO_RFO, 0, None) == ('keep', None)
