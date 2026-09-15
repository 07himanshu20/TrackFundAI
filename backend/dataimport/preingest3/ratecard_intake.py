"""U6 Phase 3 — rate-card intake. TWO CO-EQUAL paths to the SAME validated, card_id-stamped RateCard:

  • card_from_manual   — the user TYPES rates directly (pair, rate, date, source).
  • card_from_schedule — parse an uploaded rate-schedule sheet (fuzzy-matched columns, format-agnostic).

Manual entry is FIRST-CLASS, never a fallback: a user holding one MYR→INR rate and no schedule file must
be able to type it and get exactly the same card an upload would produce. Both paths normalise each entry,
run it through ONE shared fail-closed gate (`_validate_entry`), then hand the result to
RateCard.from_input — so both inherit identical validation and the same deterministic card_id.

The gate exists because a mistyped rate is precisely the ungoverned number this subsystem guards against.
It judges VALIDITY only — whether the rate is TRUSTWORTHY: a POSITIVE numeric rate; a date EQUAL to the
run's as_of; a well-formed FOREIGN currency (a 3-letter code, or a PAIR that unambiguously names one
foreign side against the INR base); and a NAMED source. Any validity failure raises IntakeError and refuses
the WHOLE card (all-or-nothing — a partially-accepted card would silently drop a rate); never a silent
skip, never a coerced value.

VALIDITY is NOT ACTIONABILITY. Whether THIS run actually needs a given rate is a property of the RUN, not
of the rate: a valid, sourced rate for a currency the run does not need (already covered, absent, or an
upstream-blocked exposure a rate cannot surface) is NOT an error — it is accepted ONTO the card and
DISCLOSED as a no-op (see `noop_currencies`). Refusing it would make a standing/org card covering many
currencies un-ingestable on any run that needs only some of them, and would reject a user who pastes their
whole FX card when only one pair is uncovered. So actionability never refuses a card; only validity does.
"""
from __future__ import annotations

import math
import re
from typing import Iterable, List, Optional

from . import lexicon
from .quantity import to_decimal
from .ratecard import BASE_CURRENCY, RateCard

_CCY_RE = re.compile(r'^[A-Z]{3}$')
_PAIR_SPLIT = re.compile(r'[\/\-\s→>|,:]+')

# ── fat-finger decimal-shift guard (the rate-entry sanity band) ────────────────────────────────────────
# A typo band is a COMPARISON — 23.38 and 233.8 are both valid-looking, only a REFERENCE distinguishes
# them. The reference is the currency's OWN prior entered rate (supplied by the caller as `reference_rate`;
# used for the check ONLY, never for conversion — the operative rate is still entered fresh each upload).
# The fingerprint is a near-exact power-of-ten shift (a decimal-point slip is ×/÷10^k), which a genuine FX
# move between uploads (±single-to-tens-of-percent) essentially never is. This is currency-agnostic — the
# only premise is 'a clean 10^k jump between a currency's own consecutive rates is a decimal slip, a %
# move is real FX', true for EVERY currency — NOT a per-currency plausible-range table (a rule-#0 patch).
_DECIMAL_SHIFT_LOG_TOL = 0.15   # within ~×1.41 of a clean 10^k fold reads as a decimal slip, not FX drift:
                                # separates the decimal-shift CLASS from real drift (a 30% move → k=0; a
                                # 10× typo even under heavy drift → k=±1). Universal, not per-currency.


def _decimal_shift_k(new_rate, reference_rate) -> Optional[int]:
    """The fat-finger fingerprint: if new/reference is within tolerance of a NON-ZERO power of ten, return
    that integer k (+1 = a 10× over-type like 233.8 for 23.38; -1 = a 10× under-type). Else None (no
    reference, a non-power-of-ten move = real FX drift, or an unusable input). Reference-only — the prior
    rate is never a conversion input."""
    nr, rr = to_decimal(new_rate), to_decimal(reference_rate)
    if nr is None or rr is None or nr <= 0 or rr <= 0:
        return None
    log = math.log10(float(nr) / float(rr))
    k = round(log)
    return k if (k != 0 and abs(log - k) <= _DECIMAL_SHIFT_LOG_TOL) else None

