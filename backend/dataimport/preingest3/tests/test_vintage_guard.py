"""Vintage / recency guard for sheet selection — reddening + negative controls.

ROOT CAUSE this locks: `_best_sheet` (and the fork-b re-source / grid fallbacks) chose the statement
sheet by concept-COVERAGE with NO awareness of reporting VINTAGE. On Analisa a comprehensive-but-STALE
'ProfitLoss (23)' (latest Mar-2024) out-ranked the thinner CURRENT sheets on a May-2025 file and shipped
a ~14-month-stale EBITDA. The guard demotes (primary path) / excludes (fallback paths) a sheet whose
data never reaches the file's stated reporting as-of, and a forward-plan (budget/AOP/forecast) sheet, so
a wrong-PERIOD or budget figure can never win.

The real corpus proves DEMOTE→HOLD (Analisa's current sheets carry no clean EBITDA → it holds). It can
NOT prove PROMOTE→CURRENT (no real file has both a stale AND a current sheet carrying the same concept),
which is the COMMON case in a real batch. These synthetic INR fixtures prove promote-current end-to-end
on the REAL extract_company path, plus the fail-closed negative controls:
  • PROMOTE-CURRENT (§4.2): stale + current sheet each carry the concept → EMIT the CURRENT value.
  • REDDENING: with the as-of OFF (no filename month) the STALE sheet wins → the guard is the cause.
  • stale-only → HOLD, never fall back to the older sheet (refinement #3).
  • a forward-PLAN sheet is never picked as the actual (refinement #1).
  • cadence-scaled tolerance: one reporting period behind is NOT stale; many periods behind IS.
  • as_of unknown → guard inert (byte-identical fail-safe).
"""
import os
import tempfile
import types
from decimal import Decimal

import openpyxl
import pytest

from backend.dataimport.preingest3 import extract, periods
from backend.dataimport.preingest3.cir import Figure, Provenance
from backend.dataimport.preingest3.gate import AUTO
from backend.dataimport.preingest3.extract import (
    _axis_cadence_months, _best_sheet, _family_carriers, _sheet_vintage_flags, extract_company,
    MIS_CONCEPTS,
)
from backend.dataimport.preingest3.profiler import profile_file
from backend.dataimport.preingest3.ratecard import default_inr_card

_RC = default_inr_card('2026-02-28')
_ASOF = (2026, 2)


def _months(year_month_pairs):
    return [f'{["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][m-1]}-{y%100:02d}'
            for (y, m) in year_month_pairs]


# ── synthetic INR sheets (distinct months → TIME SERIES, not a comparison grid) ───────────────────
def _stale_bs():
    hdr = ['Balance Sheet (INR Cr)'] + _months([(2023, m) for m in range(1, 13)])
    cash = ['Cash and cash equivalents'] + [1 + m for m in range(12)]           # Dec-23 = 12
    assets = ['Total assets'] + [100 + m for m in range(12)]
    return [hdr, cash, assets]


def _current_bs():
    hdr = ['Balance Sheet (INR Cr)'] + _months([(2025, 10), (2025, 11), (2025, 12), (2026, 1), (2026, 2)])
    cash = ['Cash and cash equivalents', 480, 490, 495, 498, 500]               # Feb-26 = 500
    assets = ['Total assets', 900, 910, 920, 930, 940]
    return [hdr, cash, assets]


def _stale_pl():
    hdr = ['Statement of Profit and Loss (INR Cr)'] + _months([(2023, m) for m in range(1, 13)])
    rev = ['Revenue'] + [1 + m for m in range(12)]
    ebitda = ['EBITDA'] + [1 for _ in range(12)]
    return [hdr, rev, ebitda]


def _current_pl():
    hdr = ['Statement of Profit and Loss (INR Cr)'] + _months([(2025, 12), (2026, 1), (2026, 2)])
    rev = ['Revenue', 100, 110, 120]
    ebitda = ['EBITDA', 20, 22, 24]
    return [hdr, rev, ebitda]


