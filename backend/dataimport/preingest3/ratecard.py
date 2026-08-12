"""
U6 — The Rate Card.

Closes D6. An exchange rate is a FINANCIAL JUDGEMENT, not a system setting: no
source file in the reference set contained one, yet several reported in foreign
currency, and the chosen rate moves the consolidated result materially. So a
rate is a required, attributed, disclosed, immutable RUN INPUT — never a
hardcoded constant (the preingest2 normalizer's `FX_TO_INR = {USD: 83, ...}` is
exactly the defect this removes).

Contract (doc Section 10):
  • Required — a run cannot start without a card covering every currency present.
  • Explicitly sourced — pair, value, rate date, and a named source per rate.
  • Versioned & immutable — amending rates creates a NEW card, hence a new run.
  • Disclosed — the workbook states every rate, its date, its source.
  • Part of the determinism key — the card id enters the run signature.

All values are the number of INR per 1 unit of the quoted currency. INR is the
base and is always 1 (no rate required, never "missing").
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional

from .quantity import to_decimal

BASE_CURRENCY = 'INR'

REFERENCE = 'reference'      # a named published reference rate
ESTIMATE = 'estimate'        # an identified management estimate (disclosed)
_SOURCE_TYPES = {REFERENCE, ESTIMATE}


class RateCardError(ValueError):
    pass


@dataclass(frozen=True)
class Rate:
    currency: str            # the quoted (foreign) currency, e.g. 'MYR'
    inr_per_unit: Decimal    # how many INR for 1 unit of `currency`
    rate_date: str           # ISO date the rate is as-of
    source: str              # human-readable source, e.g. 'RBI reference 2026-02-28'
    source_type: str = REFERENCE

    def as_row(self) -> dict:
        return {'currency': self.currency, 'inr_per_unit': str(self.inr_per_unit),
                'rate_date': self.rate_date, 'source': self.source,
                'source_type': self.source_type}


@dataclass(frozen=True)
class RateCard:
    """Immutable, identified set of rates. Build once via `from_input`, then
    treat as read-only; a change means a new card and a new run signature."""
    card_id: str
    rates: Dict[str, Rate]           # keyed by upper-case currency
    as_of: str                       # the run's reporting as-of date

    # ── construction ────────────────────────────────────────────────────
    @staticmethod
    def from_input(payload: dict) -> 'RateCard':
        """Build a card from the run-input JSON. Shape:
            {"as_of": "2026-02-28",
             "rates": [{"currency":"MYR","inr_per_unit":18.6,
                        "rate_date":"2026-02-28","source":"RBI ref",
                        "source_type":"reference"}, ...]}
        The card id is a deterministic hash of its content, so the same rates
        always yield the same id (determinism)."""
        as_of = str(payload.get('as_of') or '').strip()
        if not as_of:
            raise RateCardError('Rate Card requires an as_of reporting date')
        rates: Dict[str, Rate] = {}
        for r in (payload.get('rates') or []):
            ccy = str(r.get('currency', '')).strip().upper()
            if not ccy or ccy == BASE_CURRENCY:
                continue
            val = to_decimal(r.get('inr_per_unit'))
            if val is None or val <= 0:
                raise RateCardError(f'Rate for {ccy} must be a positive number')
            st = str(r.get('source_type', REFERENCE)).lower()
            if st not in _SOURCE_TYPES:
                raise RateCardError(f'{ccy}: source_type must be one of {_SOURCE_TYPES}')
            src = str(r.get('source', '')).strip()
            if not src:
                raise RateCardError(f'{ccy}: every rate must carry a named source')
            rates[ccy] = Rate(currency=ccy, inr_per_unit=val,
                              rate_date=str(r.get('rate_date') or as_of),
                              source=src, source_type=st)
        card_id = RateCard._compute_id(as_of, rates)
        return RateCard(card_id=card_id, rates=rates, as_of=as_of)

    @staticmethod
    def _compute_id(as_of: str, rates: Dict[str, Rate]) -> str:
        blob = json.dumps({'as_of': as_of,
                           'rates': {k: rates[k].as_row() for k in sorted(rates)}},
                          sort_keys=True)
        return 'rc_' + hashlib.sha256(blob.encode()).hexdigest()[:16]

    # ── validation ──────────────────────────────────────────────────────
    def missing_for(self, currencies) -> List[str]:
        """Currencies present in the files but not covered (INR excluded).
        Intake refuses the run if this is non-empty (fail in seconds, not at
        the barrier)."""
        need = {str(c).strip().upper() for c in currencies if c}
        need.discard(BASE_CURRENCY)
        need.discard('')
        return sorted(need - set(self.rates))

    # ── conversion (total function) ─────────────────────────────────────
    def to_inr(self, amount_native: Decimal, currency: str) -> Decimal:
        ccy = (currency or BASE_CURRENCY).strip().upper()
        if ccy == BASE_CURRENCY:
            return to_decimal(amount_native, Decimal('0'))
        rate = self.rates.get(ccy)
        if rate is None:
            raise RateCardError(f'No rate for {ccy} — intake coverage check should '
                                'have blocked this run')
        return to_decimal(amount_native, Decimal('0')) * rate.inr_per_unit

    # ── disclosure & determinism ────────────────────────────────────────
    def signature(self) -> str:
        return self.card_id

    def disclosure_rows(self) -> List[dict]:
        return [self.rates[k].as_row() for k in sorted(self.rates)]


def default_inr_card(as_of: Optional[str] = None) -> RateCard:
    """An INR-only card (no foreign rates). Valid for an all-INR run; any
    foreign currency present will correctly fail the coverage check and force a
    real card to be supplied."""
    return RateCard.from_input({'as_of': as_of or _dt.date.today().isoformat(),
                                'rates': []})
