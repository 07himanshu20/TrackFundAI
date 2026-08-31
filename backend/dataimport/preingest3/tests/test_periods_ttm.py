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


# ── Stated as-of bound (Rung 2): exclude a forward-projection MONTH, never a cumulative span ──
# Root cause it closes: the label-based scenario filter misses an UNLABELED future month (LDC 'Mar'26'
# with a value), and the cadence-peel misses a plausibly-spaced one (gap == modal cadence). The bound
# uses the source's own STATED as-of (from the filename) to drop a discrete month dated past it.
# DESIGN TRAP the kind-scoping avoids: a naive 'order > as_of' also drops a cumulative column whose
# `order` is a SORT-SENTINEL (YTD→(yr,12), TOTAL→(9999,12)) — false-dropping an ACTUAL (Agnikul YTD).

_TOTAL = periods.TOTAL
_YEAR = periods.YEAR


def test_asof_bound_drops_future_month_for_stock_and_reddens_without_bound():
    # LDC shape: monthly run to Feb-2026 plus a Mar-2026 forecast MONTH carrying a value. The stock
    # `max` would grab Mar without a bound; with as_of=Feb-2026 it binds the true Feb balance.
    cols = _months([(2026, 1), (2026, 2), (2026, 3)])       # Jan, Feb, Mar
    vals = {0: Decimal('50'), 1: Decimal('60'), 2: Decimal('999')}   # Mar = forecast stub
    bounded = periods.collapse('cash', 'stock', cols, vals, as_of=(2026, 2))
    assert bounded.value == Decimal('60') and bounded.source_cols == [1]      # Feb, not Mar
    # REDDENING negative control: bound ABSENT → the defect returns (Mar-2026 forecast is picked)
    unbounded = periods.collapse('cash', 'stock', cols, vals)
    assert unbounded.value == Decimal('999') and unbounded.source_cols == [2]


def test_asof_bound_excludes_future_month_from_flow_sum_and_reddens_without_bound():
    # a discrete monthly flow with a trailing forecast month: the bound keeps it out of the sum.
    cols = _months([(2026, 1), (2026, 2), (2026, 3)])
    vals = {0: Decimal('10'), 1: Decimal('10'), 2: Decimal('10')}
    bounded = periods.collapse('revenue', 'flow', cols, vals, as_of=(2026, 2))
    assert bounded.value == Decimal('20') and 3 not in [c for c in bounded.source_cols]   # Jan+Feb only
    unbounded = periods.collapse('revenue', 'flow', cols, vals)          # bound absent → Mar summed in
    assert unbounded.value == Decimal('30')


def test_asof_bound_NEVER_drops_cumulative_ytd_the_design_trap_control():
    # THE ANTI-CORRUPTION CONTROL. Agnikul: Jan'26, Feb'26 months + 'YTD FY26' (order (2026,12), the
    # emitted revenue). as_of=Feb-2026. The kind-aware bound MUST keep the YTD (a cumulative span, not
    # a future month) — and we prove a NAIVE 'order > as_of' bound WOULD have deleted it (the trap).
    jan = PeriodColumn(col=0, label="Jan'26", kind=MONTH, months=1, order=(2026, 1))
    feb = PeriodColumn(col=1, label="Feb'26", kind=MONTH, months=1, order=(2026, 2))
    ytd = PeriodColumn(col=2, label='YTD FY26', kind=YTD, months=0, order=(2026, 12))
    vals = {0: Decimal('5.73'), 1: Decimal('4.89'), 2: Decimal('51.46')}
    c = periods.collapse('revenue', 'flow', [jan, feb, ytd], vals, as_of=(2026, 2))
    assert not c.escalate and c.value == Decimal('51.46')                # YTD preserved
    # the trap made explicit: a naive same-order comparison drops the YTD (its sentinel (2026,12) > as_of)
    naive_survivors = [col for col in (jan, feb, ytd) if not (col.order != (0, 0) and col.order > (2026, 2))]
    assert ytd not in naive_survivors                                     # naive bound WOULD corrupt


