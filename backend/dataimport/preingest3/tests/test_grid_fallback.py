"""Grid-statement fallback (tight KIND-GATE) — reddening + first-class negative controls.

The `is_time_series` exclusion wrongly dropped self-declared / income-CONFIRMED comparison-GRIDS
from every source path. This locks the tight fix:
  • POSITIVE (reddening): an eligible grid statement becomes a source and emits (synthetic INR grid;
    CSS Summary real file for the SGD currency path).
  • NEGATIVE controls (the 63:6 junk risk): a junk grid (kind=None, non-income) is NEVER eligible;
    a multi-scope grid HOLDS; a statement-SHAPED-but-unconfirmed grid is HELD-and-disclosed (§4),
    never silently skipped.
  • ASSUMPTION-PROOF #1: the income-CONFIRMED route generalises to GRID shape.
"""
import os
import types
from decimal import Decimal

import pytest

from backend.dataimport.preingest3 import extract, periods, family
from backend.dataimport.preingest3.cir import Figure
from backend.dataimport.preingest3.ratecard import RateCard, default_inr_card

_RC = default_inr_card('2026-06-30')


def _prof(name, rows, currency_hints=('INR',)):
    s = types.SimpleNamespace(sheet=name, currency_hints=list(currency_hints))
    return {'sheets': [s], 'grid': {name: [list(r) for r in rows]}}


def _ident(fp='cfp-grid'):
    return types.SimpleNamespace(content_fp=fp, layout_fp='lfp-' + fp)


def _axlc(rows):
    ax = periods.detect_period_axis(rows)
    lc = extract._sheet_label_col(rows, ax.axis_rows[0]) if ax.columns else None
    return ax, lc


def _resource(name, rows, concept, **kw):
    prof = _prof(name, rows, kw.pop('currency_hints', ('INR',)))
    return extract._grid_resource(prof, concept, primary_sheet='__none__', ident=_ident(),
                                  label='t.xlsx', geo_ccy=None, inr_mentioned=True,
                                  anchor_cr=kw.pop('anchor_cr', None), rate_card=kw.pop('rate_card', _RC),
                                  as_of=kw.pop('as_of', (2025, 2)), require_bound=False)


# ── fixtures: comparison GRIDS (periods REPEAT across columns → is_comparison_grid) ──────────────
# self-declared P&L grid; a Budget scenario block ABOVE the axis is scenario-filtered → clean series
INCOME_GRID = [
    ['Statement of Profit and Loss (INR Cr)', 'Actual', 'Actual', 'Budget', 'Budget'],
    ['', 'Jan-25', 'Feb-25', 'Jan-25', 'Feb-25'],
    ['Revenue', 10, 11, 99, 99],
    ['COGS', 4, 4, 40, 40],
    ['Gross Profit', 6, 7, 59, 59],
    ['Operating Expenses', 3, 4, 30, 30],
    ['EBITDA', 3, 3, 29, 29],
]
# income identity holds but NO 'profit and loss' title → kind=None (exercises the income-CONFIRMED route)
INCOME_CONFIRMED_NOKIND = [
    ['Monthly Figures (INR Cr)', 'Jan-25', 'Feb-25', 'Jan-25', 'Feb-25'],
    ['Revenue', 10, 11, 10, 11],
    ['COGS', 4, 4, 4, 4],
    ['Gross Profit', 6, 7, 6, 7],
    ['Operating Expenses', 3, 4, 3, 4],
    ['EBITDA', 3, 3, 3, 3],
]
# junk grid: kind=None, non-income, carries a cash-ish flow row
JUNK_GRID = [
    ['Receivables Aging Report', 'Jan-25', 'Feb-25', 'Jan-25', 'Feb-25'],
    ['0-30 days', 100, 110, 100, 110],
    ['Cash collected', 50, 55, 50, 55],
]
# statement-SHAPED but kind=None and identity NOT confirmed (revenue only) → §4 held-candidate
STMT_SHAPED_NOKIND = [
    ['Monthly Numbers', 'Jan-25', 'Feb-25', 'Jan-25', 'Feb-25'],
    ['Revenue', 10, 11, 10, 11],
]
# eligible P&L grid but two scopes carry DIFFERENT values for the same period → conflict → HOLD
MULTISCOPE_GRID = [
    ['Statement of Profit and Loss (INR Cr)', 'Jan-25', 'Feb-25', 'Jan-25', 'Feb-25'],
    ['Revenue', 10, 11, 30, 33],
]


def test_all_fixtures_are_actually_grids():
    # guard the guards: every fixture must be a COMPARISON GRID (not a time series), else the tests
    # would be exercising the wrong path.
    for rows in (INCOME_GRID, INCOME_CONFIRMED_NOKIND, JUNK_GRID, STMT_SHAPED_NOKIND, MULTISCOPE_GRID):
        ax = periods.detect_period_axis(rows)
        assert ax.columns and ax.is_comparison_grid and not ax.is_time_series


# ── eligibility (pure gate) ──────────────────────────────────────────────────────────────────────
def test_eligible_selfdeclared_income_grid():
    ax, lc = _axlc(INCOME_GRID)
    ok, kind = extract._grid_eligible(INCOME_GRID, ax, lc, 'PL', 'revenue')
    assert ok and kind == 'income'


