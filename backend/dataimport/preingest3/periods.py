"""
CARRY-FORWARD 1 — period-axis classification and collapse (a correctness
blocker: it decides the actual number). "Sum trailing-12 for flows" is correct
ONLY for discrete, non-overlapping monthly columns. Real MIS files break that
three ways, each producing a badly wrong number:

  • cumulative-YTD columns (each = running Apr→that month) — summing them
    double/triple counts; the answer is the LATEST column, not the sum.
  • a monthly series AND a YTD/Total column side by side (LDC) — must not sum both.
  • sub-12-month reality — CPC 5 months, CPM 6, Analisa 5, LDC/Aliste 11m YTD.
    A true TTM often does not exist; never fabricate one from partial data.

The classifier is EVIDENCE-BASED, not label-based: where a Total/YTD column
exists, the values themselves decide the axis type —
    Σ(series) ≈ total  → DISCRETE   (sum the window)
    last(series) ≈ total → CUMULATIVE (take the latest, do not sum)
    neither            → ESCALATE   (ambiguous — never guess)
This reuses the sum-of-periods = total identity as the discriminator. Stock
concepts (cash, headcount) are never summed — always the latest balance. Nature
comes from the concept definition (contract.CONCEPT_NATURE), not the data.
"""
from __future__ import annotations

import datetime as _dt
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

from .quantity import STOCK, FLOW, to_decimal

# period-column kinds
MONTH = 'month'
QUARTER = 'quarter'
YEAR = 'year'
YTD = 'ytd'
TOTAL = 'total'
UNKNOWN = 'unknown'

# axis outcomes
DISCRETE = 'discrete'
CUMULATIVE = 'cumulative'
SINGLE_TOTAL = 'single_total'
ESCALATE = 'escalate'

# Validated as-of (Increment 4, D1): a future-dated TYPO header (Clientell '2026-12-25' after a
# monthly run ending Feb-2026) is still MONOTONE, so a monotonicity check misses it — but it breaks
# the CADENCE (a ~10-month jump in a 1-month series). Cadence-regularity is the self-contained
# signal (needs no as-of); peel trailing columns whose step from the prior distinct period is ≥
# max(modal_cadence·FACTOR, FLOOR). Conservative: fires only when a DOMINANT small cadence exists,
# so a legitimate 2-3-month skip or a comparatives-only layout (CPC) is never peeled.
_CADENCE_BREAK_FACTOR = 4
_CADENCE_BREAK_FLOOR = 6