# schedule column synonyms (matched via the CENTRAL lexicon matcher — no hand-rolled substring logic)
_COL_SYNS = {
    'currency': ['currency', 'ccy', 'currency pair', 'pair', 'fx pair'],
    'rate': ['inr per unit', 'inr per 1', 'rate', 'exchange rate', 'fx rate', 'inr rate'],
    'date': ['rate date', 'as of date', 'as of', 'as-of', 'date'],
    'source': ['source', 'reference', 'provider', 'source name'],
}


class IntakeError(ValueError):
    """A supplied rate failed the fail-closed gate — the card is refused, nothing is coerced."""


def _foreign_of_pair(raw) -> Optional[str]:
    """Extract the single FOREIGN currency named by an entry's currency field. Accepts a bare code
    ('MYR') or a pair in any separator ('MYR/INR', 'INR-MYR', 'MYR→INR'). Returns None if it does not
    name exactly one non-INR 3-letter code (ambiguous 'MYR/SGD', junk → refuse, never guess)."""
    parts = [p for p in _PAIR_SPLIT.split(str(raw).strip().upper()) if p]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0] if _CCY_RE.match(parts[0]) else None
    foreign = [p for p in parts if p != BASE_CURRENCY and _CCY_RE.match(p)]
    has_inr = BASE_CURRENCY in parts
    # a valid pair is FOREIGN vs the INR base and names exactly one foreign side
    return foreign[0] if (has_inr and len(foreign) == 1) else None


def _validate_entry(entry: dict, *, as_of) -> dict:
    """The single fail-closed VALIDITY gate, shared by BOTH intake paths. Returns a from_input-ready
    rate row or raises IntakeError. Judges only whether the rate is TRUSTWORTHY (positive number, date ==
    run as_of, well-formed FOREIGN-vs-INR pair, named source) — NOT whether this run needs it. A valid rate
    for a currency the run does not need is a disclosed no-op (`noop_currencies`), never a refusal, so a
    superset card (a standing/org card, or a pasted full card) ingests cleanly."""
    raw_ccy = entry.get('currency', entry.get('pair', ''))
    ccy = _foreign_of_pair(raw_ccy)
    if ccy is None:
        raise IntakeError(f'currency/pair {raw_ccy!r} does not name one foreign currency vs {BASE_CURRENCY}')
    if ccy == BASE_CURRENCY:
        raise IntakeError(f'{BASE_CURRENCY} is the base currency — it needs no rate')

    rate = to_decimal(entry.get('inr_per_unit', entry.get('rate')))
    if rate is None or rate <= 0:
        raise IntakeError(f'{ccy}: rate must be a positive number, got '
                          f'{entry.get("inr_per_unit", entry.get("rate"))!r}')

    # Rate-entry sanity band: compare against the currency's OWN prior rate (reference-only). A near-exact
    # power-of-ten shift is a decimal-point typo (a genuine FX move never is) → REFUSE fail-closed, unless
    # the human explicitly confirms the unusual rate. With NO reference (first upload for this currency)
    # the band cannot fire — it is disclosed as unverified (rate_verified=False), never faked or blocked.
    reference = entry.get('reference_rate')
    k = _decimal_shift_k(rate, reference)
    if k is not None and not entry.get('confirmed'):
        fold = ('×' if k > 0 else '÷') + f'10^{abs(k)}'
        raise IntakeError(
            f'{ccy}: rate {rate} looks like a decimal-shift typo of its prior rate {reference} ({fold}) — '
            f'a genuine FX move is never a clean power of ten. Re-enter the correct rate, or set '
            f"'confirmed': true to attest this unusual rate is intended.")

    date = str(entry.get('rate_date') or entry.get('date') or '').strip()
    if date != str(as_of):
        raise IntakeError(f'{ccy}: rate_date {date!r} must equal the run as_of {as_of!r} '
                          '(a rate for another date is not this run\'s rate)')

    source = str(entry.get('source') or '').strip()
    if not source:
        raise IntakeError(f'{ccy}: a named source is required (an unsourced rate is an ungoverned number)')

    st = str(entry.get('source_type') or 'reference').strip().lower()
    return {'currency': ccy, 'inr_per_unit': str(rate), 'rate_date': date,
            'source': source, 'source_type': st}


