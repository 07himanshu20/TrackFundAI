"""
CARRY-FORWARD 2 — unit / currency resolution (a correctness blocker: scale and
currency multiply the number). Falling back to structural hints when the model
leaves them null is necessary but NOT sufficient — the real files show the sheet
sometimes states the WRONG unit:
    CSS said SGD'000 but values were full SGD;
    CPM / Analisa said MYR'000 but were absolute;
    CPC said 'Value in Million'.
A null-fallback cannot catch a MISLABELLED unit — only magnitude can.

So resolution is held to U2's standard — independent evidence, escalate rather
than guess, learn once:

  1. Deterministic precedence — declared (model) > column/header hint > sheet
     hint. Two STRONG sources that disagree → escalate (never silently pick one).
  2. MAGNITUDE plausibility cross-check — given the resolved scale, is the value
     in a sane band around an independent anchor (the fund's known cost basis /
     fair value for that entity)? If the declared/hinted scale disagrees with the
     magnitude but a different scale fits, the LABEL is not trusted → escalate,
     carrying the suggested scale so a human resolves it instantly.
  3. GEOGRAPHY cross-check — the schedule's domicile implies an expected currency
     (Singapore→SGD, Malaysia→MYR, India→INR). A detected currency that
     contradicts the domicile is flagged (free, catches real errors).

A resolution that clears every check is trusted; anything ambiguous is escalated
and its answer stored against the layout fingerprint (learn once) by the caller.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

from .quantity import SCALE_TO_ABS, normalise_scale, to_decimal
from . import currency_ledger

# domicile → expected currency (universal country/currency map, not fund-specific)
GEO_CCY = {
    'india': 'INR', 'singapore': 'SGD', 'malaysia': 'MYR', 'usa': 'USD',
    'united states': 'USD', 'us': 'USD', 'uk': 'GBP', 'united kingdom': 'GBP',
    'uae': 'AED', 'europe': 'EUR', 'germany': 'EUR', 'france': 'EUR',
    'indonesia': 'IDR', 'thailand': 'THB', 'vietnam': 'VND',
}

# scales tried when magnitude-testing an alternative to the declared one — the
# common mislabels are clean powers of ten (absolute ↔ thousands ↔ lakhs ↔ mn).
_ALT_SCALES = ['absolute', 'thousands', 'lakhs', 'millions', 'crore']

# A declared scale is not trusted if it differs from the anchor's BEST-FIT scale
# by ≥ this many orders of magnitude. 2 orders (100×) cleanly separates a real
# scale mislabel (the 1000× thousands-vs-absolute class) from ordinary
# revenue-vs-cost dispersion, which stays well within an order of magnitude.
_ORDERS_MISMATCH = 2.0

# The anchor is only a COARSE decade-gate. Internal scale-consistency (all figures
# on a statement share one scale) is the primary evidence; the anchor just fixes
# WHICH absolute decade (rupees vs thousands vs lakhs). Judged against the LARGEST
# figure (which tracks company scale — see resolve_statement_scale), a generous
# ±1.5-order (≈31×) window is right: a revenue-to-valuation multiple ranges from
# ~0.5× to 100×+, so the top line legitimately sits well below EV for a deep-tech
# or early-SaaS. When even this wide gate leaves two decades plausible, HOLD.
_ANCHOR_ORDERS = 1.5


@dataclass
class StatementScale:
    scale: Optional[str]
    currency: str
    escalate: bool
    reason: str


@dataclass
class MonetaryFrame:
    """Scale AND currency resolved together — the two halves of one 'monetary
    frame'. A frame is resolved ONCE per statement and applied to every figure
    on it; if EITHER half is unresolved the whole frame escalates and all the
    statement's money figures are held together (never one emitted, one held)."""
    currency: Optional[str]
    scale: Optional[str]
    escalate: bool
    reason: str
    flags: List[str] = field(default_factory=list)


