"""
S5a — Read. Code opens the located cell(s) and reads the value. This is the
half of the Locator Protocol that produces numbers: the model pointed, code
reads. Reading a cell is I/O, not judgement (build rule: reading is code's job).

A direct form reads one cell; an expression form SUMS its operand cells (+ only,
already validated same-column/same-statement). The raw value is wrapped in a
typed Quantity carrying the declared unit, currency, period and — chosen by the
concept, never guessed from the magnitude — its stock/flow nature.

Units policy (U5, pragmatic): a DECLARED-but-unrecognised unit is refused
(escalate — never guess a 10–100× scale). A genuinely ABSENT unit reads at
absolute scale but is flagged unit_assumed=True so it is disclosed and can be
escalated, rather than silently blocking every unit-less MIS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .contract import concept_nature
from .quantity import Quantity, QuantityError, normalise_scale, to_decimal
from .locator_schema import LocatorRecord, FORM_DIRECT, FORM_EXPRESSION


@dataclass
class ReadFigure:
    concept: str
    raw: Optional[Decimal]
    quantity: Optional[Quantity]
    cells: List[str] = field(default_factory=list)     # A1 addresses actually read
    read_ok: bool = False
    reason: str = ''
    unit_assumed: bool = False


def _cell_value(grid: Dict[str, List[List[Any]]], sheet: str, col0: int, row0: int):
    rows = grid.get(sheet)
    if rows is None or row0 >= len(rows):
        return None
    row = rows[row0]
    return row[col0] if col0 < len(row) else None


def _a1(col0: int, row0: int) -> str:
    from openpyxl.utils import get_column_letter
    return f'{get_column_letter(col0 + 1)}{row0 + 1}'


def read_figure(rec: LocatorRecord, grid: Dict[str, List[List[Any]]], default_sheet: str,
                *, default_ccy: str = 'INR') -> ReadFigure:
    """Read a validated locator record into a typed figure. Never raises — a bad
    read degrades to read_ok=False with a reason (escalate), never a crash."""
    concept = rec.concept
    cells, values = [], []
    for (sheet, col0, row0) in rec.resolved:
        sn = sheet or default_sheet
        v = _cell_value(grid, sn, col0, row0)
        cells.append(_a1(col0, row0))
        values.append(v)

    if not values:
        return ReadFigure(concept, None, None, cells, False, 'no cells resolved')

    # Existence at the value level — every operand must be a real number.
    nums = []
    for v in values:
        d = to_decimal(v)
        if d is None:
            return ReadFigure(concept, None, None, cells, False,
                              f'non-numeric cell in {rec.form} form')
        nums.append(d)

    raw = nums[0] if rec.form == FORM_DIRECT else sum(nums, Decimal('0'))

    # Build the typed quantity.
    unit_assumed = False
    if rec.declared_unit:
        scale = normalise_scale(rec.declared_unit)
        if scale is None:
            return ReadFigure(concept, raw, None, cells, False,
                              f'declared unit {rec.declared_unit!r} unrecognised — escalate')
    else:
        scale = 'absolute'
        unit_assumed = True

    try:
        q = Quantity(amount=raw, currency=(rec.declared_ccy or default_ccy),
                     scale=scale, period_basis=rec.period_basis,
                     months=rec.period_months, nature=concept_nature(concept),
                     concept=concept)
    except QuantityError as e:
        return ReadFigure(concept, raw, None, cells, False, str(e), unit_assumed)

    return ReadFigure(concept, raw, q, cells, True, '', unit_assumed)