def test_income_confirmed_generalises_to_grid_shape():
    # ASSUMPTION-PROOF #1: kind=None, but the income identity CONFIRMS on grid shape → eligible.
    ax, lc = _axlc(INCOME_CONFIRMED_NOKIND)
    assert extract._statement_kind(INCOME_CONFIRMED_NOKIND, 'x') is None      # not self-declared
    ok, kind = extract._grid_eligible(INCOME_CONFIRMED_NOKIND, ax, lc, 'x', 'revenue')
    assert ok and kind is None                                                # eligible via income-CONFIRMED


def test_junk_grid_never_eligible():
    # NEGATIVE CONTROL (the 63:6 risk): a kind=None non-income grid is NEVER a source.
    ax, lc = _axlc(JUNK_GRID)
    assert extract._statement_kind(JUNK_GRID, 'x') is None
    ok, _ = extract._grid_eligible(JUNK_GRID, ax, lc, 'x', 'cash')
    assert ok is False
    assert extract._is_statement_grid(JUNK_GRID, ax, lc, 'x') is False


# ── _grid_resource end to end ─────────────────────────────────────────────────────────────────────
def test_income_grid_emits_scenario_filtered():
    # POSITIVE: Budget filtered → clean Jan+Feb actuals → revenue 10+11 = 21 (INR Cr).
    fig = _resource('PL', INCOME_GRID, 'revenue', anchor_cr=Decimal('20'))
    assert isinstance(fig, Figure) and fig.confirmed
    assert fig.value_cr == 21
    assert 'grid statement source' in (fig.provenance.note or '')            # disclosure
    assert 'scope=Actual' in (fig.provenance.note or '')                     # scope column disclosed


def test_junk_grid_never_emits():
    # NEGATIVE CONTROL: junk grid → never a confirmed emit (held-candidate or None only).
    fig = _resource('Aging', JUNK_GRID, 'cash')
    assert not (isinstance(fig, Figure) and fig.confirmed)


def test_statement_shaped_unconfirmed_grid_is_held_not_skipped():
    # §4 GUARD: a statement-shaped grid that fails both routes is HELD-and-disclosed, never skipped.
    fig = _resource('Numbers', STMT_SHAPED_NOKIND, 'revenue')
    assert isinstance(fig, Figure) and fig.held and not fig.confirmed
    assert 'not kind-confirmed' in (fig.hold_reason or '')


def test_multiscope_grid_holds():
    # NEGATIVE CONTROL: eligible P&L grid but two scopes differ on the same period → HOLD (never guess).
    fig = _resource('PL', MULTISCOPE_GRID, 'revenue')
    assert not (isinstance(fig, Figure) and fig.confirmed)


def test_no_grid_carrying_concept_returns_none():
    fig = _resource('Aging', JUNK_GRID, 'revenue')       # junk grid has no revenue row
    assert fig is None


# ── CSS real-file reddening: the SGD currency path (green only once SGD is covered) ────────────────
IN = 'backend/media/preingest/trivesta/100e86d5/in'
CSS = '0525_CSS_Monthy_Report_-_Consol_Updated.xlsx'


@pytest.mark.skipif(not os.path.isfile(os.path.join(IN, CSS)), reason='real fixture file not present')
def test_css_summary_grid_emits_with_fixture_sgd_card():
    # REDDENING (fixture rate, not the real FX): the grid-flip exposes CSS 'Summary' (a self-declared
    # income GRID); with SGD covered it emits, and the emitted ₹Cr equals the fixture arithmetic
    # (native SGD × rate ÷ 1e7) — proving the emit is a CODE-read + convert, not a hardcoded number.
    sgd_rate = Decimal('63.5')
    rc = RateCard.from_input({'as_of': '2026-06-30', 'rates': [
        {'currency': 'SGD', 'inr_per_unit': str(sgd_rate), 'source': 'fixture'},
        {'currency': 'MYR', 'inr_per_unit': '18.6', 'source': 'fixture'}]})
    rec = extract.extract_company(CSS, os.path.join(IN, CSS), rate_card=rc, entity='CSS',
                                  anchor_cr=Decimal('5'), use_model=False)
    rev = rec.fields.get('revenue')
    assert isinstance(rev, Figure) and rev.confirmed
    assert rev.provenance.sheet == 'Summary'
    # native SGD revenue 1,107,554.42 → ₹Cr = value × 63.5 / 1e7
    expected = (Decimal('1107554.4189499998') * sgd_rate / Decimal('1e7')).quantize(Decimal('0.0001'))
    assert abs(rev.value_cr - expected) <= Decimal('0.01')
    # accuracy frontier: the emit must trace to the ACTUAL column (budget/variance would arithmetic-check
    # fine but be wrong) and the disclosure must NAME the scope — not just "grid statement source".
    assert rev.provenance.cell == 'C14'                                       # ACTUAL column (D14=Budget)
    assert 'scope=ACTUAL' in (rev.provenance.note or '')


@pytest.mark.skipif(not os.path.isfile(os.path.join(IN, CSS)), reason='real fixture file not present')
def test_css_summary_held_when_sgd_uncovered():
    # FAIL-CLOSED twin: same file, INR-only card → SGD uncovered → held, NEVER emitted unconverted.
    rec = extract.extract_company(CSS, os.path.join(IN, CSS), rate_card=default_inr_card('2026-06-30'),
                                  entity='CSS', anchor_cr=Decimal('5'), use_model=False)
    rev = rec.fields.get('revenue')
    assert not (isinstance(rev, Figure) and rev.confirmed)