def resolve_currency(*, stmt_currency, geo_currency, inr_mentioned, base_currency=None):
    """Resolve ONE currency for a whole statement, geography-gated. Precedence:
      1. the statement's OWN header currency (strongest local evidence), UNLESS a known
         domicile OR a user-confirmed base currency contradicts it → conflict, hold.
      2. else the entity's geography-implied currency (its domicile).
      3. else the USER-CONFIRMED base currency (a batch/fund-level assertion, e.g. "this
         portfolio reports in INR") — positive evidence supplied by the user, disclosed as
         such so an emit resting on it is distinguishable from a file-detected one, and any
         file whose OWN statement token disagrees still conflicts → holds (the fail-closed
         guard that keeps a genuinely-foreign file from ever taking the batch currency).
      4. else HOLD — no positive evidence; there is NO INR default.

    Currency is CONFIRMED only by POSITIVE, NON-CONFLICTING evidence — a statement
    token, or a known domicile. Any statement-token-vs-domicile disagreement is a
    conflict (a Singapore entity does not report in INR; an INR-domicile statement
    stamped MYR is equally unproven) → AMBIGUOUS, held, never auto-applied in EITHER
    direction. Absence of evidence (no token AND no known domicile) is AMBIGUOUS too —
    a workbook-wide INR mention is not on-figure/header evidence and can never currency
    an unknown-domicile figure (a foreign file's FX note names rupees). This closes the
    untokened-foreign hole: a foreign figure with no marker must never silently ship as
    INR (a 15-20× error). Returns (currency, escalate, reason, flags).

    This is the SINGLE complete choke for currency detection (U6 Phase-2 enumeration): every
    site that can yield a foreign or conflicting currency reaches it. The observing wrapper
    records each verdict into the run's currency ledger so the uncovered-currency report is
    complete by construction — capturing DOMICILE-implied currencies a token scan would miss."""
    result = _resolve_currency(stmt_currency=stmt_currency, geo_currency=geo_currency,
                               inr_mentioned=inr_mentioned, base_currency=base_currency)
    currency_ledger.observe(currency=result[0], escalate=result[1], reason=result[2], flags=result[3])
    return result


def _resolve_currency(*, stmt_currency, geo_currency, inr_mentioned, base_currency=None):
    flags: List[str] = []
    # The confirmer that a statement token must AGREE with: a known domicile (more authoritative,
    # entity-specific) if present, else the user-confirmed base currency. A file whose own statement
    # token disagrees with the confirmer is a genuine conflict → hold (this is what keeps a
    # genuinely-foreign file — a foreign token on its statement — from ever taking the batch currency).
    confirmer, confirmer_src = ((geo_currency, 'domicile') if geo_currency
                                else (base_currency, 'user-confirmed base currency'))
    # Rule (i): a statement-header token is positive local evidence — trusted UNLESS the confirmer
    # contradicts it (either direction), which is a genuine conflict that cannot be resolved → hold.
    if stmt_currency:
        if confirmer and stmt_currency != confirmer:
            return (None, True,
                    f'statement shows {stmt_currency} but {confirmer_src} implies {confirmer} — currency '
                    f'conflict, cannot confirm; hold',
                    [f'currency_conflict_{stmt_currency}_vs_{confirmer_src.split()[0]}_{confirmer}'])
        return stmt_currency, False, f'statement-header currency {stmt_currency}', flags
    # Rule (ii): no token, but a known domicile implies its currency. A workbook INR mention never
    # overrides a foreign domicile.
    if geo_currency:
        if inr_mentioned and geo_currency != 'INR':
            flags.append('inr_mention_ignored_foreign_domicile')
        return geo_currency, False, f'domicile-implied currency {geo_currency}', flags
    # Rule (iii): no token AND no domicile, but the USER CONFIRMED a base currency for the batch/fund.
    # A positive user assertion — trusted, but DISCLOSED as user-confirmed so a number resting on it is
    # auditable (and a wrong assertion on one file is catchable) distinct from a file-detected currency.
    if base_currency:
        return base_currency, False, f'user-confirmed base currency {base_currency}', ['currency_user_confirmed']
    # Rule (iv): NO positive evidence — no statement token, no known domicile, no user confirmation.
    # AMBIGUOUS ⇒ hold, never INR-by-default/by-mention (the untokened-foreign hole). inr_mentioned is
    # deliberately NOT trusted here: it is workbook-wide, not on the figure/header, and a foreign
    # statement carries rupee FX notes too.
    return (None, True, 'no positive currency evidence (no statement token, no known domicile, '
            'no user-confirmed base currency) — ambiguous, hold', ['currency_ambiguous_no_evidence'])


