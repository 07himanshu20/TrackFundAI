"""
U1 — The Locator record: the structured shape the model must return, and the
code that VALIDATES it. The schema is the enforcement — a fabricated number is
impossible because no field can hold one (build rule #1).

Three forms, in order of preference (doc §05, refined in review):
  • direct     — one cell address. Preferred; use whenever a total cell exists.
  • expression — a list of cell addresses to SUM (+ only). Reassembles a figure
                 split across cells (e.g. revenue = HID + Sci.Lab + Service).
                 Operands are ADDRESSES ONLY (never literals), all in the SAME
                 COLUMN and SAME STATEMENT, ≤ MAX_OPERANDS. Subtraction is NOT
                 permitted — that is constructing a figure the statement does not
                 state; such figures are derived later in code from directly
                 located inputs, where the judgement is explicit and testable.
  • absent     — the figure is genuinely not in this statement. A disclosed gap.

The validator is the teeth. Anything that violates a rule is rejected and the
concept is escalated — never silently accepted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from openpyxl.utils import column_index_from_string, get_column_letter

FORM_DIRECT = 'direct'
FORM_EXPRESSION = 'expression'
FORM_ABSENT = 'absent'
_FORMS = {FORM_DIRECT, FORM_EXPRESSION, FORM_ABSENT}

MAX_OPERANDS = 12


class LocatorError(ValueError):
    pass


def parse_addr(addr: str) -> Optional[Tuple[Optional[str], int, int]]:
    """'Summary!K14' or 'K14' → (sheet_or_None, col_idx0, row_idx0). Returns
    None if it is not a valid single A1 address (e.g. a range, a number, junk)."""
    if not isinstance(addr, str):
        return None
    s = addr.strip()
    if not s:
        return None
    sheet = None
    if '!' in s:
        left, s = s.rsplit('!', 1)
        sheet = left.strip().strip("'").strip()
    s = s.replace('$', '')
    if ':' in s:                       # a range is not a single cell
        return None
    # split leading letters (column) + trailing digits (row)
    i = 0
    while i < len(s) and s[i].isalpha():
        i += 1
    col_s, row_s = s[:i], s[i:]
    if not col_s or not row_s or not row_s.isdigit():
        return None
    try:
        col = column_index_from_string(col_s.upper()) - 1
    except Exception:
        return None
    row = int(row_s) - 1
    if row < 0:
        return None
    return (sheet, col, row)


def _looks_numeric(x) -> bool:
    """True if an operand is (or contains) a numeric literal — which is
    forbidden: operands must be cell ADDRESSES so the model can never inject a
    value through the expression form."""
    if isinstance(x, (int, float)):
        return True
    try:
        float(str(x).replace(',', '').strip())
        return True
    except (TypeError, ValueError):
        return False


@dataclass
class LocatorRecord:
    concept: str
    form: str
    address: Optional[str] = None
    operands: List[str] = field(default_factory=list)
    row_label: str = ''
    col_label: str = ''
    declared_unit: Optional[str] = None
    declared_ccy: Optional[str] = None
    period_basis: Optional[str] = None
    period_months: Optional[int] = None
    # populated by validation:
    valid: bool = False
    reject_reason: str = ''
    resolved: List[Tuple[Optional[str], int, int]] = field(default_factory=list)  # parsed cells


def _same_column(cells: List[Tuple[Optional[str], int, int]]) -> bool:
    cols = {(c[0], c[1]) for c in cells}   # (sheet, col)
    return len(cols) == 1


def validate_record(rec: LocatorRecord, *, statement_rows: Optional[range] = None,
                    statement_sheet: Optional[str] = None) -> LocatorRecord:
    """Enforce every U1 rule. Sets rec.valid / rec.reject_reason / rec.resolved.
    `statement_rows` (0-based row range) and `statement_sheet` scope an expression
    to one statement — a cross-statement or cross-column sum is rejected."""
    if rec.form not in _FORMS:
        rec.reject_reason = f'unknown form {rec.form!r}'
        return rec

    if rec.form == FORM_ABSENT:
        rec.valid = True
        return rec

    if rec.form == FORM_DIRECT:
        p = parse_addr(rec.address or '')
        if p is None:
            rec.reject_reason = f'direct: unparseable address {rec.address!r}'
            return rec
        if statement_rows is not None and p[2] not in statement_rows:
            rec.reject_reason = f'direct: address outside statement rows'
            return rec
        rec.resolved = [p]
        rec.valid = True
        return rec

    # FORM_EXPRESSION — the guarded path
    ops = rec.operands or []
    if not ops:
        rec.reject_reason = 'expression: no operands'
        return rec
    if len(ops) > MAX_OPERANDS:
        rec.reject_reason = f'expression: {len(ops)} operands > MAX_OPERANDS'
        return rec
    resolved = []
    for op in ops:
        if _looks_numeric(op):
            rec.reject_reason = f'expression: numeric literal operand {op!r} forbidden'
            return rec
        p = parse_addr(op)
        if p is None:
            rec.reject_reason = f'expression: operand {op!r} is not a cell address'
            return rec
        if statement_rows is not None and p[2] not in statement_rows:
            rec.reject_reason = 'expression: operand outside statement rows'
            return rec
        resolved.append(p)
    if not _same_column(resolved):
        rec.reject_reason = 'expression: operands span multiple columns'
        return rec
    rec.resolved = resolved
    rec.valid = True
    return rec


def from_model_obj(obj: dict) -> LocatorRecord:
    """Build a LocatorRecord from one raw model object (pre-validation)."""
    ph = obj.get('period_hint') or {}
    return LocatorRecord(
        concept=str(obj.get('concept') or '').strip().lower(),
        form=str(obj.get('form') or '').strip().lower(),
        address=obj.get('address'),
        operands=[str(o) for o in (obj.get('operands') or [])],
        row_label=str(obj.get('row_label') or '').strip(),
        col_label=str(obj.get('col_label') or '').strip(),
        declared_unit=(obj.get('declared_unit') or None),
        declared_ccy=(obj.get('declared_ccy') or None),
        period_basis=(ph.get('basis') if isinstance(ph, dict) else None),
        period_months=(ph.get('months') if isinstance(ph, dict) else None),
    )
