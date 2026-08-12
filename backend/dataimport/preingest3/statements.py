"""
Semantic chunking — segment a sheet into REGIONS, each classified as either a
STATEMENT (few named-metric rows → locate with the model) or a LEDGER (a long,
homogeneous record listing → code-aggregate, never send). The STATEMENT is the
unit of a locator call; the LEDGER is summed in code. This is what makes
row-explosion structurally impossible and keeps token spend flat.

Two universal, structural rules (no thresholds tuned to one file):

  1. A HEADER ROW HAS LABELS, NOT VALUES — it carries column names and no
     numbers. Data rows have numbers, so a data row can never be mistaken for a
     header. (Before this rule, 5,000 transaction rows in a SAP feeder each
     looked like a header and spawned a "statement".)

  2. A REGION IS A LEDGER IF IT IS LONG AND ROW-HOMOGENEOUS — most of its rows
     share one column-type signature (text,text,num,date,…). This catches a
     2-column unique-id ledger AND a wide repeated-category SAP feeder alike,
     because both are homogeneous listings; a financial statement's rows are
     heterogeneous (a label row, subtotals, blanks) and short, so it is never
     mis-flagged. Classified per-region, so a sheet mixing a P&L with a long
     ledger keeps the P&L locatable.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, List, Optional

from .profiler import _cell_type

STATEMENT = 'statement'
LEDGER = 'ledger'

MIN_DATA_ROWS = 2          # a statement needs a header + at least this many data rows
# Cost/size floor only — NOT the ledger discriminator. Below this, even a
# record-listing is small enough to send to the model safely, so there is no
# need to aggregate. The correctness discriminator is the transaction signature
# below (date + repeating-categorical dimensions), which is what actually keeps
# a detailed statement from being mis-aggregated regardless of this floor.
LEDGER_MIN_ROWS = 40
HOMOGENEITY = 0.5          # dominant row-signature fraction marking a record listing
CATEGORICAL_CARDINALITY = 0.7   # a text col repeating below this ratio is a dimension
SIG_COLS = 14              # columns considered when computing a row's type signature

# Tripwire — a safety net that fires on the EXPLOSION SIGNATURE, not a raw count
# (a raw count punishes a legitimately rich workbook — the patch-in-disguise).
# The explosion's real cause is a FLOOD OF DEGENERATE 1–2 row "statements" (data
# rows misread as headers). So a sheet trips only when it has MANY statements
# AND MOST of them are degenerate. Calibrated from real files (worst legitimate
# degenerate-fraction was 0.28) vs an explosion (~1.0). And it QUARANTINES the
# single offending sheet — disclosed as a held gap — while the other sheets and
# files keep processing; it never halts the whole run (same isolate-and-continue
# philosophy as the per-call timeout).
DEGENERATE_MAX_ROWS = 2
FLOOD_MIN_STATEMENTS = 50
FLOOD_DEGENERATE_FRACTION = 0.6

_MONTHS = ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec')
_PERIOD_RE = re.compile(r'^(q[1-4]|h[12]|fy\s?\d{2,4}|cy\s?\d{2,4}|[1-4]\s?q|ytd|mtd|ttm)\b', re.I)


def _is_year(v) -> bool:
    return isinstance(v, (int, float)) and float(v).is_integer() and 1990 <= v <= 2099


def _is_period_token(v) -> bool:
    """A column IDENTIFIER that names a period rather than measuring one: a date,
    a year (2024), a quarter/half/FY tag (Q1, FY25), or a month name (Jan-25).
    These legitimately appear IN header rows — the reason 'a header has no
    numbers' was wrong."""
    t = _cell_type(v)
    if t == 'date':
        return True
    if t == 'num':
        return _is_year(v)
    if t == 'text':
        s = str(v).strip().lower()
        return bool(_PERIOD_RE.match(s)) or any(m in s for m in _MONTHS)
    return False


def _is_measure(v) -> bool:
    """A numeric VALUE being measured (an amount) — a number that is not a year.
    A row containing a measure is a data row, never a header."""
    return _cell_type(v) == 'num' and not _is_year(v)


@dataclass
class Statement:
    sheet: str
    start_row: int          # 0-based, inclusive (header row for a statement)
    end_row: int            # 0-based, inclusive
    header_row: Optional[int]
    label_col: Optional[int]
    title: str = ''
    kind: str = STATEMENT   # STATEMENT | LEDGER

    @property
    def rows_range(self) -> range:
        return range(self.start_row, self.end_row + 1)


def _row_counts(row: List[Any]):
    text = num = 0
    for v in row:
        t = _cell_type(v)
        if t == 'text':
            text += 1
        elif t == 'num':
            num += 1
    return text, num