def resolve_monetary_frame(*, stmt_currency, geo_currency, inr_mentioned, declared_unit,
                           sample_values, anchor_cr, ratecard, base_currency=None):
    """Resolve the statement's monetary frame (currency THEN scale) as one unit.
    Currency is geography-gated (resolve_currency, incl. a user-confirmed base currency);
    scale is anchor-gated (resolve_statement_scale) using that currency. If either half is
    unresolved the frame escalates — the caller holds ALL the statement's money figures."""
    ccy, ccy_esc, ccy_reason, ccy_flags = resolve_currency(
        stmt_currency=stmt_currency, geo_currency=geo_currency, inr_mentioned=inr_mentioned,
        base_currency=base_currency)
    if ccy_esc:
        return MonetaryFrame(None, None, True, ccy_reason, ccy_flags)
    ss = resolve_statement_scale(declared_unit=declared_unit, currency=ccy,
                                 sample_values=sample_values, anchor_cr=anchor_cr, ratecard=ratecard)
    if ss.escalate or ss.scale is None:
        return MonetaryFrame(ccy, None, True, f'{ccy_reason}; scale: {ss.reason}', ccy_flags)
    # A resolved FOREIGN currency needs a rate to become ₹Cr. If the card does not cover it, hold the
    # whole statement HERE (FX_UNCOVERED) — fail-closed and UNIFORM across every emit path, so the
    # conversion is never attempted on an uncovered currency and to_inr never raises for a missing rate.
    # This keeps a benign 'awaiting a rate' hold cleanly distinct from a genuine code fault (which stays
    # a broad-except UNEXPECTED_ERROR). Neutral on the emit set: a foreign figure could only ever emit
    # WITH a rate, so a covered currency still resolves and an uncovered one was already held.
    if ccy and ccy != 'INR':
        rates = getattr(ratecard, 'rates', None) or {}
        if ccy not in rates:
            return MonetaryFrame(ccy, ss.scale, True,
                                 f'{ccy_reason}; FX_UNCOVERED: no rate for {ccy} in the card — hold',
                                 list(ccy_flags) + [f'fx_uncovered_{ccy}'])
    return MonetaryFrame(ccy, ss.scale, False, f'{ccy_reason}; {ss.reason}', ccy_flags)


def whole_company_anchor(*, cost_cr=None, ownership_frac=None, fair_value_cr=None):
    """The scale anchor must be a WHOLE-COMPANY magnitude, because the figures it
    gates (revenue, EBITDA, cash) are whole-company. Both the fund's COST and its
    FV are for a MINORITY stake (14–26%), so raw cost systematically undershoots
    whole-company scale and false-holds high-revenue companies. Gross up by
    ownership to the implied whole-company valuation.

    Prefer cost ÷ ownership (implied ENTRY valuation) over FV ÷ ownership:
      • cost is stable; FV is inflated by markups, pushing the anchor too high so
        the ±1.5-order gate can mis-resolve a high-multiple growth company's scale
        by 1000× (e.g. anchor 161 → picks 'thousands' 4000 for a true ₹4 Cr
        revenue), whereas the entry-valuation anchor keeps only the true scale in
        band.
      • FV is also circular — it is itself a figure we cross-check, not a ruler.
    FV ÷ ownership is only a fallback when cost is absent. Returns ₹Cr or None."""
    def _grossed(v):
        if v is None:
            return None
        v = to_decimal(v)
        if v is None or v <= 0:
            return None
        if ownership_frac and 0 < float(ownership_frac) <= 1:
            return v / Decimal(str(ownership_frac))
        return v
    return _grossed(cost_cr) or _grossed(fair_value_cr)


def _declared_scale_is_magnitude_lie(scale, currency, sample_values, anchor_cr, ratecard):
    """The EMIT-PATH anchor cross-check the declared-unit branch previously bypassed
    (the bug behind premise #2: 'declared units lie'). Given a whole-statement declared
    `scale`, is it a magnitude LIE against the whole-company anchor? Mirrors the per-figure
    resolve() test EXACTLY: the declared scale places the statement's top line (the
    largest money sample — the EV-comparable figure) ≥_ORDERS_MISMATCH decades from the
    anchor WHILE a different scale sits <1 decade from it. Returns the reason string (a lie
    → the caller HOLDS the whole statement, fail-closed — never ship a 10^k-wrong number),
    or None (trust the label). ESCALATES rather than repairs: overriding a declared unit to
    a coarse-anchor guess could mis-scale an HONEST statement whose top line is legitimately
    far below its valuation (a deep-tech/early-SaaS high multiple) — the dangerous direction.
    NOTE: a 10× (lakhs↔millions) lie whose true scale sits <1 decade from the anchor lands
    the declared scale <2 decades off → below this gate: it is BELOW the anchor's resolving
    power on the emit path (mitigated by the cross-sheet reconciler on MULTI-source stocks
    and by the single-source 'un-cross-checked' disclosure — never claimed as verified)."""
    if not anchor_cr or ratecard is None:
        return None
    samples = [abs(to_decimal(v)) for v in (sample_values or []) if to_decimal(v) not in (None,)]
    samples = [s for s in samples if s and s != 0]
    if not samples:
        return None
    rep = max(samples)
    anchor = float(anchor_cr)

    def cr(sc):
        return float(ratecard.to_inr(rep * SCALE_TO_ABS[sc], currency)) / 1e7

    def orders(sc):
        c = cr(sc)
        return abs(math.log10(c / anchor)) if c > 0 else 99.0
    try:
        best = min(_ALT_SCALES, key=orders)
        if orders(scale) >= _ORDERS_MISMATCH and orders(best) < 1.0 and best != scale:
            return (f'declared unit → ₹{cr(scale):.1f}Cr top line, {orders(scale):.1f} decades off '
                    f'anchor ₹{anchor:.0f}Cr while {best!r} fits (<1) — declared scale not trusted, hold')
    except Exception as e:  # noqa: BLE001 — no FX rate → cannot check, do not override the label
        return None
    return None