def _plan_pl():                     # a forward AOP/budget: real values in months AFTER the as-of
    hdr = ['Statement of Profit and Loss (INR Cr)'] + _months(
        [(2026, 1), (2026, 2), (2026, 3), (2026, 4), (2026, 5)])
    rev = ['Revenue', 100, 110, 130, 150, 170]      # Mar/Apr/May-26 are AFTER Feb-26 as-of → plan
    ebitda = ['EBITDA', 20, 22, 26, 30, 34]
    return [hdr, rev, ebitda]


def _write_book(path, sheets):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets:
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    wb.save(path)
    return path


def _prof_from(sheets):
    ss = [types.SimpleNamespace(sheet=n, currency_hints=['INR']) for n, _ in sheets]
    return {'sheets': ss, 'grid': {n: [list(r) for r in rows] for n, rows in sheets}}


def _axlc(rows):
    ax = periods.detect_period_axis(rows)
    lc = extract._sheet_label_col(rows, ax.axis_rows[0]) if ax.columns else None
    return ax, lc


# ── fixture sanity: the synthetic sheets are TIME SERIES with the intended vintages ───────────────
def test_fixtures_are_time_series_with_expected_vintage():
    for rows in (_stale_bs(), _current_bs(), _stale_pl(), _current_pl(), _plan_pl()):
        ax = periods.detect_period_axis(rows)
        assert ax.is_time_series and ax.columns, 'fixture must be a time series, not a comparison grid'
    ax, _ = _axlc(_current_bs())
    assert max(pc.order for pc in ax.columns) == (2026, 2)
    ax, _ = _axlc(_stale_bs())
    assert max(pc.order for pc in ax.columns) == (2023, 12)


# ── §4.2 PROMOTE-CURRENT (headline, real extract_company path) ────────────────────────────────────
def test_vintage_guard_promotes_current_sheet_and_emits_current_value():
    with tempfile.TemporaryDirectory() as d:
        # filename carries the reporting month → _stated_as_of = Feb-2026
        path = _write_book(os.path.join(d, 'Synthco_MIS_Feb26.xlsx'),
                           [('Balance Sheet 2023', _stale_bs()), ('Balance Sheet 2026', _current_bs())])
        rec = extract_company('Synthco', path, rate_card=_RC, entity='Synthco', anchor_cr=Decimal('500'))
        cash = rec.fields.get('cash')
        assert isinstance(cash, Figure) and cash.confirmed, f'cash should EMIT, got {cash!r}'
        assert cash.provenance.sheet == 'Balance Sheet 2026', \
            f'cash must come from the CURRENT sheet, got {cash.provenance.sheet!r}'
        assert cash.value_cr == Decimal('500'), f'cash must be the Feb-26 value 500, got {cash.value_cr}'


def test_reddening_without_as_of_the_stale_sheet_would_win():
    # The guard is the CAUSE: same two sheets, as_of OFF → coverage/columns pick the STALE sheet;
    # as_of ON → the CURRENT sheet. (Selection level, no file needed.)
    prof = _prof_from([('Balance Sheet 2023', _stale_bs()), ('Balance Sheet 2026', _current_bs())])
    blind = _best_sheet(prof, MIS_CONCEPTS, as_of=None)
    guarded = _best_sheet(prof, MIS_CONCEPTS, as_of=_ASOF)
    assert blind[2] == 'Balance Sheet 2023', f'without the guard the stale sheet wins, got {blind[2]!r}'
    assert guarded[2] == 'Balance Sheet 2026', f'the guard must promote the current sheet, got {guarded[2]!r}'


# ── flag classification ───────────────────────────────────────────────────────────────────────────
def test_sheet_vintage_flags_classify_stale_current_and_plan():
    def flags(rows):
        ax, lc = _axlc(rows)
        found = {c: extract._find_concept_row(rows, lc, c, ax.axis_rows[0] + 1, len(rows), ax.columns)
                 for c in MIS_CONCEPTS}
        found = {c: r for c, r in found.items() if r is not None}
        return _sheet_vintage_flags(rows, ax, _ASOF, found)
    assert flags(_stale_pl()) == (True, False)      # stale, not forward
    assert flags(_current_pl()) == (False, False)   # current actual
    assert flags(_plan_pl()) == (False, True)       # reaches as-of but has post-as-of plan values → forward


