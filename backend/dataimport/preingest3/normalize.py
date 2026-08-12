"""
S7 — Normalisation. Turns a concept's located per-column values into ONE typed,
₹Cr figure by composing the two correctness-blockers and the Rate Card:

  CF1 (periods.collapse)  → the right period value (discrete sum / cumulative
                            latest / partial / stock latest), escalating ambiguity.
  CF2 (units.resolve)     → the right scale + currency, escalating a mislabel.
  RateCard                → native → INR (a required, disclosed run input).
  Quantity                → the typed carrier; absolute-scale conversion.

Decimal throughout, rounding defined once. If EITHER blocker escalates, the
figure is returned held (value computed for the reviewer's convenience, but
flagged escalate=True) — never silently shipped. Total function: every branch
is defined; nothing defaults silently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional

from . import periods, units
from .contract import ROUNDING_DP, concept_measure, concept_nature
from .quantity import Quantity, QuantityError

_BASIS_MAP = {'TTM': 'TTM', 'YTD': 'YTD', 'FY': 'FY', 'MTD': 'MTD',
              'point_in_time': 'point_in_time', 'partial': None}
_CR = Decimal('10000000')
_Q = Decimal('1').scaleb(-ROUNDING_DP)   # rounding quantum


@dataclass
class NormalizedFigure:
    concept: str
    value_cr: Optional[Decimal]
    currency: str
    scale: str
    basis: str
    months: int
    native_value: Optional[Decimal]
    escalate: bool = False
    flags: List[str] = field(default_factory=list)
    reason: str = ''
    suggested_scale: Optional[str] = None


def normalize_concept(concept: str, period_columns, values_by_col: Dict[int, object], *,
                      ratecard, declared_unit=None, declared_ccy=None, header_hints=None,
                      sheet_hints=None, domicile=None, anchor_cr=None,
                      as_of_year: int = None) -> NormalizedFigure:
    """Normalise one concept from its per-column located values to ₹Cr."""
    nature = concept_nature(concept)

    # ── CF1: collapse the period axis ───────────────────────────────────
    col = periods.collapse(concept, nature, period_columns, values_by_col, as_of_year=as_of_year)
    flags = list(col.flags)
    if col.escalate or col.value is None:
        return NormalizedFigure(concept, None, declared_ccy or 'INR', 'absolute',
                                col.basis, col.months, None, escalate=True,
                                flags=flags + ['period_' + col.axis], reason=col.reason)

    # ── non-money measures (headcount / ratios) skip FX + scaling entirely ──
    if concept_measure(concept) != 'money':
        return NormalizedFigure(concept, col.value, 'count', 'absolute', col.basis,
                                col.months, col.value, escalate=False,
                                flags=flags + ['non_monetary'],
                                reason=f'{col.reason}; count/ratio — not currency-scaled')

    # ── CF2: resolve scale + currency, magnitude-checked against the anchor ──
    ur = units.resolve(value=col.value, declared_unit=declared_unit, declared_ccy=declared_ccy,
                       header_hints=header_hints, sheet_hints=sheet_hints, domicile=domicile,
                       anchor_cr=anchor_cr, ratecard=ratecard)
    flags += ur.flags
    if ur.escalate:
        return NormalizedFigure(concept, None, ur.currency, ur.scale, col.basis, col.months,
                                col.value, escalate=True, flags=flags,
                                reason=ur.reason, suggested_scale=ur.suggested_scale)

    # ── build the typed quantity and convert to ₹Cr ─────────────────────
    try:
        q = Quantity(amount=col.value, currency=ur.currency, scale=ur.scale,
                     period_basis=_BASIS_MAP.get(col.basis), months=col.months or None,
                     nature=nature, concept=concept)
    except QuantityError as e:
        return NormalizedFigure(concept, None, ur.currency, ur.scale, col.basis, col.months,
                                col.value, escalate=True, flags=flags + ['quantity_error'],
                                reason=str(e))
    native_abs = q.absolute_native()
    try:
        inr = ratecard.to_inr(native_abs, ur.currency)
    except Exception as e:  # noqa: BLE001 — a missing/invalid rate HOLDS, never crashes
        return NormalizedFigure(concept, None, ur.currency, ur.scale, col.basis, col.months,
                                col.value, escalate=True, flags=flags + ['no_rate_for_currency'],
                                reason=f'Rate Card has no rate for {ur.currency} — hold: {e}')
    value_cr = (inr / _CR).quantize(_Q, rounding=ROUND_HALF_UP)
    return NormalizedFigure(concept, value_cr, ur.currency, ur.scale, col.basis, col.months,
                            col.value, escalate=False, flags=flags,
                            reason=f'{col.reason}; {ur.reason}')