def _is_blank(row: List[Any]) -> bool:
    return not row or all(_cell_type(v) == 'blank' for v in row)


def _is_header(row: List[Any]) -> bool:
    """A header row: ≥2 label-like cells and NO measure values. 'Label-like'
    means text OR a period token (year / quarter / month / date), so a period
    header like `2024 2025 2026` or `Particulars | Jan-25 | Feb-25` is correctly
    a header. A row containing any measured amount is a data row, never a header."""
    nonblank = [v for v in row if _cell_type(v) != 'blank']
    if len(nonblank) < 2:
        return False
    if any(_is_measure(v) for v in nonblank):
        return False
    # at least one cell must be an actual label-like token (text or period)
    return any(_cell_type(v) == 'text' or _is_period_token(v) for v in nonblank)


def _row_signature(row: List[Any]):
    return tuple(_cell_type(v) for v in row[:SIG_COLS])


def _data_rows(rows, span) -> List[int]:
    return [r for r in span if r < len(rows) and not _is_blank(rows[r])]


def _dominant_col_type(rows, col: int, start: int) -> str:
    counts = {'num': 0, 'text': 0, 'date': 0}
    for r in range(start, len(rows)):
        if col < len(rows[r]):
            t = _cell_type(rows[r][col])
            if t in counts:
                counts[t] += 1
    return max(counts, key=counts.get) if any(counts.values()) else 'blank'


def _text_cardinality(rows, col: int, start: int) -> float:
    vals = [str(rows[r][col]).strip().lower() for r in range(start, len(rows))
            if col < len(rows[r]) and _cell_type(rows[r][col]) == 'text']
    return (len(set(vals)) / len(vals)) if vals else 1.0


def _is_ledger_span(rows, span) -> bool:
    """A raw transaction ledger to code-aggregate — distinguished from a
    (possibly long, detailed) STATEMENT by its TRANSACTION SIGNATURE, not by row
    count:

      long + row-homogeneous
      AND has a per-row DATE column (transaction dates)
      AND has ≥1 repeating-categorical text dimension beyond the label column
          (department / brand / type — low-cardinality repeats).

    A detailed P&L is label + numeric measure/period columns only — no date
    dimension, no repeating categorical — so it is NEVER swallowed, even at 50+
    lines. Biased to under-flag: an un-flagged listing is still safe (bounded
    window + the relevance filter skips it), whereas over-flagging LOSES data.
    """
    data = _data_rows(rows, span)
    if len(data) < LEDGER_MIN_ROWS:
        return False
    sigs = Counter(_row_signature(rows[r]) for r in data)
    _top_sig, top_n = sigs.most_common(1)[0]
    if (top_n / len(data)) < HOMOGENEITY:
        return False

    start = span.start
    n_cols = max((len(rows[r]) for r in span if r < len(rows)), default=0)
    label_col = _label_col_for(rows, span, None)
    has_date_dim = False
    categorical_dims = 0
    for c in range(n_cols):
        if c == label_col:
            continue
        dt = _dominant_col_type(rows, c, start)
        if dt == 'date':
            has_date_dim = True
        elif dt == 'text' and _text_cardinality(rows, c, start) < CATEGORICAL_CARDINALITY:
            categorical_dims += 1        # a repeating dimension (dept/brand/type)
    return has_date_dim and categorical_dims >= 1


def _label_col_for(rows, span: range, header_row: Optional[int]) -> Optional[int]:
    start = (header_row + 1) if header_row is not None else span.start
    n_cols = max((len(rows[r]) for r in span if r < len(rows)), default=0)
    best, best_score = None, 0
    for c in range(min(n_cols, 8)):
        texts = sum(1 for r in range(start, span.stop)
                    if r < len(rows) and c < len(rows[r]) and _cell_type(rows[r][c]) == 'text')
        if texts > best_score:
            best, best_score = c, texts
    return best


def _title_above(rows, header_row: int, floor: int) -> str:
    r = header_row - 1
    while r >= floor and _is_blank(rows[r]):
        r -= 1
    if r >= floor:
        texts = [str(v).strip() for v in rows[r] if _cell_type(v) == 'text']
        if len(texts) == 1:
            return texts[0]
    return ''


def _trim_trailing_blanks(rows, start: int, end: int) -> int:
    while end > start and _is_blank(rows[end]):
        end -= 1
    return end


