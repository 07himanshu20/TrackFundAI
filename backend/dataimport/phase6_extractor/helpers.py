"""
Universal row-shape helpers — no fund-specific behavior.

Detects:
  - junk rows       (TOTAL/SUBTOTAL/NOTES/DISCLAIMER)
  - section titles  (single-cell text label announcing a sub-section)
  - the header row  (row with the most text cells among the first N)
  - entity ID headers (LP001, PC-005, INV-14, F14C001)
  - period headers  (Apr-24, Q1-25, FY24)
"""
import re
from decimal import Decimal
from typing import Any


# Junk rows — subtotals, notes, disclaimers, sheet annotations
_JUNK_RE = re.compile(
    r'^(grand\s*)?(sub[\s\-]*)?total\s*[:\-]?\s*$|'
    r'^cumulative\s*total|'
    r'^t\s*o\s*t\s*a\s*l\s*$|'
    r'^total\s+(called|exits|distributions|investors|lps|committed|drawdown)\b|'
    r'^totals?\s*[:\-]?\s*$|'
    r'^notes?\s*[:\-\(]|'
    r'^summary\s*[:\-]?\s*$|'
    r'^disclaimer|'
    r'^important\s*[:\-]|'
    r'^column\s+[\'"].*shaded|'
    r'^col\s+[a-z]\s+[a-z0-9]+\s*=|'  # Col I Drawn% = H/F formula
    r'^\*+',
    re.I,
)

_SECTION_TITLE_PREFIX = re.compile(
    r'^[a-z]\.\s+',  # "A. SEBI IDENTIFIERS", "B. SCHEME LIFECYCLE"
    re.I,
)


def _cell_str(v: Any) -> str:
    if v is None:
        return ''
    if isinstance(v, float) and v != v:
        return ''
    return str(v).strip()


def row_non_empty(row: tuple) -> list[tuple[int, Any]]:
    return [(i, v) for i, v in enumerate(row)
            if v not in (None, '')
            and not (isinstance(v, float) and v != v)]


def is_junk_row(row: tuple) -> bool:
    cells = row_non_empty(row)
    if not cells:
        return False
    first_text = _cell_str(cells[0][1]).lower()
    return bool(_JUNK_RE.match(first_text))


def is_section_title_row(row: tuple) -> bool:
    """True for a row with exactly one non-empty text cell that reads as a
    label (e.g. 'A. SEBI IDENTIFIERS')."""
    cells = row_non_empty(row)
    if len(cells) != 1:
        return False
    _, val = cells[0]
    text = _cell_str(val)
    if not text or len(text) > 120:
        return False
    if isinstance(val, (int, float, Decimal)):
        return False
    if hasattr(val, 'isoformat'):
        return False
    if not re.search(r'[a-z]', text):
        return False
    return True


def find_header_row(rows: list[tuple], scan_upto: int = 20) -> int:
    """Return the row index of the header — the row with the most text cells."""
    best = -1
    best_score = 0
    for ri in range(min(len(rows), scan_upto)):
        cells = row_non_empty(rows[ri])
        if len(cells) < 3:
            continue
        text_cells = sum(
            1 for _, v in cells
            if not isinstance(v, (int, float, Decimal))
            and not hasattr(v, 'isoformat')
            and _cell_str(v)
        )
        if text_cells >= 3 and text_cells > best_score:
            best_score = text_cells
            best = ri
    return best


# ── Entity ID headers (LP001, PC-005, F14C001, INV-14) ──────────────────────
# Universal AIF convention: portfolio company IDs, LP IDs and investment IDs
# all follow a "prefix + digits" pattern with optional hyphen/space.
_ENTITY_ID_HEADER_RE = re.compile(
    r'^\s*(?:lp|inv|pc|co|entity|acct|account|f\d*c)[\s_\-]*'
    r'(\d+|[a-z]\d+|[ivx]+)'
    r'(?:\s*\([^)]*\))?\s*$',
    re.I,
)


def is_entity_id_header(text: str) -> bool:
    if not text:
        return False
    return bool(_ENTITY_ID_HEADER_RE.match(str(text).strip()))


# ── Period headers (Apr-24, Q1-25, FY24, 2024) ──────────────────────────────
_PERIOD_HEADER_RE = re.compile(
    r'^(?:'
    r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*[\s\-/]?\d{2,4}'
    r'|q[1-4][\s\-/]?(?:fy)?\s*\d{2,4}'
    r'|fy\s*\d{2,4}(?:[\s\-/]\d{2,4})?'
    r'|\d{4}[\s\-/]\d{1,2}'
    r'|\d{4}'
    r')$',
    re.I,
)


def is_period_header(text: str) -> bool:
    if not text:
        return False
    return bool(_PERIOD_HEADER_RE.match(str(text).strip()))


def slug(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', str(text).lower()).strip('_')
