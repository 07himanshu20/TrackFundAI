"""Regression fixtures for the stated-as-of resolver (Rung 2) — the source's own reporting boundary,
read from the FILENAME (non-circular; never from a data column). Locks the two bugs found + fixed:

  Bug 1 — the reporting month lives in an UNDERSCORE-joined token ('..._MIS_Feb26'); '_' is a regex
          word char, so a \\b-anchored pattern never matched → None for every file. Fix: normalise
          separators to spaces first, drop the \\b dependence.
  Bug 2 — an in-cell 'as on <date>' scan FALSE-POSITIVES on operational notes / prior-year
          comparatives (Hubler's only cue cell is 'Orders under delivery - as on 1 march 2023' →
          (2023,3), which would bound out every Feb-2026 actual). Fix: the header scan is NOT used;
          the filename is the operative, noise-immune bound.
"""
import os

from backend.dataimport.preingest3 import periods
from backend.dataimport.preingest3.periods import PeriodColumn, MONTH, YTD
from backend.dataimport.preingest3.extract import _filename_month, _stated_as_of, _filename_understated
from backend.dataimport.preingest3.profiler import profile_file

_IN = '/Users/himanshusharma/portfolio-dashboard/backend/media/preingest/trivesta/100e86d5/in'
_REAL = [
    ('AVF_2026_03_12_P_Agnikul_MIS_Feb26.xlsx', (2026, 2)),
    ('AVF_2026_03_16_P_Clientell_MIS_Feb26.xlsx', (2026, 2)),
    ('AVF_2026_03_11_P_Hubler_MIS_Feb26.xlsx', (2026, 2)),
    ('AVF_2026_03_18_P_InstaAstro_MIS_Feb_2026.xlsx', (2026, 2)),
    ('AVF_2026_03_20_P_LDC_Detailed_MIS_Feb26.xlsx', (2026, 2)),
]


# ── Bug 1: underscore-joined month token must parse (the exact failure) ──
def test_underscore_joined_month_parses():
    # '..._MIS_Feb26' — the case a \b-anchored regex could never match ('_' is a word char).
    assert _filename_month('AVF_2026_03_12_P_Agnikul_MIS_Feb26.xlsx') == (2026, 2)


def test_prep_date_is_not_mistaken_for_reporting_month():
    # the leading yyyy_mm_dd is the file-PREP date (March 12); the reporting month is the spelled Feb.
    assert _filename_month('AVF_2026_03_12_P_Agnikul_MIS_Feb26.xlsx') == (2026, 2)


def test_separator_styles_all_parse():
    for name, exp in [('x_MIS_Feb26.xlsx', (2026, 2)),        # underscore + 2-digit year
                      ('x-mis-mar-2025.xlsx', (2025, 3)),     # hyphen + spelled + 4-digit
                      ('x MIS Dec 24.xlsx', (2024, 12)),      # spaces
                      ('report.jan2026.xlsx', (2026, 1))]:    # dots
        assert _filename_month(name) == exp, name


def test_year_then_month_order_parses():
    # '..._2025_May_Analisa' — the reporting month is written year-BEFORE-month; must resolve (2025,5).
    assert _filename_month('01_Monthly_Financial_Presentation_2025_May_Analisa.xlsx') == (2025, 5)
    assert _filename_month(os.path.join(_IN, '01_Monthly_Financial_Presentation_2025_May_Analisa.xlsx')) == (2025, 5)


# ── under-statement conflict guard: CADENCE, not count (the filename is a single point of trust) ──
def _m(col, y, mo, kind=MONTH):
    return PeriodColumn(col=col, label=f'{y}-{mo:02d}', kind=kind, months=1, order=(y, mo))


def test_filename_understated_fires_on_off_by_one_nonpeeled():
    # THE case a count threshold missed: filename says Feb but the row runs one cadence-consistent month
    # forward to Mar (a file re-saved +1 month under a stale name). A single non-peeled future month → True.
    cols = [_m(0, 2026, 1), _m(1, 2026, 2), _m(2, 2026, 3)]
    rows = [[10, 20, 30]]
    assert _filename_understated(rows, cols, {'revenue': 0}, (2026, 2)) is True


