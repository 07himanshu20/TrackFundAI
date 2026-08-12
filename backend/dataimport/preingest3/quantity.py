"""
U5 — Period Algebra: the typed quantity.

Non-negotiable build rule #8: "Every quantity carries its currency, unit,
period basis and stock-or-flow nature. Bare numbers do not exist downstream of
extraction." This module is where that rule is enforced at the type level.

Normalisation is a TOTAL FUNCTION over the type — defined for every combination,
with no default branch and no silent assumption (doc Section 09). Where the file
does not state something the code needs (an absent period basis), the quantity
is INCOMPLETE and normalisation refuses it — the run escalates rather than
guessing. A wrong Lakhs-vs-Million or YTD-vs-annual guess is a 10–100× error.

Arithmetic is Decimal throughout (build rule #11). Floating point is never used
for a monetary value.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional


# ── period basis ─────────────────────────────────────────────────────────
YTD = 'YTD'
MTD = 'MTD'
TTM = 'TTM'
FY = 'FY'
POINT_IN_TIME = 'point_in_time'
_PERIOD_BASES = {YTD, MTD, TTM, FY, POINT_IN_TIME}

# ── nature ───────────────────────────────────────────────────────────────
FLOW = 'flow'     # accrues over a period (revenue, EBITDA, capital called)
STOCK = 'stock'   # a balance at an instant (cash, NAV, headcount)
_NATURES = {FLOW, STOCK}

# ── unit scale → multiplier to ABSOLUTE native-currency units ────────────
# Universal Indian + international scale words. NEVER inferred from magnitude;
# only set from a declared unit on the sheet (the locator reports declared_unit).
SCALE_TO_ABS = {
    'absolute': Decimal('1'),
    'units': Decimal('1'),
    'ones': Decimal('1'),
    'tens': Decimal('10'),
    'hundreds': Decimal('100'),
    'thousands': Decimal('1000'),
    'k': Decimal('1000'),
    'lakhs': Decimal('100000'),
    'lakh': Decimal('100000'),
    'lac': Decimal('100000'),
    'millions': Decimal('1000000'),
    'million': Decimal('1000000'),
    'mn': Decimal('1000000'),
    'm': Decimal('1000000'),
    'crore': Decimal('10000000'),
    'crores': Decimal('10000000'),
    'cr': Decimal('10000000'),
    'billions': Decimal('1000000000'),
    'billion': Decimal('1000000000'),
    'bn': Decimal('1000000000'),
}

_CRORE = Decimal('10000000')


class QuantityError(ValueError):
    """Raised when a quantity cannot be normalised without guessing — the run
    must escalate to review, never default."""


def to_decimal(val, default=None) -> Optional[Decimal]:
    if val is None or val == '':
        return default
    if isinstance(val, Decimal):
        return val
    try:
        return Decimal(str(val).replace(',', '').strip())
    except (InvalidOperation, ValueError, TypeError):
        return default


def format_pct(frac, dp: int = 2) -> str:
    """THE canonical fraction→percent string. One implementation so two independent bug
    classes stay fixed everywhere they were previously hand-rolled (nav._pct, fund_terms
    ._fmt_pct, master_workbook._pctlabel all delegate here):

      1. Integer-valued percents keep their zeros. '100' must NOT become '1' — bare
         rstrip('0') eats significant zeros, so zeros are stripped ONLY after a decimal
         point. (This guard existed in two copies and was dropped in the third → the 100→1
         regression; centralising removes the chance to drop it again.)
      2. A COMPUTED fraction is ROUNDED, so 70/600 renders '11.67%', not the full division
         tail. (No copy rounded; latent until a computed fraction was first formatted.)

    0.02→'2%' · 0.2→'20%' · 1→'100%' · 0.025→'2.5%' · 70/600→'11.67%'. None→'n/r'."""
    if frac is None:
        return 'n/r'
    q = Decimal('1').scaleb(-dp)                      # dp=2 → Decimal('0.01')
    s = format((Decimal(str(frac)) * 100).quantize(q), 'f')
    if '.' in s:                                      # cosmetic zeros live only after the point
        s = s.rstrip('0').rstrip('.')
    return f'{s}%'


def normalise_scale(name) -> Optional[str]:
    """Map a free-text unit word to a canonical scale key, or None if unknown.
    Handles currency-prefixed words ("Rs. '000"), and singular/plural variants
    ("thousand"/"thousands") so a declared unit is never refused over a trailing
    's'. Returns None (not a guess) when genuinely unrecognised — the caller
    escalates rather than assume a scale."""
    if not name:
        return None
    key = str(name).strip().lower().replace("'", '').replace('’', '')
    for token in ('rs.', 'rs', 'inr', '₹', 'rm', 'usd', '$'):
        key = key.replace(token, '')
    key = key.strip(' .-')
    if key in SCALE_TO_ABS:
        return key
    # zero-count notation: "'000" = thousands, "'00000" = lakhs, "000000" = millions
    digits = key.replace(',', '').replace(' ', '')
    if digits and set(digits) == {'0'}:
        return {3: 'thousands', 5: 'lakhs', 6: 'millions', 7: 'crore',
                4: 'thousands', 2: 'hundreds'}.get(len(digits))
    # try singular↔plural before giving up
    if key.endswith('s') and key[:-1] in SCALE_TO_ABS:
        return key[:-1]
    if (key + 's') in SCALE_TO_ABS:
        return key + 's'
    return None


@dataclass
class Quantity:
    """A number that knows what it is. Constructed at extraction; consumed only
    through the total-function normalisers below."""
    amount: Decimal                      # native, at the declared scale
    currency: str                        # e.g. 'INR', 'MYR', 'USD'
    scale: str = 'absolute'              # a SCALE_TO_ABS key
    period_basis: Optional[str] = None   # a _PERIOD_BASES member, or None
    months: Optional[int] = None         # length of the period for a flow
    period_end: Optional[_dt.date] = None
    nature: str = FLOW                   # FLOW or STOCK
    concept: Optional[str] = None        # what it measures (for error messages)

    def __post_init__(self):
        self.amount = to_decimal(self.amount, Decimal('0'))
        self.currency = (self.currency or 'INR').upper()
        if self.nature not in _NATURES:
            raise QuantityError(f'{self.concept}: nature {self.nature!r} invalid')
        if self.period_basis is not None and self.period_basis not in _PERIOD_BASES:
            raise QuantityError(f'{self.concept}: period_basis {self.period_basis!r} invalid')
        if self.scale not in SCALE_TO_ABS:
            raise QuantityError(f'{self.concept}: scale {self.scale!r} unknown — '
                                'refusing to guess (build rule: no silent assumption)')

    # ── total functions ─────────────────────────────────────────────────
    def absolute_native(self) -> Decimal:
        """Native-currency amount at absolute (1×) scale. Total for any valid
        scale (guaranteed by __post_init__)."""
        return self.amount * SCALE_TO_ABS[self.scale]

    def annualised_native(self) -> Decimal:
        """Native absolute amount, annualised iff it is a FLOW over a known
        part-year. A STOCK is NEVER annualised (a balance is a balance) — the
        type prevents the error, not a convention. A flow with unknown months
        is refused, not assumed to be annual."""
        base = self.absolute_native()
        if self.nature == STOCK:
            return base
        if self.period_basis in (None,):
            raise QuantityError(f'{self.concept}: flow with no period basis — escalate')
        if self.period_basis in (FY, TTM, POINT_IN_TIME):
            return base
        # YTD / MTD → scale up by the period length
        if not self.months or self.months <= 0:
            raise QuantityError(f'{self.concept}: {self.period_basis} flow with no month '
                                'count — escalate, do not annualise blindly')
        return (base * Decimal(12)) / Decimal(self.months)

    def is_normalisable(self) -> bool:
        try:
            self.annualised_native()
            return True
        except QuantityError:
            return False
