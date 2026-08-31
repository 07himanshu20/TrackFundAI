"""
Quoted / Unquoted classification (Q2) — an ADDITIVE overlay over the portfolio's
already-verified fair values. It PARTITIONS the untouched per-company FV figures into
quoted / unquoted / held buckets; it never recomputes, re-marks, or mutates a Figure. So
`Σquoted + Σunquoted + Σheld ≡ Σ portfolio FV` holds BY CONSTRUCTION, and the labelling
cannot move a single number on the NAV / TVPI / MOIC surfaces (neutral by construction).

The classification is POSITIVE-EVIDENCE and FAIL-CLOSED, with a deliberate asymmetry that
mirrors how these securities actually behave:

  • QUOTED is the exceptional claim and requires a HARD anchor — a checksum-valid ISIN or a
    named listing exchange (or a source that explicitly states listed / IPEV level 1). A
    valuation *method* that merely says "market price" with NO exchange/ISIN to confirm it is
    the ambiguous middle → HELD, never force-bucketed.
  • UNQUOTED is inferable from POSITIVE private-valuation-method evidence (recent round,
    revenue/EBITDA multiple, EV/EBITDA, DCF, cost, book value …) — because a private
    technique is itself evidence the holding is not exchange-listed, and it is the
    overwhelming reality for an Indian AIF.
  • Any genuine CONTRADICTION (a listing anchor but a stated-unlisted source; a method that
    names both a market price and a private technique; no evidence either way) → HELD. We
    disclose the hold; we never guess a bucket.

Two honesty conditions on every emitted classification:
  • basis is STATED only when the source itself carries listing data (ISIN / exchange /
    share type / IPEV level); otherwise it is INFERRED and labelled as such, so a reader
    knows it is a derivation ready to be overridden — not an extracted fact.
  • the inference fires only on positive evidence and HOLDS when ambiguous — the same
    fail-closed rule used everywhere else in this engine.

STANDING INVARIANT — LABELLING, NEVER REVALUATION (the trap that stays invisible while the
quoted bucket is empty, and bites the day a real listed holding arrives): this overlay
LABELS the fund's own STATED fair value; it must NEVER recompute a quoted holding's FV from
market data. A Level-1 holding is valued by the fund as market-price × shares, but the
overlay takes that stated figure exactly as it takes a Level-3 DCF/last-round figure —
because Level-1 valuation MECHANICALLY IS 'market price × shares', a future implementer (or
the field-test of locate_listing_signals) may be tempted to re-derive the quoted FV from
live market data. That would (a) introduce a brand-new value path sourced OUTSIDE the fund's
documents, (b) break the Σquoted+Σunquoted+Σheld ≡ Σ portfolio FV neutrality the whole
overlay rests on, and (c) risk disagreeing with the fund's own audited NAV. If a fund's
stated quoted FV ever differs from a market recompute, that is a RECONCILIATION DISCLOSURE,
not a licence to override the fund's number. The source-parse, when it firms up against a
real listed holding, adds only CLASSIFICATION EVIDENCE (ISIN / exchange) — never a value.

The *classification decision logic*, the ISIN checksum gate, the methodology lexicons, and
the partition + tie control in this module are fully validatable now against synthetic
fixtures (no real quoted fund required). The one piece that cannot be field-validated until
a real listed-holding fund arrives — LOCATING the ISIN / exchange / share-type columns in a
real source sheet — lives in `locate_listing_signals()`, isolated and labelled
spec-tested-until-field-tested. When such a fund shows up, only that locator firms up
against its real layout; the decision logic, partition, tie, and subsheet do not change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from . import lexicon

# ── classification + basis vocabulary ────────────────────────────────────────
QUOTED = 'quoted'
UNQUOTED = 'unquoted'
HELD = 'held'

STATED = 'stated'        # source explicitly carries listing data (ISIN/exchange/share_type/IPEV)
INFERRED = 'inferred'    # derived from valuation methodology; no source-stated listing data
HELD_BASIS = 'held'

# universal valuation-technique vocabulary (financial-domain terms, NOT fund-specific column
# or sheet names) — the ONLY methodology evidence used, matched WHOLE-WORD through the ONE
# shared matcher (lexicon.label_matches_any), never a hand-rolled substring test. Authored in
# the normalised form the matcher sees (lexicon.normalise_label drops parentheticals, so
# 'EV/EBITDA (comparable)' presents as 'ev ebitda').
_QUOTED_METHOD_SYNS = ('market price', 'quoted price', 'quoted market value', 'quoted market price',
                       'current market price', 'cmp', 'last traded price', 'closing price',
                       'mark to market', 'mtm', 'exchange price', 'listed price', 'market quotation')
_PRIVATE_METHOD_SYNS = ('recent round', 'latest round', 'last round', 'round price', 'last-round',
                        'revenue multiple', 'arr multiple', 'sales multiple', 'ebitda multiple',
                        'ev ebitda', 'ev revenue', 'comparable', 'comparables', 'dcf',
                        'discounted cash flow', 'cost', 'book value', 'price to book', 'p b',
                        'net asset value', 'milestone', 'entry price', 'transaction multiple',
                        'option pricing', 'backsolve')
_LISTED_SHARE_SYNS = ('listed', 'quoted')
_UNLISTED_SHARE_SYNS = ('unlisted', 'unquoted', 'private')

# the signal keys the classifier reads off a portfolio_investments record — the canonical
# field names populated upstream (valuation_method today; the rest by the isolated locator
# when a source provides them).
SIGNAL_KEYS = ('valuation_method', 'isin', 'listing_exchange', 'share_type', 'ipev_level')


@dataclass(frozen=True)
class QuotedClass:
    """One company's quoted/unquoted verdict, its basis, and the positive evidence used."""
    classification: str          # QUOTED | UNQUOTED | HELD
    basis: str                   # STATED | INFERRED | HELD_BASIS
    evidence: str = ''           # the positive evidence relied on (for quoted/unquoted)
    reason: str = ''             # why held (for HELD)

    @property
    def is_quoted(self) -> Optional[bool]:
        if self.classification == QUOTED:
            return True
        if self.classification == UNQUOTED:
            return False
        return None