def resolve_statement_scale(*, declared_unit, currency, sample_values, anchor_cr, ratecard):
    """Resolve ONE scale for a whole statement (all its figures share it). Order:
      1. an explicit unit label on the statement wins — UNLESS the anchor proves it a
         magnitude lie (_declared_scale_is_magnitude_lie), then the whole statement HOLDS
         (fail-closed; the emit-path fix for premise #2 — a lying header must not ship a
         10^k-wrong number, and the anchor cross-check that lived only in the per-figure
         resolve() now guards the statement-scale path the extractor actually uses).
      2. no label → the magnitude ANCHOR (the company's implied whole-company
         valuation) decides, as an ASYMMETRIC order-of-magnitude gate: the
         representative figure may sit far BELOW the anchor (small operating scale
         vs valuation is normal) but only ~1 order ABOVE it (no operating figure
         is many× enterprise value). Exactly one scale in band → resolve; two
         plausible or none → escalate the WHOLE statement (every figure held
         together, never revenue-held-but-EBITDA-emitted, never a plausible guess).
    The anchor validates the magnitude band, never the exact value (revenue may
    be several× cost or a small fraction of enterprise value)."""
    currency = (currency or 'INR')
    if declared_unit:
        sc = normalise_scale(declared_unit)
        if sc:
            lie = _declared_scale_is_magnitude_lie(sc, currency, sample_values, anchor_cr, ratecard)
            if lie:
                return StatementScale(None, currency, True, lie)
            return StatementScale(sc, currency, False, f'declared unit {declared_unit!r}')
        return StatementScale(None, currency, True, f'declared unit {declared_unit!r} unrecognised')

    samples = [abs(to_decimal(v)) for v in sample_values if to_decimal(v) not in (None,)]
    samples = [s for s in samples if s and s != 0]
    if not samples:
        return StatementScale(None, currency, True, 'no sample values to resolve statement scale')
    # Representative = the LARGEST figure (the top line), because scale must be
    # judged against a figure whose magnitude tracks enterprise value. A median
    # over figures that include a near-breakeven EBITDA or thin cash balance sits
    # far below EV and lets the anchor mis-pick a 1000×-too-large scale; the top
    # line (revenue/opex) is the EV-comparable one.
    rep = max(samples)
    if not anchor_cr or ratecard is None:
        return StatementScale(None, currency, True,
                              'no unit label and no cost-basis anchor — statement scale unresolved (hold)')
    anchor = float(anchor_cr)

    def cr(sc):
        return float(ratecard.to_inr(rep * SCALE_TO_ABS[sc], currency)) / 1e7

    def in_band(sc):
        c = cr(sc)
        return c > 0 and abs(math.log10(c / anchor)) <= _ANCHOR_ORDERS
    try:
        fits = [sc for sc in _ALT_SCALES if in_band(sc)]
    except Exception as e:  # noqa: BLE001 — no FX rate for this currency → hold, never crash
        return StatementScale(None, currency, True,
                              f'cannot magnitude-check {currency} scale (no FX rate): {e}')
    if len(fits) == 1:
        return StatementScale(fits[0], currency, False, f'scale {fits[0]!r} resolved by cost-basis anchor')
    if not fits:
        return StatementScale(None, currency, True,
                              f'no scale places the top line within ±{_ANCHOR_ORDERS} orders of '
                              f'anchor ₹{anchor:.0f}Cr — hold')
    return StatementScale(None, currency, True,
                          f'scales {fits} all plausible vs anchor ₹{anchor:.0f}Cr — ambiguous decade, hold')


@dataclass
class UnitResolution:
    scale: str
    currency: str
    escalate: bool = False
    flags: List[str] = field(default_factory=list)
    reason: str = ''
    suggested_scale: Optional[str] = None


