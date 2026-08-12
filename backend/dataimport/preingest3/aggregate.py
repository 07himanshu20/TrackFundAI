"""
Hole-aware and basis-aware aggregation — the two silent-wrong-number guards for
the back half.

#1 HOLES ARE NOT ZERO. A total that depends on a HELD or GAP figure is not
   computed as if the hole were 0. It is reported INCOMPLETE, listing the held
   items, so a partial sum can never masquerade as a finished total.

#2 PERIODS ARE NEVER SILENTLY BLENDED. Summing a 5-month figure with an 11-month
   YTD figure is apples-to-oranges (the exact source of the reference's 13%
   variance). The mixed-period policy is EXPLICIT and stated in the output:

     • as_reported  — sums ONLY figures that already share one basis; a mixed set
                      is reported mixed=True and is not collapsed to one number.
     • annualized   — the cross-company COMMON-BASIS total: every flow annualised
                      to 12 months (×12/months), each annualisation recorded, so
                      a 5-month and an 11-month figure are put on the same footing
                      before adding. Stocks are never annualised.

   Both are offered; S9 presents the annualized total as the headline (labelled)
   and retains each company's native value+period, with a disclosure of what was
   annualised. No total blends periods without saying so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

from .cir import Figure
from .contract import concept_nature
from .quantity import STOCK

# a partial flow shorter than this (in months) that is annualised is flagged as a
# projection, since annualising a very short stub is a weak extrapolation.
_SHORT_STUB_MONTHS = 3


@dataclass
class AggResult:
    value: Optional[Decimal]          # None when incomplete under 'as_reported'
    complete: bool                    # False if any input was held/gap
    basis: str                        # the common basis, or 'mixed'
    mixed: bool                       # inputs spanned more than one basis
    n_confirmed: int = 0
    held: List[str] = field(default_factory=list)      # concepts/entities excluded
    annualized_items: List[dict] = field(default_factory=list)
    unresolved_period: List[str] = field(default_factory=list)   # flows with unknown period
    notes: List[str] = field(default_factory=list)


def _confirmed(figs: List[Figure]):
    ok, held = [], []
    for f in figs:
        if f.confirmed:
            ok.append(f)
        else:
            held.append(f.provenance.source_file + ':' + f.concept if f.provenance else f.concept)
    return ok, held


def sum_as_reported(figures: List[Figure]) -> AggResult:
    """Sum only if all confirmed AND all share one basis. Any held → incomplete;
    mixed basis → not collapsed (mixed=True, value=None)."""
    ok, held = _confirmed(figures)
    bases = {f.basis for f in ok if f.basis}
    mixed = len(bases) > 1
    if held:
        return AggResult(None, False, next(iter(bases), 'unknown'), mixed,
                         len(ok), held, notes=[f'{len(held)} held input(s) — total incomplete'])
    if mixed:
        return AggResult(None, True, 'mixed', True, len(ok), [],
                         notes=[f'inputs span bases {sorted(bases)} — not summed as-reported'])
    return AggResult(sum((f.value_cr for f in ok), Decimal('0')), True,
                     next(iter(bases), 'n/a'), False, len(ok), [])


def _is_stock(f: Figure) -> bool:
    """Nature is authoritative from the concept table (U5) — NOT f.native, which is
    None on every pipeline-emitted figure, so keying off it silently treats stocks
    as flows. A native typed quantity, when present, refines it."""
    if f.native is not None:
        return f.native.nature == STOCK
    return concept_nature(f.concept) == STOCK


def sum_annualized(figures: List[Figure]) -> AggResult:
    """Common-basis total: annualise each FLOW to 12 months before adding; stocks
    pass through un-annualised. Records every annualisation. Hole-aware (any held
    input → incomplete) AND period-aware: a FLOW whose period length is unknown is
    un-annualisable, so it can NOT be placed on the 12-month footing — it makes the
    total INCOMPLETE rather than being added raw (the silent mixed-basis blend)."""
    ok, held = _confirmed(figures)
    total = Decimal('0')
    items, notes, unresolved = [], [], []
    for f in ok:
        v = f.value_cr
        if _is_stock(f):
            total += v                                  # a balance is never annualised
            continue
        if not f.months:                                # FLOW with unknown period
            unresolved.append((f.provenance.source_file if f.provenance else '?') + ':' + f.concept)
            continue
        if 0 < f.months < 12:
            factor = Decimal(12) / Decimal(f.months)
            annual = (v * factor)
            items.append({'concept': f.concept, 'native_months': f.months,
                          'native_value': str(v), 'annualized': str(annual),
                          'stub': f.months < _SHORT_STUB_MONTHS})
            if f.months < _SHORT_STUB_MONTHS:
                notes.append(f'{f.concept}: annualised from only {f.months}m (weak projection)')
            total += annual
        else:                                           # months >= 12 — already annual
            total += v
    incomplete = bool(held) or bool(unresolved)
    res = AggResult(None if incomplete else total, not incomplete, 'annualized_12m', False,
                    len(ok), held, annualized_items=items, unresolved_period=unresolved, notes=notes)
    if held:
        res.notes.append(f'{len(held)} held input(s) — annualized total incomplete')
    if unresolved:
        res.notes.append(f'{len(unresolved)} flow(s) with unknown period — cannot annualise, '
                         f'total incomplete: {unresolved}')
    return res


def aggregate(figures: List[Figure], *, policy: str = 'annualized') -> AggResult:
    """Single entry point. policy='annualized' (common-basis headline) or
    'as_reported' (only same-basis). Hole-aware in both."""
    return sum_annualized(figures) if policy == 'annualized' else sum_as_reported(figures)