# ── the ISIN checksum gate (a deterministic anchor, not a heuristic) ─────────
def valid_isin(value) -> bool:
    """True iff `value` is a checksum-valid ISIN (ISO 6166): a 2-letter country code, 9
    alphanumeric characters, and a final Luhn mod-10 check digit computed over the
    letter-expanded (A=10 … Z=35) digit string. A checksum-valid ISIN is a near-certain
    positive identifier of an exchange-listed security — a hard anchor for QUOTED, not a
    guess. A bad length, non-alphanumeric body, or failed checksum ⇒ False.

    SCOPE (documented residual): this validates STRUCTURE + CHECKSUM, not country-code
    AUTHENTICITY — so a deliberately-crafted checksum-valid ISIN with a non-existent prefix
    (e.g. 'ZZ000000000A') passes. classify() treats a lone valid ISIN as sufficient for
    QUOTED (hard_anchor = isin OR exchange), so this residual is live. It is an ACCEPTED call:
    (1) a full security/country registry is brittle, and the checksum makes an ACCIDENTAL
    pass negligible; (2) the overlay is neutral by construction, so a fake-ISIN mislabel only
    moves an FV between buckets (tie still holds) — a mislabel, never a wrong number. The
    available closing lever, if a field-test ever wants it: gate the 2-letter prefix on the
    ISO 3166-1 alpha-2 set (+ 'XS'/'EU'). Deliberately NOT added now."""
    if value is None:
        return False
    s = str(value).strip().upper()
    if len(s) != 12 or not s[:2].isalpha() or not s[2:].isalnum():
        return False
    digits: List[int] = []
    for ch in s:
        if ch.isdigit():
            digits.append(int(ch))
        elif ch.isalpha():
            v = ord(ch) - 55                 # 'A'->10 … 'Z'->35
            digits.append(v // 10)
            digits.append(v % 10)
        else:
            return False
    # Luhn mod-10 from the rightmost digit: rightmost is position 1 (not doubled).
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _ipev_int(v) -> Optional[int]:
    try:
        return int(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


# ── the classification decision logic ────────────────────────────────────────
def classify(signals: Dict[str, Any]) -> QuotedClass:
    """Classify one holding from whatever positive signals are present. See the module
    docstring for the asymmetric, fail-closed policy. Deterministic and side-effect-free."""
    method = signals.get('valuation_method') or ''
    isin = signals.get('isin')
    exchange = signals.get('listing_exchange')
    share_type = signals.get('share_type') or ''
    ipev = _ipev_int(signals.get('ipev_level'))

    isin_ok = valid_isin(isin)
    exch_ok = bool(str(exchange).strip()) if exchange not in (None, '') else False
    share_listed = lexicon.label_matches_any(share_type, _LISTED_SHARE_SYNS)
    share_unlisted = lexicon.label_matches_any(share_type, _UNLISTED_SHARE_SYNS)
    method_quoted = lexicon.label_matches_any(method, _QUOTED_METHOD_SYNS)
    method_private = lexicon.label_matches_any(method, _PRIVATE_METHOD_SYNS)
    ipev1 = ipev == 1
    ipev3 = ipev == 3

    hard_anchor = isin_ok or exch_ok
    stated_listed = share_listed or ipev1
    stated_unlisted = share_unlisted or ipev3

    # 1) a hard listing anchor CONFIRMS quoted — unless the source ALSO explicitly states
    #    unlisted / level-3 (a genuine contradiction we must never silently resolve).
    if hard_anchor:
        if stated_unlisted:
            anch = 'a checksum-valid ISIN' if isin_ok else 'a listing exchange'
            return QuotedClass(HELD, HELD_BASIS, reason=(
                'conflict: %s is present but the source states unlisted / IPEV level 3' % anch))
        ev = 'checksum-valid ISIN' if isin_ok else ('listing exchange %r' % str(exchange).strip())
        return QuotedClass(QUOTED, STATED, evidence=ev)

    # 2) source explicitly states LISTED (share type / IPEV level 1) but carries no hard
    #    anchor to confirm it — accept the source's own statement, unless it also carries
    #    contradicting unlisted / private-method evidence.
    if stated_listed:
        if stated_unlisted or method_private:
            return QuotedClass(HELD, HELD_BASIS, reason=(
                'conflict: source states listed but also carries unlisted / private-method evidence'))
        return QuotedClass(QUOTED, STATED, evidence='source states listed (share type / IPEV level 1)')

    # 3) source explicitly states UNLISTED (share type / IPEV level 3).
    if stated_unlisted:
        return QuotedClass(UNQUOTED, STATED, evidence='source states unlisted (share type / IPEV level 3)')

    # 4) a QUOTED valuation method ('market price') but NO hard anchor and no stated listing
    #    — a claim of quoted-ness we cannot confirm ⇒ HELD (quoted needs a hard anchor).
    if method_quoted and not method_private:
        return QuotedClass(HELD, HELD_BASIS, reason=(
            'valuation method names a market/quoted price but no ISIN or exchange confirms listing'))

    # 5) a POSITIVE private-valuation method and no quoted signal ⇒ INFERRED unquoted.
    if method_private and not method_quoted:
        return QuotedClass(UNQUOTED, INFERRED, evidence=('private valuation method %r' % str(method).strip()))

    # 6) method names BOTH a market price and a private technique ⇒ contradiction ⇒ HELD.
    if method_quoted and method_private:
        return QuotedClass(HELD, HELD_BASIS, reason=(
            'valuation method names both a market price and a private technique'))

    # 7) no positive evidence either way ⇒ HELD (fail-closed; NEVER default to unquoted).
    m = str(method).strip()
    return QuotedClass(HELD, HELD_BASIS, reason=(
        'no positive quoted/unquoted evidence (method: %s)' % (repr(m) if m else 'blank')))


def signals_from_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the classification signals off a portfolio_investments record's `fields`. Reads
    whatever the upstream extractor populated — `valuation_method` today; ISIN / exchange /
    share type / IPEV level when a source provides them (via the isolated locator)."""
    return {k: fields.get(k) for k in SIGNAL_KEYS}


# ── the partition + HARD tie control ─────────────────────────────────────────
@dataclass(frozen=True)
class Partition:
    """Σ of the UNTOUCHED per-company FV, bucketed by classification. `total_cr` is the
    independent grand total over every item the classifier saw (each counted once);
    `bucket_total_cr` is Σ of the three buckets. They are equal by construction, and the
    HARD tie compares `total_cr` to the SEPARATELY-computed Σ portfolio FV — so a company
    whose FV never reached the classifier (a join/drop bug) reddens the gate."""
    quoted_cr: Decimal
    unquoted_cr: Decimal
    held_cr: Decimal
    total_cr: Decimal
    bucket_total_cr: Decimal
    quoted: List[str] = field(default_factory=list)
    unquoted: List[str] = field(default_factory=list)
    held: List[str] = field(default_factory=list)


def partition(items) -> Partition:
    """items: iterable of (company_name, fair_value_cr_or_None, QuotedClass). A held FV
    (None) contributes 0 to its bucket sum — never an implicit 0 inside a trusted total —
    but the company is still recorded in its bucket's name list."""
    q = u = h = total = Decimal('0')
    qn: List[str] = []
    un: List[str] = []
    hn: List[str] = []
    for name, fv, cls in items:
        val = fv if fv is not None else Decimal('0')
        total += val
        iq = cls.is_quoted
        if iq is True:
            q += val
            qn.append(name)
        elif iq is False:
            u += val
            un.append(name)
        else:
            h += val
            hn.append(name)
    return Partition(quoted_cr=q, unquoted_cr=u, held_cr=h, total_cr=total,
                     bucket_total_cr=q + u + h, quoted=qn, unquoted=un, held=hn)


def tie_check(part: Partition, portfolio_fv_cr: Optional[Decimal],
              *, tol: Decimal = Decimal('0.01')) -> dict:
    """HARD control: Σquoted+Σunquoted+Σheld (the partition) must equal the independently
    computed Σ portfolio FV. This is what proves the partition dropped or double-counted no
    company. Indeterminate (not fail) if the reference total is unavailable."""
    lhs = part.total_cr
    if portfolio_fv_cr is None:
        return {'id': 'quoted_unquoted_partition_ties_to_total', 'class': 'hard',
                'status': 'indeterminate', 'lhs': str(lhs), 'rhs': '',
                'detail': 'Σ portfolio FV unavailable (a company FV held) — partition tie deferred'}
    ok = abs(lhs - portfolio_fv_cr) <= tol
    return {'id': 'quoted_unquoted_partition_ties_to_total', 'class': 'hard',
            'status': 'pass' if ok else 'fail', 'lhs': str(lhs), 'rhs': str(portfolio_fv_cr),
            'detail': 'Σquoted+Σunquoted+Σheld (%s) %s Σ portfolio FV (%s)'
                      % (lhs, '=' if ok else '≠', portfolio_fv_cr)}


# ── the ISOLATED source-location parse (spec-tested-until-field-tested) ───────
# LOCATING ISIN / exchange / share-type columns in a REAL listed-holdings sheet is
# field-dependent and cannot be validated until such a fund arrives. It is isolated here so
# that, when one does, only THIS function firms up against the real layout — the decision
# logic, partition, tie, and subsheet above do not change. Built and tested now against
# synthetic fixtures + the ISIN checksum gate; matched WHOLE-WORD via the shared matcher.
_ISIN_HDR_SYNS = ('isin', 'isin code', 'isin no', 'isin number', 'international securities identification number')
_EXCHANGE_HDR_SYNS = ('listing exchange', 'exchange', 'stock exchange', 'listed on', 'bourse')
_SHARE_TYPE_HDR_SYNS = ('share type', 'security type', 'listing status', 'listed unlisted',
                        'quoted unquoted', 'instrument type', 'class of shares')
_LISTING_HDR = {'isin': _ISIN_HDR_SYNS, 'listing_exchange': _EXCHANGE_HDR_SYNS,
                'share_type': _SHARE_TYPE_HDR_SYNS}


def locate_listing_signals(header_cells) -> Dict[str, int]:
    """Given one header row's cells, return {signal_name: column_index} for any ISIN /
    exchange / share-type column present (whole-word header match via the shared matcher).
    Empty when none present — the case for every file we have today. SPEC-TESTED against
    synthetic fixtures; FIELD-VALIDATION deferred to the first real listed-holdings fund.

    INVARIANT THE FIELD-TEST MUST PRESERVE: when this firms up against a real listed holding,
    it may add only CLASSIFICATION EVIDENCE (ISIN / exchange / share-type columns) — it must
    NEVER introduce a value column that recomputes a quoted holding's FV from market data. The
    fund's STATED fair value remains the single source of the number (see the module
    docstring's LABELLING-NEVER-REVALUATION invariant); a divergence is a reconciliation
    disclosure, not an override."""
    found: Dict[str, int] = {}
    for c, cell in enumerate(header_cells):
        if cell is None:
            continue
        for sig, syns in _LISTING_HDR.items():
            if sig not in found and lexicon.label_matches_any(cell, syns):
                found[sig] = c
    return found