def expected_currency(domicile) -> Optional[str]:
    if not domicile:
        return None
    d = str(domicile).strip().lower()
    for k, v in GEO_CCY.items():
        if k in d:
            return v
    return None


def _first_scale(hints) -> Optional[str]:
    for h in (hints or []):
        s = normalise_scale(h)
        if s:
            return s
    return None


def _first_ccy(hints) -> Optional[str]:
    for h in (hints or []):
        c = str(h).strip().upper()
        if c and c.isalpha() and len(c) == 3:
            return c
    return None


def resolve(*, value, declared_unit=None, declared_ccy=None, header_hints=None,
            sheet_hints=None, domicile=None, anchor_cr=None, ratecard=None) -> UnitResolution:
    """Resolve scale + currency for one figure. `anchor_cr` is an independent
    magnitude anchor (the entity's known cost basis / FV in ₹Cr) enabling the
    mislabel cross-check; `ratecard` converts native→INR for that test."""
    flags: List[str] = []

    # ── currency: precedence declared > header > sheet > domicile ────────
    ccy_declared = (declared_ccy or '').strip().upper() or None
    ccy_hint = _first_ccy(header_hints) or _first_ccy(sheet_hints)
    ccy_geo = expected_currency(domicile)
    currency = ccy_declared or ccy_hint or ccy_geo or 'INR'
    if not ccy_declared and not ccy_hint and not ccy_geo:
        flags.append('currency_assumed_inr')
    # geography cross-check (soft flag, not escalate — a co may report in USD)
    if ccy_geo and currency != ccy_geo:
        flags.append(f'currency_{currency}_vs_domicile_{ccy_geo}')

    # ── scale: precedence declared > header > sheet ─────────────────────
    scale_declared = normalise_scale(declared_unit) if declared_unit else None
    scale_header = _first_scale(header_hints)
    scale_sheet = _first_scale(sheet_hints)
    # a declared unit that is present but unrecognised is itself an escalation
    if declared_unit and scale_declared is None:
        return UnitResolution('absolute', currency, escalate=True,
                              flags=flags + ['declared_unit_unrecognised'],
                              reason=f'declared unit {declared_unit!r} unrecognised — escalate')
    # conflict between two STRONG sources (declared vs header) → escalate
    if scale_declared and scale_header and scale_declared != scale_header:
        return UnitResolution(scale_declared, currency, escalate=True,
                              flags=flags + ['scale_conflict'],
                              reason=f'declared {scale_declared} vs header {scale_header} conflict — escalate')
    scale = scale_declared or scale_header or scale_sheet
    if scale is None:
        scale = 'absolute'
        flags.append('scale_assumed_absolute')

    # ── magnitude plausibility cross-check (needs an anchor + ratecard) ──
    if anchor_cr and ratecard is not None:
        v = to_decimal(value)
        anchor = float(anchor_cr)
        if v is not None and v != 0 and anchor > 0:
            try:
                def cr_under(sc):
                    native = abs(v) * SCALE_TO_ABS[sc]
                    return float(ratecard.to_inr(native, currency)) / 1e7
                def orders_from_anchor(sc):
                    c = cr_under(sc)
                    return abs(math.log10(c / anchor)) if c > 0 else 99.0
                best = min(_ALT_SCALES, key=orders_from_anchor)
                # trust the declared/hinted scale unless it sits ≥2 orders from the
                # anchor AND a different scale sits within one order (a real mislabel).
                if orders_from_anchor(scale) >= _ORDERS_MISMATCH and orders_from_anchor(best) < 1.0 and best != scale:
                    return UnitResolution(scale, currency, escalate=True,
                                          flags=flags + ['magnitude_mismatch'],
                                          suggested_scale=best,
                                          reason=(f'scale {scale!r} → ₹{cr_under(scale):.1f}Cr, '
                                                  f'{orders_from_anchor(scale):.1f} orders off anchor '
                                                  f'₹{anchor:.0f}Cr; {best!r} fits — escalate, do not trust the label'))
            except Exception:  # noqa: BLE001 — no FX rate for this currency → cannot cross-check the scale;
                flags.append('magnitude_unchecked_no_fx')  # keep the precedence scale, never crash (mirrors
        # else value 0 / no anchor magnitude → nothing to test  # the wrapped statement-scale path 230/284)
    elif not anchor_cr:
        flags.append('magnitude_unchecked_no_anchor')

    return UnitResolution(scale, currency, escalate=False, flags=flags,
                          reason='resolved by precedence' + (' + magnitude ok' if anchor_cr else ''))