def test_filename_understated_ignores_peeled_typo_outlier():
    # Clientell's real shape: a long monthly run to Feb-2026 + a gross-jump typo '2026-12-25'. The typo is
    # a cadence-peel OUTLIER → excluded → the guard does NOT fire (a count-2 guard only left it alone by
    # coincidence; cadence leaves it alone by principle).
    cols = [_m(i, 2025, i + 1) for i in range(12)] + [_m(12, 2026, 1), _m(13, 2026, 2),
                                                      _m(14, 2026, 12)]   # trailing Dec-2026 typo
    rows = [[1] * 15]
    from backend.dataimport.preingest3 import periods as _P
    assert 14 in _P._period_outlier_cols(cols)                            # the typo IS peeled
    assert _filename_understated(rows, cols, {'revenue': 0}, (2026, 2)) is False


def test_filename_understated_ignores_zero_placeholder_future_month():
    # R1a-2 (the fix + its reddening pair): a future-month column pre-laid as 0 by the template
    # (Aliste 'P&L' Mar-2026 = 0 after a Feb-2026 as-of) is an EMPTY placeholder, not newer data — it
    # must NOT trip the guard and fail-close a correct filename over a live figure. The discriminator is
    # the VALUE, not the column: the IDENTICAL shape carrying a real non-zero actual in that same Mar
    # column DOES still fire. So the `!= 0` clause is load-bearing (spares only the zero) and does not
    # blanket-ignore future months (a genuine +1-month re-save is still caught).
    cols = [_m(0, 2026, 1), _m(1, 2026, 2), _m(2, 2026, 3)]
    assert _filename_understated([[10, 20, 0]], cols, {'revenue': 0}, (2026, 2)) is False   # 0 placeholder → spared
    assert _filename_understated([[10, 20, 30]], cols, {'revenue': 0}, (2026, 2)) is True   # real actual → still fires


def test_filename_understated_false_when_nothing_past_asof():
    cols = [_m(0, 2026, 1), _m(1, 2026, 2)]
    rows = [[10, 20]]
    assert _filename_understated(rows, cols, {'revenue': 0}, (2026, 2)) is False


def test_filename_understated_ignores_cumulative_future_sentinels():
    # a YTD/(yr,12) sentinel is not a discrete future month → never trips understatement.
    cols = [_m(0, 2026, 2), PeriodColumn(col=1, label='YTD FY26', kind=YTD, months=0, order=(2026, 12))]
    rows = [[20, 51]]
    assert _filename_understated(rows, cols, {'revenue': 0}, (2026, 2)) is False


def test_no_spelled_month_returns_none_safe_degradation():
    # no month in the name → None → the caller applies NO bound (safe, never a wrong bound).
    assert _filename_month('AVF_2026_03_12_P_Agnikul_Quarterly.xlsx') is None
    assert _filename_month('random_export_v3.xlsx') is None


def test_company_name_with_month_substring_does_not_false_match():
    # 'augment' contains 'aug' but has no trailing digits → not a month token.
    assert _filename_month('P_Augment_Labs_report.xlsx') is None


# ── the five real files resolve Feb-2026 from their filenames ──
def test_real_files_resolve_feb_2026():
    for fn, exp in _REAL:
        assert _filename_month(os.path.join(_IN, fn)) == exp, fn


# ── Bug 2 negative control: the noisy header date can no longer poison the bound ──
def test_hubler_bound_is_filename_feb_not_the_orders_note_march_2023():
    # Hubler's ONLY in-cell 'as on' cue is an operational note ('Orders under delivery - as on 1 march
    # 2023'). If the header scan were used, the bound would be (2023,3) and bound out every Feb-2026
    # actual. The resolver must return the filename's (2026,2), proving the noisy path is not taken.
    path = os.path.join(_IN, 'AVF_2026_03_11_P_Hubler_MIS_Feb26.xlsx')
    assert _stated_as_of(profile_file('hubbler', path), path) == (2026, 2)


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'ok  {name}')
    print('ALL PASS')