_MONTHS = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], start=1)}
_MONTH_RE = re.compile(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\b', re.I)
_QUARTER_RE = re.compile(r'\b(?:q([1-4])|([1-4])\s*q)\b', re.I)
_YTD_RE = re.compile(r'\b(ytd|year\s*to\s*date|cumulative|cumm?)\b', re.I)
# Frame markers that OVERRIDE a bare month name (bug fix): a month inside a cumulative/quarter frame is
# NOT a discrete month. `CY 2024 Act May` = calendar-year-to-date THROUGH May (a YTD), not May itself;
# `For QE 30th June 2025` = the quarter ENDING June, not June the month. Without this, the cumulative
# column takes the same (year, month) order as the real monthly column and collides with it. File-
# agnostic — recognises the frame WORDS, never a position/company.
_QTR_FRAME_RE = re.compile(r'\b(q\s*/?\s*e|quarter\s*end(?:ed|ing)?|quarter)\b', re.I)
_CUM_FRAME_RE = re.compile(r'\b(cy|c\.y\.|calendar\s*year|fiscal\s*year|financial\s*year|ytd|'
                           r'year\s*to\s*date|cumulative|cumm?|to\s*date)\b', re.I)
_TOTAL_RE = re.compile(r'\b(grand\s+total|total|full\s*year|fy\s*total)\b', re.I)
_FY_RE = re.compile(r'\bfy', re.I)                 # 'fy', 'fy25', 'fy2025' (attached digits ok)
_YEAR4_RE = re.compile(r'20\d{2}')                 # a 4-digit year anywhere
_YEAR2_RE = re.compile(r"(?<!\d)'?(\d{2})(?!\d)")  # a standalone 2-digit year


@dataclass
class PeriodColumn:
    col: int
    label: str
    kind: str
    months: int = 0
    order: tuple = (0, 0)      # (year, month) for chronological sort
    basis: str = ''            # reporting basis (Lever 1): '' | actual | budget | forecast | plan | revised | prior_year | variance


# ── reporting BASIS — Lever 1: header-band parsing ────────────────────────────────────────────────
# A statement column carries TWO independent header dimensions: the PERIOD (which month/quarter/year)
# and the BASIS (is this the reported ACTUAL, or a Budget / Forecast / Plan / Revised / Prior-year
# comparative). `parse_period_label` reads the period; `parse_basis` reads the basis from the SAME or an
# ADJACENT header cell. PRINCIPLE (file-agnostic — a general reading skill, never an "if file==X" rule):
# when two columns collide on the SAME period with different values, an EXPLICIT basis label breaks the
# tie — the ACTUAL is the figure; Budget / Forecast / Plan / Revised / Prior-year yield to it. If the
# basis cannot make the actual unique for a collision, we do NOT guess → the collision stands → hold.
ACTUAL = 'actual'
BUDGET = 'budget'
FORECAST = 'forecast'
PLAN = 'plan'
PRIOR_YEAR = 'prior_year'
VARIANCE = 'variance'
AMBIGUOUS = 'ambiguous'   # a recognised basis whose relation to "the reported actual" is NOT settled
# bases that CLEARLY YIELD to a same-period ACTUAL (a comparative the client did NOT report as actual).
# Deliberately CONSERVATIVE — only the unambiguous comparatives. Anything else colliding with an actual
# (VARIANCE, AMBIGUOUS, or an UNKNOWN '' label) leaves the collision UNRESOLVED → hold, never guess.
_YIELD_BASES = frozenset({BUDGET, FORECAST, PLAN, PRIOR_YEAR})
# ACTUAL is matched FIRST so 'Actual 2024' reads as ACTUAL (prior-year-ness is carried by the PERIOD,
# not the basis). AMBIGUOUS covers revised/restated/provisional/unaudited/management/proforma/normalised
# — these are NOT auto-yielded (a revised/restated ACTUAL may be the truer figure) and NOT auto-picked;
# in a collision they force a HOLD, pending CA review / T2-T3. This vocabulary is DOMAIN KNOWLEDGE — it is
# surfaced for the user (a CA) to confirm/extend; an unmatched label falls to '' which also holds.
_BASIS_PATTERNS = [
    (ACTUAL,     re.compile(r'\b(actuals?|actls?|act|reported)\b', re.I)),
    (BUDGET,     re.compile(r'\b(budget(?:ed)?|bdgt|bgt|bud)\b', re.I)),
    (FORECAST,   re.compile(r'\b(forecast(?:ed)?|projected|proj|fcst|fcast)\b', re.I)),
    (PLAN,       re.compile(r'\b(planned|plan|pln)\b', re.I)),
    (PRIOR_YEAR, re.compile(r'\b(prior\s*(?:year|yr)|previous\s*(?:year|yr)|last\s*year|py|ly)\b', re.I)),
    (AMBIGUOUS,  re.compile(r'\b(revised|revision|rev\s*est|re-?est|restated|provisional|prov|'
                            r'unaudited|management|mgmt|proforma|pro\s*forma|normalised|normalized)\b', re.I)),
    (VARIANCE,   re.compile(r'(\b(variance|var|growth|change|movement|mom|yoy|vs)\b|%)', re.I)),
]


def parse_basis(text) -> str:
    """Classify a header cell's reporting BASIS, or '' if it carries no basis token. File-agnostic:
    recognises the WORDS (actual/budget/forecast/plan/prior-year/ambiguous/variance), never a position
    or a company name. Returns '' for a plain period cell (e.g. 'Feb-25') so a period sub-row is never
    mistaken for a basis row. Case/whitespace-insensitive; an unrecognised label → '' (which also holds
    in a collision)."""
    s = str(text or '')
    if not s.strip():
        return ''
    for basis, pat in _BASIS_PATTERNS:
        if pat.search(s):
            return basis
    return ''


def _basis_resolved_cols(cols: List['PeriodColumn'], values: Dict[int, object], conflict: set):
    """Lever 1 resolution — break a same-period value collision using an EXPLICIT basis label.
    For each conflicting period: if exactly ONE colliding column is labelled ACTUAL and EVERY other
    colliding column carrying a (non-zero) value is a KNOWN non-actual basis (Budget/Forecast/Plan/
    Revised/Prior-year), the ACTUAL is the figure → drop the others for that period. If the basis
    cannot make the actual unique (no explicit actual, ≥2 actuals, or ANY colliding column has an
    UNKNOWN basis or is a variance column), the collision is left intact and the caller holds.
    Returns the SAME list object when nothing is resolved (so callers detect 'no change' by identity)."""
    drop = set()
    for kind, o in conflict:
        group = [c for c in cols
                 if c.kind == kind and c.order == o and to_decimal(values.get(c.col)) not in (None,)
                 and to_decimal(values.get(c.col)) != 0]
        actuals = [c for c in group if c.basis == ACTUAL]
        others = [c for c in group if c.basis != ACTUAL]
        if len(actuals) == 1 and others and all(c.basis in _YIELD_BASES for c in others):
            drop.update(c.col for c in others)
    return [c for c in cols if c.col not in drop] if drop else cols


def parse_period_label(text, col: int = 0) -> PeriodColumn:
    """Classify one column header cell into a period type. A date is a month; a
    quarter/FY/year/YTD/total is recognised by pattern; anything else UNKNOWN."""
    if isinstance(text, (_dt.datetime, _dt.date)):
        return PeriodColumn(col, str(text), MONTH, 1, (text.year, text.month))
    s = str(text or '').strip()
    low = s.lower()
    if not s:
        return PeriodColumn(col, s, UNKNOWN)
    # YTD / cumulative first (a 'YTD' column is cumulative even if it names a month)
    if _YTD_RE.search(low):
        yr = _year_of(low)
        return PeriodColumn(col, s, YTD, 0, (yr, 12))
    if _TOTAL_RE.search(low):
        return PeriodColumn(col, s, TOTAL, 12, (9999, 12))
    q = _QUARTER_RE.search(low)
    if q:
        qn = int(q.group(1) or q.group(2))
        return PeriodColumn(col, s, QUARTER, 3, (_year_of(low), qn * 3))
    m = _MONTH_RE.search(low)
    if m:
        mon = _MONTHS[m.group(1).lower()[:3]]
        # a month name inside a QUARTER frame ('QE June', 'quarter ended June') → the quarter ending
        # that month; inside a CUMULATIVE/year frame ('CY 2024 … May') → year-to-date, NOT a discrete
        # month. Only an EXPLICIT frame marker overrides; a plain 'May-24' stays a discrete month.
        if _QTR_FRAME_RE.search(low):
            return PeriodColumn(col, s, QUARTER, 3, (_year_of(low), mon))
        if _CUM_FRAME_RE.search(low):
            return PeriodColumn(col, s, YTD, 0, (_year_of(low), 12))
        return PeriodColumn(col, s, MONTH, 1, (_year_of(low), mon))
    if _FY_RE.search(low) or re.fullmatch(r'20\d{2}(\s*-\s*\d{2,4})?', low) or re.fullmatch(r"'?\d{2}", low):
        return PeriodColumn(col, s, YEAR, 12, (_year_of(low), 12))
    return PeriodColumn(col, s, UNKNOWN)


def _year_of(low: str) -> int:
    """A 4-digit year anywhere wins; else a standalone 2-digit year (handles
    attached forms like 'fy25' / 'q1fy25' that word boundaries would miss)."""
    m4 = _YEAR4_RE.search(low)
    if m4:
        return int(m4.group())
    m2 = _YEAR2_RE.search(low)
    return 2000 + int(m2.group(1)) if m2 else 0


@dataclass
class PeriodAxis:
    columns: List[PeriodColumn]     # the period columns, left→right
    axis_rows: List[int]            # the header row(s) the axis was read from
    is_comparison_grid: bool        # period tokens REPEAT (budget-vs-actual, FY-vs-FY, div×period)
    is_time_series: bool            # distinct, ordered periods (a real monthly/quarterly series)
    # ── STRUCTURE (additive; the include/exclude decision above is unchanged) ──────────────────────
    # A "grid" is not one thing: it can be a multi-division month-run (same months repeated per
    # division), a current│prior│variance triple, actual│budget scenarios, or FY-vs-FY comparatives.
    # These fields expose the column STRUCTURE so a selector can pick the right slice (latest actual
    # period, consolidated scope) instead of excluding the sheet. Empty/1.0 defaults keep every
    # existing construction site valid and every existing caller byte-identical (they read only the
    # four fields above).
    distinct_ratio: float = 1.0                                   # distinct periods / total columns
    period_groups: Dict[tuple, List[PeriodColumn]] = field(default_factory=dict)   # period order → its columns, left→right


def detect_period_axis(rows, r_start: int = 0, r_end: int = None, scan: int = 60) -> PeriodAxis:
    """Find a sheet region's TIME AXIS — the horizontal header row whose cells are
    period tokens (dates / months / quarters / years). Real financial sheets put
    the axis ONCE near the top; the line-item sections below all share it, so the
    axis is a SHEET/region property, not a per-section one (this is why looking
    only inside each section's own header found 0 columns on a 47-month P&L).

    Also classifies the axis:
      • is_comparison_grid — the same period appears MORE THAN ONCE across the
        columns (FY22-23 | FY21-22 | FY22-23 …, or budget|actual pairs). These
        must not be summed as a time series.
      • is_time_series — periods are distinct and orderable (a real Apr…Mar run).
    """
    n = len(rows)
    r_end = min(n, (r_end if r_end is not None else n), r_start + scan)
    best_row, best_cols = None, []
    for r in range(r_start, r_end):
        cols = {}
        for c, v in enumerate(rows[r]):
            pc = parse_period_label(v, c)
            if pc.kind != UNKNOWN:
                cols[c] = pc
        if len(cols) > len(best_cols):
            best_row, best_cols = r, sorted(cols.values(), key=lambda p: p.col)
    if not best_cols:
        return PeriodAxis([], [], False, False)
    # A time series has DISTINCT periods across its columns (ratio ≈ 1.0). A
    # comparison grid repeats a few periods many times (FY22|FY21|FY22|… → a
    # handful of distinct values over many columns → low ratio).
    distinct = len({(pc.kind, pc.order) for pc in best_cols})
    distinct_ratio = distinct / len(best_cols)
    is_grid = distinct_ratio < 0.6
    # Lever 1 (increment 1a) — read each period column's reporting BASIS from its OWN header cell first
    # (a combined 'May-25 Actual' cell), then from the BASIS sub-row directly beneath the period row (a
    # 'period' row over an 'Actual│Budget' row). File-agnostic; parse_basis returns '' for a plain
    # period/date/number, so a second period row or a data row is never mistaken for a basis row. A
    # merged group-BANNER above the period row ('Budget 2025' spanning columns) is increment 1b.
    sub = best_row + 1
    sub_row = rows[sub] if 0 <= sub < n else None
    for pc in best_cols:
        b = parse_basis(pc.label)
        if not b and sub_row is not None and pc.col < len(sub_row):
            b = parse_basis(sub_row[pc.col])
        if b:
            pc.basis = b
    groups: Dict[tuple, List[PeriodColumn]] = {}
    for pc in best_cols:                              # period order → its columns (left→right), for the selector
        groups.setdefault((pc.kind, pc.order), []).append(pc)
    return PeriodAxis(best_cols, [best_row], is_grid, not is_grid,
                      distinct_ratio=distinct_ratio, period_groups=groups)


@dataclass
class Collapsed:
    value: Optional[Decimal]
    basis: str                       # 'TTM' | 'YTD' | 'FY' | 'MTD' | 'point_in_time' | 'partial'
    months: int
    axis: str                        # DISCRETE | CUMULATIVE | SINGLE_TOTAL | ESCALATE
    escalate: bool = False
    flags: List[str] = field(default_factory=list)
    reason: str = ''
    source_cols: List[int] = field(default_factory=list)  # column indices whose cells RECONSTRUCT
    #   the value (provenance): [latest] for a stock, [total] for a stated total, the summed run for
    #   a flow. Empty on escalate. extract turns these into A1 cell refs for Figure.provenance.


def _tol(target: Decimal, rel=Decimal('0.02')) -> Decimal:
    return max(abs(target) * rel, Decimal('0.01'))


def _period_outlier_cols(series: List[PeriodColumn]) -> set:
    """Column indices of TRAILING cadence-break outliers in a MONTH/QUARTER series — a future-dated
    typo header that is still monotone (Clientell '2026-12-25' after Feb-2026) but jumps far past the
    regular spacing. Self-contained (no as-of): establish the DOMINANT small cadence (modal gap between
    consecutive distinct periods), then peel trailing distinct periods whose gap ≥ max(modal·FACTOR,
    FLOOR). Returns {} when no reliable cadence exists (comparatives-only, <3 periods) so nothing is
    peeled — the validated latest then falls back to raw max, unchanged. Never removes an interior
    period; only the anomalous tail an as-of would otherwise land on."""
    ordered = sorted(series, key=lambda c: c.order)
    idx = lambda c: c.order[0] * 12 + c.order[1]
    distinct = sorted({idx(c) for c in ordered})
    if len(distinct) < 3:
        return set()
    gaps = [b - a for a, b in zip(distinct, distinct[1:])]
    modal, freq = Counter(gaps).most_common(1)[0]
    if modal <= 0 or modal > 3 or freq < len(gaps) / 2:   # no reliable monthly/quarterly cadence
        return set()
    thresh = max(modal * _CADENCE_BREAK_FACTOR, _CADENCE_BREAK_FLOOR)
    drop, d = set(), list(distinct)
    while len(d) >= 2 and d[-1] - d[-2] >= thresh:
        drop.add(d.pop())
    return {c.col for c in series if idx(c) in drop}


def _conflicting_periods(cols: List[PeriodColumn], values: Dict[int, object]) -> set:
    """Periods (orders) carrying ≥2 columns with DIFFERENT non-zero values — an UNLABELED plan/actual
    (or restated) overlap the scenario filter didn't catch (Increment 4, D3 guard A). CPM's 'PL' is
    such a grid: a 2025-datetime block and a 2025-'Feb-25'-string block hold different values for the
    same month with NO banner to say which is actual. There is no deterministic evidence to pick one,
    so the emit must HOLD — guessing risks shipping budget as actual (the worst failure class). A period
    with a value and its zero/blank duplicate is NOT a conflict (that resolves to the non-empty one)."""
    # Kind-aware (bug fix): a collision is ≥2 columns of the SAME KIND at the same order carrying
    # different non-zero values (two rival MONTH actuals, two rival YTDs …). A MONTH and a QUARTER/YTD
    # at the same order are DIFFERENT granularities (a month vs a cumulative that ends in/contains it),
    # never rival actuals — comparing their values is meaningless, so it is NOT a plan/actual overlap.
    # Returns a set of (kind, order) keys; callers test (c.kind, c.order).
    by = {}
    for c in cols:
        v = to_decimal(values.get(c.col))
        if v is not None and v != 0:
            by.setdefault((c.kind, c.order), set()).add(v)
    return {ko for ko, vs in by.items() if len(vs) > 1}


def collapse(concept: str, nature: str, columns: List[PeriodColumn],
             values: Dict[int, object], *, as_of_year: int = None,
             as_of: tuple = None, require_bound: bool = False) -> Collapsed:
    """Collapse a concept's per-column values into ONE figure.

    `columns` — parsed period columns for the statement.
    `values`  — {col_index: raw cell value} for THIS concept's row.
    `as_of`   — the source's own STATED reporting boundary as (year, month), when known
                (from a header 'as on' cell or the filename month). Columns dated strictly
                after it are forward projections, not actuals — see the kind-aware guard below.
    `require_bound` — the caller COULD NOT establish any reporting boundary (no filename month,
                no flow-derived date) and demands a fail-CLOSED outcome: a latest/summed MONTH
                selection that cannot be proven to be an actual (rather than a projection) HOLDS
                rather than proceed unguarded. The library DEFAULT (False) keeps the old behaviour;
                production (extract_company) sets it so fail-open is never the floor.
    Nature drives the branch: STOCK → latest balance (never summed); FLOW →
    evidence-based axis classification.
    """
    # keep only columns we have a numeric value for
    cols = [c for c in columns if to_decimal(values.get(c.col)) is not None]
    if not cols:
        return Collapsed(None, 'point_in_time', 0, ESCALATE, True,
                         reason='no parseable period columns with values — escalate')
    # Validated as-of (D1): drop trailing cadence-break typo columns BEFORE either branch picks the
    # latest — a future-dated typo would otherwise win the stock `max` and anchor the flow walk-back.
    outliers = _period_outlier_cols([c for c in cols if c.kind in (MONTH, QUARTER)])
    if outliers:
        cols = [c for c in cols if c.col not in outliers]
    # Stated as-of bound (Rung 2): a DISCRETE month dated strictly AFTER the source's own stated
    # reporting as-of is a forward projection, never an actual of that report — exclude it from BOTH
    # the stock latest-pick and the flow sum (the label-based scenario filter misses an UNLABELED
    # future month like 'Mar'26'; the cadence-peel misses a plausibly-spaced one). SCOPED TO kind==
    # MONTH by design: a month's `order` is a true point-in-time, so `> as_of` is meaningful. A
    # cumulative span carries a SORT-SENTINEL order (YTD→(yr,12), TOTAL→(9999,12), YEAR→(yr,12)) and a
    # QUARTER's order is its end-month (Q4→(yr,3)) which can STRADDLE the as-of — comparing either to a
    # month would false-drop an ACTUAL (a YTD-through-as-of, a straddling partial quarter). Fail-open:
    # no as_of, or the bound would empty the axis (a mislabeled/too-early as_of) → columns unchanged.
    if as_of is not None:
        kept = [c for c in cols if not (c.kind == MONTH and c.order != (0, 0) and c.order > as_of)]
        if kept:
            cols = kept
    # Fail-closed floor (Rung-2, the agreed ladder's bottom rung): if NO reporting boundary could be
    # established (as_of is None) and the caller REQUIRES one, a latest/summed MONTH selection cannot be
    # proven to be an actual rather than a projection → HOLD, never proceed unguarded. Fires only on
    # genuine ambiguity (≥2 distinct MONTH periods); a single month or a cumulative-only axis has no
    # trailing projection to rule out, so it still emits. This is why fail-open is reserved for NEVER.
    if as_of is None and require_bound:
        month_orders = {c.order for c in cols if c.kind == MONTH and c.order != (0, 0)}
        if len(month_orders) >= 2:
            return Collapsed(None, 'point_in_time', 0, ESCALATE, True,
                             flags=['unbounded_projection_risk'],
                             reason=('no reporting as-of (filename/flow-derived) to rule out a projection '
                                     'in the latest month column — hold (fail-closed floor)'))
    val = lambda c: to_decimal(values.get(c.col))
    # Unlabeled plan/actual overlap (D3, guard A): fail-closed. Scoped to the emit-relevant periods so
    # a benign old conflict far from the figure never causes a false hold.
    conflict = _conflicting_periods(cols, values)
    # Lever 1 (header-band parsing): a collision carrying an EXPLICIT basis label (Actual vs Budget/
    # Forecast/Plan/Revised/Prior-year) is not unlabeled — resolve it to the ACTUAL rather than hold.
    # This runs ONLY when a conflict already exists, so a currently-emitted (non-conflicting) figure is
    # untouched: byte-identical by construction. Unresolved collisions fall through to the holds below.
    if conflict:
        reduced = _basis_resolved_cols(cols, values, conflict)
        if reduced is not cols:
            cols = reduced
            conflict = _conflicting_periods(cols, values)

    # ── STOCK: always the latest balance, never summed ──────────────────
    if nature == STOCK:
        series = [c for c in cols if c.kind in (MONTH, QUARTER, YEAR)]
        pick = max(series or cols, key=lambda c: c.order)
        if (pick.kind, pick.order) in conflict:
            return Collapsed(None, 'point_in_time', 0, ESCALATE, True, flags=['unlabeled_period_overlap'],
                             reason=(f'as-of {pick.label!r} has ≥2 differing values with no disambiguating '
                                     f'label (unlabeled plan/actual overlap) — cannot pick, hold'))
        return Collapsed(val(pick), 'point_in_time', 0, SINGLE_TOTAL,
                         reason=f'stock → latest column {pick.label!r}', source_cols=[pick.col])

    # ── FLOW ────────────────────────────────────────────────────────────
    series = sorted([c for c in cols if c.kind in (MONTH, QUARTER)], key=lambda c: c.order)
    totals = [c for c in cols if c.kind in (TOTAL, YTD, YEAR)]
    wc = conflict & {(c.kind, c.order) for c in series}
    if wc:                                       # a summed FLOW period carries conflicting values → hold
        return Collapsed(None, 'point_in_time', 0, ESCALATE, True, flags=['unlabeled_period_overlap'],
                         reason=(f'{len(wc)} period(s) carry ≥2 differing values with no disambiguating '
                                 f'label (unlabeled plan/actual overlap) — cannot sum, hold'))

    # A stated total/YTD column present → let the VALUES classify the axis.
    if totals and series:
        total_col = max(totals, key=lambda c: c.order)
        total_val = val(total_col)
        s_sum = sum((val(c) for c in series), Decimal('0'))
        s_last = val(series[-1])
        if abs(s_sum - total_val) <= _tol(total_val):
            return _flow_from_series(concept, val, series, DISCRETE, additive_confirmed=True,
                                     note=f'Σseries≈total ({total_col.label!r}) → discrete')
        if abs(s_last - total_val) <= _tol(total_val):
            return Collapsed(total_val, 'YTD', _ytd_months(total_col, as_of_year), CUMULATIVE,
                             flags=['cumulative_take_latest'], source_cols=[total_col.col],
                             reason=f'last≈total → cumulative; took {total_col.label!r}')
        # PARTIAL recent-months display + an EXPLICIT same-year YTD column: the shown
        # months are a subset (Σseries < YTD), so the YTD column IS the year-to-date
        # figure — READ it (the column self-declares YTD and its year matches the
        # latest shown month). This is NOT a guess and canNOT fire on multi-year /
        # overlapping layouts: those have Σseries ≥ total (Aliste) or a different-year
        # total (Analisa), which still escalate below.
        if (total_col.kind == YTD and total_col.order[0]
                and total_col.order[0] == series[-1].order[0]
                and s_sum < total_val - _tol(total_val)):
            return Collapsed(total_val, 'YTD', _ytd_months(total_col, as_of_year), SINGLE_TOTAL,
                             flags=['partial_series_same_year_ytd_taken'], source_cols=[total_col.col],
                             reason=(f'{len(series)} recent month(s) are a partial subset of the '
                                     f'same-year YTD column {total_col.label!r} → took YTD'))
        return Collapsed(None, 'YTD', 0, ESCALATE, True,
                         flags=['axis_ambiguous'],
                         reason=(f'neither Σseries ({s_sum}) nor last ({s_last}) matches '
                                 f'total {total_val} — escalate, do not guess'))

    # Only a total/YTD column, no monthly series → use it directly.
    if totals and not series:
        tc = max(totals, key=lambda c: c.order)
        basis = 'YTD' if tc.kind == YTD else 'FY'
        return Collapsed(val(tc), basis, _ytd_months(tc, as_of_year) if tc.kind == YTD else 12,
                         SINGLE_TOTAL, reason=f'single {tc.kind} column {tc.label!r}', source_cols=[tc.col])

    # Only a monthly/quarterly series, no stated total.
    if series:
        return _flow_from_series(concept, val, series, DISCRETE)

    return Collapsed(None, 'point_in_time', 0, ESCALATE, True,
                     reason='unclassifiable period axis — escalate')


def _ytd_months(col: PeriodColumn, as_of_year) -> int:
    """Months covered by a YTD column ending at its own month (Apr→that month).
    Unknown → 0 (downstream must not annualise a 0-month YTD without a flag)."""
    return col.order[1] if col.order[1] and col.order[1] <= 12 else 0


def _true_ttm_window(series: List[PeriodColumn]) -> Optional[List[PeriodColumn]]:
    """The trailing sub-series that forms an HONEST 12-month window — 12 distinct,
    CONSECUTIVE months (or 4 consecutive quarters) — else None.

    A TTM tag is only honest if the summed columns are one unbroken 12-month run.
    Σ(c.months) ≥ 12 is NOT sufficient: it also passes when the columns span two
    fiscal years, overlap (duplicated periods), or have gaps — each of which
    carries a WRONG-WINDOW figure under a 'TTM' label. Walk back from the latest
    column accumulating exactly 12 months, then require the window to be distinct
    and evenly spaced by each column's own length (contiguous)."""
    if not series:
        return None
    ordered = sorted(series, key=lambda c: c.order)
    window, total = [], 0
    for c in reversed(ordered):
        window.append(c)
        total += c.months
        if total >= 12:
            break
    if total != 12:
        return None                          # 5m/6m partial, or an irregular overshoot
    window = list(reversed(window))
    idx = [c.order[0] * 12 + c.order[1] for c in window]
    if len(set(idx)) != len(idx):
        return None                          # duplicate / overlapping columns
    for k in range(1, len(window)):
        if idx[k] - idx[k - 1] != window[k].months:
            return None                      # a gap or irregular jump → not consecutive
    return window


def _trailing_consecutive_run(series: List[PeriodColumn]) -> List[PeriodColumn]:
    """The maximal TRAILING run of columns that are truly consecutive — each step
    exactly one column-length before the next (Jan,Feb,Mar… or Q1,Q2,Q3,Q4).
    A COMPARATIVE layout (e.g. May-2025 | Dec-2024 | May-2024: a current period plus
    prior-year snapshots) is NOT a summable series — the run collapses to just the
    latest column, so comparatives are never summed as if they were a monthly run.
    This is the twin of _true_ttm_window: consecutiveness gate for ANY length."""
    if not series:
        return []
    ordered = sorted(series, key=lambda c: c.order)
    key = lambda c: c.order[0] * 12 + c.order[1]
    run = [ordered[-1]]
    for k in range(len(ordered) - 1, 0, -1):
        cur, prev = ordered[k], ordered[k - 1]
        if key(cur) - key(prev) == cur.months:      # prev is exactly one period before cur
            run.insert(0, prev)
        else:
            break                                   # a gap / different-year comparative → stop
    return run


def _flow_from_series(concept, val, series, axis, note='', additive_confirmed=False) -> Collapsed:
    """A discrete flow sum. An honest, consecutive 12-month window → real TTM; a
    shorter run → a PARTIAL figure carried WITH its true month count. NON-consecutive
    comparative columns (prior-year/period snapshots) are NOT summed — the run
    collapses to the latest period — UNLESS a stated total already confirmed the
    series is additive (`additive_confirmed`), in which case every component sums.
    Downstream annualisation (Quantity ×12/months) is then explicit and flagged."""
    flags = []
    window = _true_ttm_window(series)
    if window is not None:
        w_sum = sum((val(c) for c in window), Decimal('0'))
        if len(window) != len(series):
            flags.append('trailing_12_of_more')
        return Collapsed(w_sum, 'TTM', 12, axis, flags=flags,
                         reason=note or f'{len(window)} consecutive periods → true TTM sum',
                         source_cols=[c.col for c in window])
    summable = series if additive_confirmed else _trailing_consecutive_run(series)
    run_sum = sum((val(c) for c in summable), Decimal('0'))
    run_months = sum(c.months for c in summable)
    dropped = len(series) - len(summable)
    if dropped:
        flags.append(f'{dropped}_non_consecutive_comparative_cols_not_summed')
    flags.append(f'partial_{run_months}m_not_ttm')
    reason = note or (
        f'{run_months} consecutive month(s) present'
        + (f', {dropped} non-consecutive comparative column(s) dropped' if dropped else '')
        + ' — carried as partial, not a fabricated TTM')
    return Collapsed(run_sum, 'partial', run_months, axis, flags=flags, reason=reason,
                     source_cols=[c.col for c in summable])