def test_as_of_none_makes_guard_inert():
    # fail-safe byte-identical: no reporting as-of known → NEITHER flag fires, on any sheet.
    for rows in (_stale_pl(), _current_pl(), _plan_pl()):
        ax, lc = _axlc(rows)
        found = {c: extract._find_concept_row(rows, lc, c, ax.axis_rows[0] + 1, len(rows), ax.columns)
                 for c in MIS_CONCEPTS}
        found = {c: r for c, r in found.items() if r is not None}
        assert _sheet_vintage_flags(rows, ax, None, found) == (False, False)


# ── refinement #3: stale-only fallback must HOLD, never reach back ─────────────────────────────────
def test_family_carriers_excludes_stale_carrier_no_fallback():
    # only a STALE income carrier exists for ebitda → the re-source fallback yields NOTHING (→ hold),
    # rather than re-sourcing the stale value. Reddening: as_of None → the stale carrier IS returned.
    prof = _prof_from([('BS current', _current_bs()), ('PL 2023', _stale_pl())])
    assert _family_carriers(prof, 'ebitda', 'BS current', as_of=_ASOF) == []       # excluded → no fallback
    assert [c[3] for c in _family_carriers(prof, 'ebitda', 'BS current', as_of=None)] == ['PL 2023']


# ── refinement #1: a forward-PLAN sheet is never chosen as the actual ──────────────────────────────
def test_forecast_plan_sheet_is_not_picked_as_actual():
    prof = _prof_from([('PL current', _current_pl()), ('PL AOP', _plan_pl())])
    best = _best_sheet(prof, MIS_CONCEPTS, as_of=_ASOF)
    assert best[2] == 'PL current', f'the plan sheet must not win; got {best[2]!r}'


def test_stale_plus_plan_only_leaves_no_current_sheet_holds():
    # both a stale-actual and a forward-plan sheet carry the concept, but NO current-actual one →
    # the re-source fallback must return [] (→ hold), never emit stale or plan.
    prof = _prof_from([('BS current', _current_bs()), ('PL 2023', _stale_pl()), ('PL AOP', _plan_pl())])
    assert _family_carriers(prof, 'ebitda', 'BS current', as_of=_ASOF) == []


# ── condition #3: cadence-scaled tolerance ────────────────────────────────────────────────────────
def test_axis_cadence_is_derived_monthly_quarterly_annual():
    monthly, _ = _axlc(_current_bs())
    assert _axis_cadence_months(monthly) == 1
    q = [['P&L (INR Cr)', 'Q1-25', 'Q2-25', 'Q3-25', 'Q4-25'], ['Revenue', 10, 11, 12, 13]]
    ax, _ = _axlc(q)
    assert _axis_cadence_months(ax) == 3
    annual = [['P&L (INR Cr)', 'FY23', 'FY24', 'FY25'], ['Revenue', 10, 11, 12]]
    ax, _ = _axlc(annual)
    assert _axis_cadence_months(ax) == 12


def test_one_reporting_period_behind_is_not_stale_but_many_are():
    # a MONTHLY sheet whose latest actual is exactly ONE month before the as-of is NOT stale
    # (reporting lag); 14 months behind IS. Cadence-derived, no fixed constant.
    lag1 = [['Balance Sheet (INR Cr)'] + _months([(2025, 10), (2025, 11), (2025, 12), (2026, 1)]),
            ['Cash and cash equivalents', 1, 2, 3, 4]]        # latest Jan-26, as_of Feb-26 → 1 mo behind
    ax, lc = _axlc(lag1)
    found = {'cash': extract._find_concept_row(lag1, lc, 'cash', ax.axis_rows[0] + 1, len(lag1), ax.columns)}
    assert _sheet_vintage_flags(lag1, ax, _ASOF, found) == (False, False)
    ax, lc = _axlc(_stale_bs())                                # latest Dec-23, as_of Feb-26 → 14 mo behind
    found = {'cash': extract._find_concept_row(_stale_bs(), lc, 'cash', ax.axis_rows[0] + 1,
                                               len(_stale_bs()), ax.columns)}
    assert _sheet_vintage_flags(_stale_bs(), ax, _ASOF, found) == (True, False)


