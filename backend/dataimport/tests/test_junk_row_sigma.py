"""Reddening control for the Σ totals-row double-counting bug.

The consolidated workbook marks its totals rows with the Greek sum symbol
"Σ" (LP_REGISTER "Σ (hole-aware)", PORTFOLIO_MASTER / VALUATIONS "Σ",
CAPITAL_CALLS "Σ calls = stated total"). Before the fix these passed the
junk-row filter and were imported as a phantom LP / company, doubling every
Σ-based metric (committed 1000→2000, invested 448→896, MOIC 2.0x→1.01x).

is_junk_row must treat a first cell that IS "Σ" (alone, or Σ followed by a
non-word char) as a totals row, while never touching a real name that merely
starts with the word "Sigma".
"""
from dataimport.phase6_extractor.helpers import is_junk_row


def test_sigma_totals_rows_are_junk():
    # exactly the rows the real TFAI.xlsx emits — each was wrongly imported before
    assert is_junk_row(('Σ',)) is True
    assert is_junk_row(('Σ', 448)) is True                       # PORTFOLIO_MASTER / VALUATIONS
    assert is_junk_row(('Σ (hole-aware)', 1000, 600, 70)) is True  # LP_REGISTER
    assert is_junk_row(('Σ (hole-aware) (Individual)',)) is True
    assert is_junk_row(('Σ calls = stated total', '600')) is True  # CAPITAL_CALLS check row
    assert is_junk_row(('Σ exit proceeds = stated total', '81')) is True


def test_real_entities_are_not_junked():
    # the reddening control's other half — no false positives on real data rows
    assert is_junk_row(('Aditya Pension Trust', 'Pension', 180)) is False
    assert is_junk_row(('Agnikul Cosmos', 'Space / Deep-tech', 65)) is False
    # a real company whose NAME starts with the word "Sigma" must survive
    assert is_junk_row(('Sigma Technologies Pvt Ltd', 12)) is False
    assert is_junk_row(('Signal Ventures', 5)) is False


def test_existing_total_markers_still_junk():
    # guard the pre-existing behaviour we extended, not replaced
    assert is_junk_row(('Total',)) is True
    assert is_junk_row(('Grand Total',)) is True
    assert is_junk_row(('Subtotal',)) is True
