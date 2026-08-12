"""Permanent regression fixture for TTM-window honesty (CF1).

A 'TTM' basis is only honest when the summed columns form ONE unbroken, distinct
12-month (or 4-quarter) window. Σ(months) ≥ 12 is NOT sufficient — it also passes
for a multi-year history, overlaps, or gaps, each carrying a WRONG-WINDOW figure
under a TTM tag. The canonical bug: InstaAstro's 57 consecutive months were summed
whole (−₹30 Cr) and mislabelled TTM/12m; the honest trailing-12 is ≈ −₹9 Cr.
"""
from decimal import Decimal

from backend.dataimport.preingest3 import periods
from backend.dataimport.preingest3.periods import PeriodColumn, MONTH, QUARTER, YTD, DISCRETE


def _months(spec):
    """spec = [(year, month), ...] → monthly PeriodColumns in given order."""
    return [PeriodColumn(col=i, label=f'{y}-{m:02d}', kind=MONTH, months=1, order=(y, m))
            for i, (y, m) in enumerate(spec)]


def _run(cols):
    return sorted(cols, key=lambda c: c.order)


def test_clean_12_is_ttm():
    s = _run(_months([(2025, m) for m in range(3, 13)] + [(2026, 1), (2026, 2)]))
    w = periods._true_ttm_window(s)
    assert w is not None and len(w) == 12


def test_57_months_takes_trailing_12_not_whole_history():
    spec = []
    for y in range(2021, 2026):
        for m in range(1, 13):
            spec.append((y, m))
    spec = spec[5:] + [(2026, 1), (2026, 2)]     # 57 consecutive months
    s = _run(_months(spec))
    w = periods._true_ttm_window(s)
    assert w is not None and len(w) == 12
    assert w[0].order == (2025, 3) and w[-1].order == (2026, 2)
    # _flow_from_series must sum ONLY the trailing window, not all 57
    vals = {c.col: Decimal('10') for c in s}     # each month = 10
    col = periods._flow_from_series('ebitda', lambda c: vals[c.col], s, DISCRETE)
    assert col.basis == 'TTM' and col.months == 12
    assert col.value == Decimal('120')           # 12×10, not 57×10
    assert 'trailing_12_of_more' in col.flags


def test_gappy_snapshots_are_not_summed_take_latest():
    # CPC: May-24, Dec-24, May-25 — 3 non-consecutive COMPARATIVE snapshots (a current
    # period + two prior-year comparatives). These must NOT be summed — doing so
    # produced a 3× false EBITDA (₹79.62 Cr vs the true ₹15.65 Cr May-25 figure). The
    # run collapses to the LATEST period only; the comparatives are dropped and flagged.
    s = _run(_months([(2024, 5), (2024, 12), (2025, 5)]))
    assert periods._true_ttm_window(s) is None
    col = periods._flow_from_series('rev', lambda c: Decimal('5'), s, DISCRETE)
    assert col.basis == 'partial' and col.months == 1
    assert col.value == Decimal('5')                      # latest column only, not 3×5=15
    assert any('non_consecutive' in f for f in col.flags)


def test_partial_months_plus_same_year_ytd_takes_ytd():
    # Agnikul: 2 recent months (Jan'26, Feb'26) + an explicit same-year 'YTD FY26'
    # column. Σmonths << YTD (partial display) → READ the YTD column (self-declared
    # basis), do not escalate. This is a legitimate resolver fix, not a guess.
    jan = PeriodColumn(col=0, label="Jan'26", kind=MONTH, months=1, order=(2026, 1))
    feb = PeriodColumn(col=1, label="Feb'26", kind=MONTH, months=1, order=(2026, 2))
    ytd = PeriodColumn(col=2, label='YTD FY26', kind=YTD, months=0, order=(2026, 12))
    vals = {0: Decimal('5.73'), 1: Decimal('4.89'), 2: Decimal('51.46')}
    c = periods.collapse('revenue', 'flow', [jan, feb, ytd], vals)
    assert not c.escalate and c.basis == 'YTD' and c.value == Decimal('51.46')
    assert 'partial_series_same_year_ytd_taken' in c.flags