def card_from_manual(entries: Iterable[dict], *, as_of) -> RateCard:
    """Build a validated card from manually-typed entries. FAIL-CLOSED on VALIDITY: any invalid entry
    raises IntakeError and NO card is produced (all-or-nothing — a partially-accepted card would silently
    drop a rate). A valid rate the run does not need is accepted (disclose it via `noop_currencies`),
    never a build-time refusal — a standing/org card or a pasted superset ingests unchanged."""
    rows: List[dict] = [_validate_entry(e, as_of=as_of) for e in (entries or [])]
    if not rows:
        raise IntakeError('no rate entries supplied')
    return RateCard.from_input({'as_of': str(as_of), 'rates': rows})


def noop_currencies(card: RateCard, *, uncovered) -> List[dict]:
    """Actionability DISCLOSURE (non-fatal) — the accept-and-note half of the validity/actionability
    split. Given a built card and the run's rate-actionable (`uncovered`) currency set, return the card's
    currencies NOT actionable THIS run: the rate is valid and stays on the card (it may be actionable on
    another run — the standing-card case), but it surfaces nothing now. Empty when `uncovered` is None
    (nothing to disclose against) or every card currency is actionable. Phase 5 shows these as 'supplied
    but not needed this run', distinct from the currencies it REQUESTS (the uncovered set)."""
    if uncovered is None:
        return []
    need = {str(c).strip().upper() for c in uncovered}
    return [{'currency': ccy,
             'reason': 'valid rate, but not rate-actionable this run — the currency is already covered, '
                       'absent, or blocked upstream, so the rate surfaces nothing now (kept on the card)'}
            for ccy in sorted(card.rates) if ccy not in need]


def _match_header_row(rows) -> Optional[tuple]:
    """Find the header row + its column map. The header is the first row whose cells match BOTH a
    currency column and a rate column (the two load-bearing fields), via the central label matcher."""
    for ri, row in enumerate(rows[:25]):
        colmap = {}
        for ci, cell in enumerate(row):
            if not isinstance(cell, str):
                continue
            for key, syns in _COL_SYNS.items():
                if key not in colmap and lexicon.label_matches_any(cell, syns):
                    colmap[key] = ci
        if 'currency' in colmap and 'rate' in colmap:
            return ri, colmap
    return None


def card_from_schedule(rows, *, as_of) -> RateCard:
    """Build a validated card from an uploaded rate-schedule sheet (list-of-rows grid). Columns are
    fuzzy-matched (format-agnostic — no hardcoded positions); each data row runs the SAME validity gate as
    manual entry. A sheet with no recognisable currency+rate header raises IntakeError (never guess a layout)."""
    hit = _match_header_row(rows)
    if hit is None:
        raise IntakeError('no rate-schedule header found (need at least a currency column and a rate column)')
    hdr, colmap = hit
    entries: List[dict] = []
    for row in rows[hdr + 1:]:
        def _cell(key):
            ci = colmap.get(key)
            return row[ci] if (ci is not None and ci < len(row)) else None
        ccy_cell = _cell('currency')
        if ccy_cell is None or str(ccy_cell).strip() == '':
            continue                                       # blank row / spacer — not a rate line
        entries.append({'currency': ccy_cell, 'rate': _cell('rate'),
                        'date': _cell('date') if 'date' in colmap else as_of,
                        'source': _cell('source') if 'source' in colmap else None})
    return card_from_manual(entries, as_of=as_of)