def test_asof_bound_never_drops_total_or_year_columns():
    # TOTAL (9999,12) and YEAR (yr,12) are cumulative sentinels, never future months → always kept.
    # A NAIVE 'order > as_of' bound would drop them (sentinel year >> as_of), emptying the axis to an
    # escalate; the kind-aware bound keeps them so the single-total path reads the value directly.
    tot = PeriodColumn(col=0, label='Grand Total', kind=_TOTAL, months=12, order=(9999, 12))
    c = periods.collapse('revenue', 'flow', [tot], {0: Decimal('48')}, as_of=(2026, 2))
    assert not c.escalate and c.value == Decimal('48')                    # total kept, read directly
    yr = PeriodColumn(col=0, label='FY26', kind=_YEAR, months=12, order=(2026, 12))
    c2 = periods.collapse('revenue', 'flow', [yr], {0: Decimal('60')}, as_of=(2026, 2))
    assert not c2.escalate and c2.value == Decimal('60')                  # FY-year kept, not dropped


def test_asof_bound_keeps_straddling_quarter():
    # Q4 FY26 (Jan–Mar) has order == end-month (2026,3); as_of Feb-2026 STRADDLES it. A quarter is a
    # span (partial actual through the as-of), not a future point → the month-scoped bound never drops it.
    q4 = PeriodColumn(col=0, label='Q4', kind=QUARTER, months=3, order=(2026, 3))
    vals = {0: Decimal('30')}
    c = periods.collapse('cash', 'stock', [q4], vals, as_of=(2026, 2))
    assert c.value == Decimal('30')                                       # kept, not stripped


def test_asof_bound_none_is_the_LIBRARY_DEFAULT_noop_not_a_blessed_failopen():
    # as_of=None + require_bound=False is the LIBRARY default (backward-compatible no-op) — NOT a
    # blessed production fail-open. Production (extract_company) NEVER relies on it: it either resolves a
    # bound or sets require_bound=True (the fail-closed floor below). This test pins the primitive; the
    # require_bound tests pin the production floor. Keeping both apart is the fix for 'fail-open looks green'.
    cols = _months([(2026, 1), (2026, 2), (2026, 3)])
    vals = {0: Decimal('10'), 1: Decimal('20'), 2: Decimal('30')}
    a = periods.collapse('cash', 'stock', cols, vals, as_of=None)
    b = periods.collapse('cash', 'stock', cols, vals)
    assert a.value == b.value == Decimal('30')                            # library default unchanged


# ── Fail-closed FLOOR (Rung-2 bottom rung): no bound established → never proceed unguarded ──

def test_require_bound_holds_projection_ambiguous_multimonth_and_reddens_without_it():
    # PRODUCTION FLOOR: no reporting as-of could be established (as_of=None) and the caller REQUIRES one.
    # A latest MONTH pick over ≥2 month periods cannot be proven an actual vs a projection → HOLD.
    cols = _months([(2026, 1), (2026, 2), (2026, 3)])
    vals = {0: Decimal('10'), 1: Decimal('20'), 2: Decimal('30')}
    held = periods.collapse('cash', 'stock', cols, vals, as_of=None, require_bound=True)
    assert held.escalate and held.value is None and 'unbounded_projection_risk' in held.flags
    # REDDENING control: WITHOUT the floor (require_bound=False) the defect returns (unguarded max picked)
    open_ = periods.collapse('cash', 'stock', cols, vals, as_of=None, require_bound=False)
    assert open_.value == Decimal('30') and not open_.escalate


def test_require_bound_still_emits_single_month_no_ambiguity():
    # only ONE month period → no trailing projection to rule out → emits even with require_bound.
    cols = _months([(2026, 2)])
    c = periods.collapse('cash', 'stock', cols, {0: Decimal('20')}, as_of=None, require_bound=True)
    assert not c.escalate and c.value == Decimal('20')


def test_require_bound_still_emits_cumulative_only_no_month_ambiguity():
    # a cumulative-only axis (YTD/total) has no discrete month whose actuality is in doubt → emits.
    ytd = PeriodColumn(col=0, label='YTD FY26', kind=YTD, months=0, order=(2026, 12))
    c = periods.collapse('revenue', 'flow', [ytd], {0: Decimal('51.46')}, as_of=None, require_bound=True)
    assert not c.escalate and c.value == Decimal('51.46')


def test_asof_bound_never_empties_the_axis_fail_open():
    # a too-early / mislabeled as_of that would remove EVERY column → keep them all (better a flagged
    # figure than a false hold from a bad bound); downstream escalation still guards it.
    cols = _months([(2026, 1), (2026, 2)])
    vals = {0: Decimal('10'), 1: Decimal('20')}
    c = periods.collapse('cash', 'stock', cols, vals, as_of=(2025, 1))    # before all data
    assert c.value == Decimal('20')                                       # not emptied → latest kept


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'ok  {name}')
    print('ALL PASS')