def test_overlapping_multiyear_series_still_escalates():
    # Aliste-like: Σseries ≥ total (overlapping years/quarters) → the same-year-YTD
    # shortcut must NOT fire. Holding is CORRECT (ambiguous), not a resolver gap.
    m1 = PeriodColumn(col=0, label='m1', kind=MONTH, months=1, order=(2024, 12))
    m2 = PeriodColumn(col=1, label='m2', kind=MONTH, months=1, order=(2025, 1))
    ytd = PeriodColumn(col=2, label='YTD', kind=YTD, months=0, order=(2025, 12))
    vals = {0: Decimal('100'), 1: Decimal('100'), 2: Decimal('50')}   # Σseries 200 > total 50
    c = periods.collapse('revenue', 'flow', [m1, m2, ytd], vals)
    assert c.escalate and c.value is None


def test_different_year_total_still_escalates():
    # Analisa-like: monthly 2024 series + a DIFFERENT-year annual total (FYE 2021).
    # Σ<total but the years don't match → escalate (do not read a foreign-year total).
    m1 = PeriodColumn(col=0, label='Jan-24', kind=MONTH, months=1, order=(2024, 1))
    m2 = PeriodColumn(col=1, label='Feb-24', kind=MONTH, months=1, order=(2024, 2))
    ytd = PeriodColumn(col=2, label='FYE2021', kind=YTD, months=0, order=(2021, 12))
    vals = {0: Decimal('5'), 1: Decimal('5'), 2: Decimal('100')}
    c = periods.collapse('revenue', 'flow', [m1, m2, ytd], vals)
    assert c.escalate and c.value is None


def test_confirmed_additive_series_still_sums_all():
    # a stated total that matches Σseries CONFIRMS the columns are additive components
    # → sum ALL of them even though they aren't a full consecutive 12 (the comparative
    # guard must never suppress a total-validated partial series).
    s = _run(_months([(2025, m) for m in range(1, 7)]))   # 6 consecutive months
    col = periods._flow_from_series('rev', lambda c: Decimal('4'), s, DISCRETE,
                                    additive_confirmed=True)
    assert col.basis == 'partial' and col.months == 6 and col.value == Decimal('24')


def test_twelve_months_spanning_a_gap_is_not_ttm():
    # 12 columns but a hole (skips 2025-08) → not one consecutive window
    spec = [(2024, m) for m in range(9, 13)] + [(2025, m) for m in (1, 2, 3, 4, 5, 6, 7, 9)]
    s = _run(_months(spec))
    assert len(s) == 12
    assert periods._true_ttm_window(s) is None    # gap at Aug-25 breaks consecutiveness


def test_four_consecutive_quarters_is_ttm():
    s = _run([PeriodColumn(col=i, label=f'Q{q}', kind=QUARTER, months=3, order=(2025, q * 3))
              for i, q in enumerate((1, 2, 3, 4))])
    w = periods._true_ttm_window(s)
    assert w is not None and sum(c.months for c in w) == 12


def test_sub_12_months_never_fabricates_ttm():
    s = _run(_months([(2025, m) for m in range(1, 7)]))    # 6 consecutive months
    assert periods._true_ttm_window(s) is None
    col = periods._flow_from_series('rev', lambda c: Decimal('4'), s, DISCRETE)
    assert col.basis == 'partial' and col.months == 6
    assert col.value == Decimal('24')


# ── Validated as-of (Increment 4, D1): cadence-regularity peels a future-dated typo tail ──