def segment_regions(sheet: str, rows: List[List[Any]], blocks=None) -> List[Statement]:
    """Segment a sheet into regions DELIMITED BY HEADER ROWS — never by blank
    rows. Per the project's core rule, blank rows are visual spacing WITHIN a
    section (funds use 3–10 consecutive blanks as separators); reading continues
    through them. A statement therefore runs from its header to the row before
    the NEXT header (blanks and all), so a hierarchy like 'Automation → B2C/B2B'
    stays one statement instead of shattering into fragments.

    Each resulting region is classified LEDGER (long transaction listing →
    code-aggregate) or STATEMENT (→ locate). A header-less leading region with
    real data is still emitted (some sheets start data before any header)."""
    n = len(rows)
    headers = [r for r in range(n) if not _is_blank(rows[r]) and _is_header(rows[r])]
    # Collapse CONSECUTIVE header rows into one compound header group — real
    # statements routinely stack 2–4 header rows (period hierarchy over a
    # metric label over sub-columns). Each group heads ONE statement; the data
    # begins after the LAST row of the group. Without this the upper header rows
    # become 0-data fragments and are dropped, and column/period detection reads
    # the wrong header line.
    groups: List[List[int]] = []
    for hr in headers:
        if groups and hr == groups[-1][-1] + 1:
            groups[-1].append(hr)
        else:
            groups.append([hr])

    bounds = []   # (start, header_row|None, end)
    if not groups:
        if _data_rows(rows, range(0, n)):
            bounds.append((0, None, n - 1))
    else:
        if groups[0][0] > 0 and _data_rows(rows, range(0, groups[0][0])):
            bounds.append((0, None, _trim_trailing_blanks(rows, 0, groups[0][0] - 1)))
        for i, grp in enumerate(groups):
            gstart, col_header = grp[0], grp[-1]   # span from first; columns on last
            end = (groups[i + 1][0] - 1) if i + 1 < len(groups) else (n - 1)
            bounds.append((gstart, col_header, _trim_trailing_blanks(rows, gstart, end)))

    out: List[Statement] = []
    for (s, hr, e) in bounds:
        span = range(s, e + 1)
        data_span = range((hr + 1) if hr is not None else s, e + 1)
        if _is_ledger_span(rows, span):
            out.append(Statement(sheet, s, e, hr,
                                 _label_col_for(rows, span, hr), kind=LEDGER))
        elif len(_data_rows(rows, data_span)) >= MIN_DATA_ROWS:
            out.append(Statement(sheet, s, e, hr,
                                 _label_col_for(rows, span, hr),
                                 _title_above(rows, s, 0), kind=STATEMENT))
    return out


def segment_statements(sheet: str, rows: List[List[Any]], blocks=None) -> List[Statement]:
    """Only the STATEMENT regions (what the locator runs on)."""
    return [r for r in segment_regions(sheet, rows, blocks) if r.kind == STATEMENT]


def ledger_regions(sheet: str, rows: List[List[Any]], blocks=None) -> List[Statement]:
    return [r for r in segment_regions(sheet, rows, blocks) if r.kind == LEDGER]


def sheet_statement_stats(rows: List[List[Any]], regions: List[Statement]) -> dict:
    """Per-sheet signature: how many statements, and how many are degenerate
    (≤ DEGENERATE_MAX_ROWS data rows). A flood of degenerate fragments is the
    fingerprint of the header-misdetection explosion."""
    stmts = [g for g in regions if g.kind == STATEMENT]
    degenerate = 0
    for st in stmts:
        start = (st.header_row + 1) if st.header_row is not None else st.start_row
        if len(_data_rows(rows, range(start, st.end_row + 1))) <= DEGENERATE_MAX_ROWS:
            degenerate += 1
    n = len(stmts)
    return {'n': n, 'degenerate': degenerate,
            'degenerate_fraction': (degenerate / n) if n else 0.0}


def segmentation_health(per_sheet_stats: dict) -> dict:
    """Tripwire — fires on the explosion SIGNATURE (many statements AND mostly
    degenerate), and QUARANTINES the offending sheet rather than halting the run.
    Never drops data silently: a quarantined sheet is a disclosed held gap.

    `per_sheet_stats` — {sheet: stats-dict from sheet_statement_stats}. Returns
    {'ok', 'quarantine': [sheet,...], 'details': [...]}. The pipeline holds the
    quarantined sheets for review and keeps processing everything else."""
    quarantine, details = [], []
    for sheet, st in per_sheet_stats.items():
        if st['n'] > FLOOD_MIN_STATEMENTS and st['degenerate_fraction'] > FLOOD_DEGENERATE_FRACTION:
            quarantine.append(sheet)
            details.append(
                f"SEGMENTATION ANOMALY on {sheet!r}: {st['n']} statements, "
                f"{st['degenerate_fraction']:.0%} degenerate (> {FLOOD_DEGENERATE_FRACTION:.0%}) "
                f"— quarantined & disclosed; other sheets continue.")
    return {'ok': not quarantine, 'quarantine': quarantine, 'details': details}