# ── THE all-stale residual: every sheet predates the as-of → HOLD, never emit stale as current ────
def _income_grid_current():                 # a CURRENT comparison grid (repeated months → grid, not series)
    return [['Statement of Profit and Loss (INR Cr)', 'Jan-26', 'Feb-26', 'Jan-26', 'Feb-26'],
            ['Revenue', 100, 110, 100, 110], ['COGS', 40, 40, 40, 40],
            ['Gross Profit', 60, 70, 60, 70], ['Operating Expenses', 30, 30, 30, 30],
            ['EBITDA', 30, 40, 30, 40]]


def test_all_stale_file_holds_every_concept_never_emits_stale():
    # every candidate sheet is 2023 on a Feb-2026 file → nothing may EMIT (the primary only demotes, so
    # this is the case demotion alone would still ship). All MIS concepts HOLD with a disclosed reason.
    with tempfile.TemporaryDirectory() as d:
        path = _write_book(os.path.join(d, 'Allstale_MIS_Feb26.xlsx'),
                           [('Balance Sheet 2023', _stale_bs()), ('P&L 2023', _stale_pl())])
        rec = extract_company('Allstale', path, rate_card=_RC, entity='Allstale', anchor_cr=Decimal('10'))
        for c in ('revenue', 'ebitda', 'cash'):
            fig = rec.fields.get(c)
            assert isinstance(fig, Figure) and not fig.confirmed, f'{c} must NOT emit stale, got {fig!r}'
            assert 'no current-actual statement' in (fig.hold_reason or ''), \
                f'{c} hold must disclose the all-stale reason, got {fig.hold_reason!r}'


def test_reddening_all_stale_without_a_filename_month_still_emits_stale():
    # the guard is the CAUSE: the SAME all-stale sheets under a filename with NO month → as-of unknown →
    # guard OFF → the stale 2023 value emits. Proves the hold above is the guard, not incidental.
    with tempfile.TemporaryDirectory() as d:
        path = _write_book(os.path.join(d, 'Allstale_no_month.xlsx'),
                           [('Balance Sheet 2023', _stale_bs()), ('P&L 2023', _stale_pl())])
        rec = extract_company('Allstale', path, rate_card=_RC, entity='Allstale', anchor_cr=Decimal('10'))
        emitted = [c for c in ('revenue', 'ebitda')
                   if isinstance(rec.fields.get(c), Figure) and rec.fields[c].confirmed]
        assert emitted, 'without a filename month the guard is off and the stale value emits (reddening)'


def test_all_stale_timeseries_but_current_grid_still_recovers():
    # all-stale must NOT over-suppress: if a CURRENT comparison-grid carries a concept, the vintage-
    # excluding grid fallback still emits it (only the stale time-series primary is silenced).
    with tempfile.TemporaryDirectory() as d:
        path = _write_book(os.path.join(d, 'Mixedvintage_MIS_Feb26.xlsx'),
                           [('Balance Sheet 2023', _stale_bs()), ('PL current', _income_grid_current())])
        rec = extract_company('Mixed', path, rate_card=_RC, entity='Mixed', anchor_cr=Decimal('200'))
        rev = rec.fields.get('revenue')
        assert isinstance(rev, Figure) and rev.confirmed, f'current-grid revenue must emit, got {rev!r}'
        assert rev.provenance.sheet == 'PL current', f'revenue must come from the current grid, got {rev.provenance.sheet!r}'
        cash = rec.fields.get('cash')                          # no CURRENT balance sheet → cash holds, not stale
        assert not (isinstance(cash, Figure) and cash.confirmed), f'cash must not emit stale, got {cash!r}'