def test_future_dated_typo_tail_is_peeled_and_stock_picks_true_latest():
    # Clientell: a 1-month run to Feb-2026 PLUS a future-dated typo '2026-12-25'. The typo is still
    # MONOTONE (Dec-2026 > Feb-2026) — a monotonicity check misses it — but it breaks the cadence by
    # ~10 months. It must NOT win the stock as-of; the true latest Feb-2026 does. (Clientell's real
    # AK80/AM80 in miniature.)
    cols = (_months([(2025, m) for m in range(1, 13)] + [(2026, 1), (2026, 2)])
            + [PeriodColumn(col=99, label='2026-12-25', kind=MONTH, months=1, order=(2026, 12))])
    assert periods._period_outlier_cols(cols) == {99}
    vals = {c.col: Decimal('19') for c in cols}
    vals[99] = Decimal('999')                              # the typo column holds a DIFFERENT number
    c = periods.collapse('headcount', 'stock', cols, vals)
    assert c.value == Decimal('19') and c.source_cols == [13]   # Feb-2026 (col 13), never the typo 99


def test_clean_monthly_series_peels_nothing():
    cols = _months([(2025, m) for m in range(1, 13)] + [(2026, 1), (2026, 2)])
    assert periods._period_outlier_cols(cols) == set()


def test_comparatives_only_series_peels_nothing():
    # CPC: May-24 | Dec-24 | May-25 — no dominant small cadence → peel nothing (fall back to raw max)
    assert periods._period_outlier_cols(_months([(2024, 5), (2024, 12), (2025, 5)])) == set()


def test_legit_multimonth_skip_is_below_break_floor_and_kept():
    # a real 3-month reporting gap is a plausible skip, not a typo → below the FLOOR, never peeled
    assert periods._period_outlier_cols(_months([(2025, 1), (2025, 2), (2025, 3), (2025, 6)])) == set()


# ── Unlabeled plan/actual overlap guard (Increment 4, D3 guard A): fail-closed HOLD ──

def _col(c, y, m, kind=MONTH):
    return PeriodColumn(col=c, label=f'{y}-{m:02d}', kind=kind, months=1, order=(y, m))


def test_unlabeled_period_overlap_flow_holds():
    # CPM shape: two columns for the SAME month with DIFFERENT non-zero values (unlabeled plan vs
    # actual) — no evidence to pick one → cannot sum → HOLD (never guess budget-as-actual).
    cols = [_col(0, 2025, 1), _col(1, 2025, 1), _col(2, 2025, 2)]
    vals = {0: Decimal('100'), 1: Decimal('120'), 2: Decimal('110')}   # Jan conflict 100≠120
    c = periods.collapse('revenue', 'flow', cols, vals)
    assert c.escalate and 'unlabeled_period_overlap' in c.flags


def test_unlabeled_period_overlap_stock_holds_when_asof_conflicts():
    # the AS-OF (latest) period carries two differing values → cannot pick the balance → HOLD.
    cols = [_col(0, 2025, 1), _col(1, 2025, 2), _col(2, 2025, 2)]
    vals = {0: Decimal('50'), 1: Decimal('60'), 2: Decimal('65')}      # Feb (as-of) conflict
    c = periods.collapse('cash', 'stock', cols, vals)
    assert c.escalate and 'unlabeled_period_overlap' in c.flags


def test_real_plus_empty_duplicate_is_not_a_conflict():
    # same period, one real + one ZERO/blank duplicate → resolves to the non-empty, NOT a conflict.
    cols = [_col(0, 2025, 1), _col(1, 2025, 1), _col(2, 2025, 2)]
    vals = {0: Decimal('100'), 1: Decimal('0'), 2: Decimal('110')}
    assert periods._conflicting_periods(cols, vals) == set()


def test_old_period_conflict_does_not_hold_a_clean_stock_asof():
    # SCOPING (the Clientell shape): a conflict at an OLD period must NOT hold a stock whose as-of
    # (latest) period is clean — the guard is scoped to the emit-relevant period, not the whole sheet.
    cols = [_col(0, 2023, 5), _col(1, 2023, 5), _col(2, 2026, 2)]
    vals = {0: Decimal('10'), 1: Decimal('20'), 2: Decimal('19')}      # old conflict, clean Feb-26 as-of
    c = periods.collapse('headcount', 'stock', cols, vals)
    assert not c.escalate and c.value == Decimal('19')


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'ok  {name}')
    print('ALL PASS')