# ── model-path (use_model) location-filter at the _model_emit choke (deterministic, no AI run) ─────
def _model_prof():
    return _prof_from([('P&L 2023', _stale_pl()), ('P&L 2026', _current_pl())])


def _prov(sheet):
    return Provenance(source_file='t', content_fingerprint='', sheet=sheet, cell='A2', row_label='EBITDA')


def test_model_emit_refuses_a_stale_located_sheet():
    # a PASSING (AUTO) model verdict whose located sheet is stale is REFUSED at the choke → None,
    # even though build() would have produced a figure. The model may not source what the
    # deterministic path rejects as stale. (No AI: _model_emit is called directly.)
    prof = _model_prof()
    verdict = types.SimpleNamespace(status=AUTO)
    built = Figure('ebitda', Decimal('99'), None, _prov('P&L 2023'), )
    out = extract._model_emit('ebitda', verdict, _prov('P&L 2023'),
                              build=lambda: built, prof=prof, as_of=_ASOF)
    assert out is None, 'a stale located sheet must be refused at the _model_emit choke'


def test_model_emit_allows_a_current_located_sheet():
    prof = _model_prof()
    verdict = types.SimpleNamespace(status=AUTO)
    built = Figure('ebitda', Decimal('99'), None, _prov('P&L 2026'))
    out = extract._model_emit('ebitda', verdict, _prov('P&L 2026'),
                              build=lambda: built, prof=prof, as_of=_ASOF)
    assert out is built, 'a current located sheet must pass the choke'


def test_model_emit_reddening_without_as_of_the_stale_location_passes():
    # the filter is the CAUSE: same stale location, as_of None (or prof None) → not filtered → emits.
    prof = _model_prof()
    verdict = types.SimpleNamespace(status=AUTO)
    built = Figure('ebitda', Decimal('99'), None, _prov('P&L 2023'))
    assert extract._model_emit('ebitda', verdict, _prov('P&L 2023'),
                               build=lambda: built, prof=prof, as_of=None) is built
    assert extract._model_emit('ebitda', verdict, _prov('P&L 2023'),
                               build=lambda: built) is built           # prof/as_of default None → inert


def test_model_emit_non_auto_verdict_still_holds():
    # unchanged existing behavior: a non-AUTO verdict never emits, filter or not.
    prof = _model_prof()
    built = Figure('ebitda', Decimal('99'), None, _prov('P&L 2026'))
    assert extract._model_emit('ebitda', types.SimpleNamespace(status='HOLD'), _prov('P&L 2026'),
                               build=lambda: built, prof=prof, as_of=_ASOF) is None
    assert extract._model_emit('ebitda', None, _prov('P&L 2026'),
                               build=lambda: built, prof=prof, as_of=_ASOF) is None


# ── refinement #3 (value level): empty current cell HOLDS, no older-sheet fallback ────────────────
def test_empty_current_cell_holds_never_falls_back_to_older_sheet():
    # the CURRENT balance sheet carries the cash row but its as-of column is EMPTY; a STALE sheet has a
    # value. The result must be a HOLD/absent cash, NEVER the stale value.
    cur = [['Balance Sheet (INR Cr)'] + _months([(2025, 12), (2026, 1), (2026, 2)]),
           ['Cash and cash equivalents', 495, 498, None]]     # Feb-26 empty
    with tempfile.TemporaryDirectory() as d:
        path = _write_book(os.path.join(d, 'Emptyco_MIS_Feb26.xlsx'),
                           [('BS current', cur), ('BS 2023', _stale_bs())])
        rec = extract_company('Emptyco', path, rate_card=_RC, entity='Emptyco', anchor_cr=Decimal('500'))
        cash = rec.fields.get('cash')
        stale_vals = {Decimal('12'), Decimal('11')}           # the stale Dec-23 / Nov-23 cash
        if isinstance(cash, Figure):
            assert not (cash.confirmed and cash.value_cr in stale_vals), \
                f'must not fall back to the stale value, got {cash.value_cr} from {cash.provenance.sheet!r}'
