"""
Finance extension — the distribution WATERFALL engine (term- + date-driven) and the
accrued-carry RECONCILIATION that closes v2 Appendix A ("the distribution waterfall
independently reproduces the reported accrued carry").

Discipline (unchanged from the layer):
  • TERM-DRIVEN.  Carry %, hurdle, catch-up and waterfall TYPE are read from the fund's
    own extracted LPA terms — never hardcoded, never assumed European.
  • DATE-DRIVEN.  The preferred return accrues on the ACTUAL drawdown dates (XIRR-style),
    not on period counts.
  • FAIL-CLOSED.  A missing / ambiguous term (no hurdle, unknown waterfall type, no profit
    base, no dated calls) → HOLD, never a guessed default.
  • CHECK, NEVER FIT.  The reported accrued carry is something the INDEPENDENTLY-computed
    value is COMPARED TO — never a target whose unstated conventions we reverse-engineer to
    hit.  When the LPA leaves the hurdle BASIS (committed vs drawn) and COMPOUNDING (simple
    vs compound) and hard-vs-soft UNSTATED — as this fund's terms do — we do NOT pick the
    convention that lands on the reported number.  We compute EVERY plausible convention
    independently and let the OUTCOME adjudicate into one of three honest statuses:

       hard_tie              conventions were STATED → one independent number, compared to
                             reported (pass, else held).  (Not this fund's case.)
       consistent_inferred   conventions unstated, but exactly ONE computed value reproduces
       (SOFT)                reported and the others are clearly off → adopt + emit it,
                             LABELLED "consistent under an inferred convention, NOT
                             independently verified".
       held_underdetermined  several DIFFERENT computed values all sit within the "approx"
                             band → publish the range, HOLD the point figure.
       discrepancy           NO plausible convention reproduces reported → the reconciliation
                             has done its job: HOLD + flag a real discrepancy.

The engine is a pure function of primitives (Decimals + dates); the pipeline wrapper at the
bottom assembles those primitives from the already-extracted CIR objects and emits the
reconciliation as SOFT check rows + disclosures on the existing spine (a held accrued-carry
figure can never sink the workbook — add-on #5).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional, Tuple

# ── unstated-convention axes (enumerated only where the terms are silent) ──────────
# FIVE axes, not three: each moves the accrued-carry number as much as the basis does, so fixing
# any of them silently would understate the true convention cloud and could flip held↔discrepancy.
_BASES = ('drawn', 'committed')                 # preferred return accrues on drawn vs committed capital
_COMPOUNDINGS = ('simple', 'annual_compound')   # simple interest vs annual compounding
_HURDLE_TYPES = ('hard', 'soft')                # permanent LP carve-out vs GP catch-up
_PREF_METHODS = ('contributed', 'net_of_distributions')  # does return-of-capital reduce the accrual base?
# carry base is enumerated from the fund's own figures: gross investment gain vs net-of-fees profit.
# FIXED (not free) sub-conventions, disclosed so the range is honestly labelled conditional on them:
#   • day-count = Actual/365.25   • valuation/measurement date = the run's as_of (a real input, not a convention)

_DAYS_PER_YEAR = Decimal('365.25')
_APPROX_REL_TOL = Decimal('0.05')       # "reproduce" band: a scenario within ±5% of reported reproduces it …
_AMBIGUITY_REL_TOL = Decimal('0.10')    # "ambiguity" band: a runner-up within ±10% makes the inference NOT unique
_APPROX_ABS_FLOOR = Decimal('1')        # … with a ₹1 Cr floor so tiny reported values keep a band
_DISTINCT_Q = Decimal('0.1')            # group computed values to 0.1 Cr when counting distinct outcomes
# The two-band dominance test is the guard against a knife-edge single pick: a single value counts as a clean
# inferred match ONLY when it reproduces reported AND no OTHER materially-different value is even NEAR it. When
# two plausible conventions bracket an "approx" reported figure, that is genuine UNDERDETERMINATION → HOLD, never
# a tolerance-artifact winner. Widening the ambiguity band only ever makes us HOLD MORE (the fail-closed direction).


# ══════════════════════════════════════════════════════════════════════════════════
# PURE ENGINE  (unit-testable in isolation; hand-computable)
# ══════════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Scenario:
    basis: str                          # 'drawn' | 'committed'
    compounding: str                    # 'simple' | 'annual_compound'
    hurdle_type: str                    # 'hard' | 'soft'
    pref_method: str                    # 'contributed' | 'net_of_distributions'
    carry_base: str                     # 'gross_gain' | 'net_of_fees'
    pref_cr: Optional[Decimal]
    carry_cr: Optional[Decimal]
    note: str = ''

    @property
    def label(self) -> str:
        return (f'basis={self.basis}, compounding={self.compounding}, hurdle={self.hurdle_type}, '
                f'pref_method={self.pref_method}, carry_base={self.carry_base}')


@dataclass
class WaterfallInputs:
    carry_rate: Optional[Decimal]                        # 0.20 (VERIFIED rate)
    hurdle_rate: Optional[Decimal]                       # 0.08
    catch_up_rate: Optional[Decimal]                     # 1.0 (100% to GP)
    waterfall_type: str                                  # 'european_whole_fund' | 'american_deal_by_deal' | …
    total_profit_cr: Optional[Decimal]                   # gross investment gain = realised + unrealised gain
    drawn_capital_cr: Optional[Decimal]                  # called capital (returned first)
    committed_capital_cr: Optional[Decimal]              # Σ commitments (committed-basis pref)
    calls: Tuple[Tuple[_dt.date, Decimal], ...]          # dated drawdowns ((date, amount_cr), …)
    as_of: Optional[_dt.date]
    distributions: Tuple[Tuple[_dt.date, Decimal], ...] = ()   # dated net distributions (return-of-capital)
    net_profit_cr: Optional[Decimal] = None              # net-of-fees profit (Σ ITD P&L) — the alt carry base
    hurdle_basis_stated: Optional[str] = None            # None ⇒ UNSTATED ⇒ enumerate both
    compounding_stated: Optional[str] = None
    hurdle_type_stated: Optional[str] = None
    pref_method_stated: Optional[str] = None
    carry_base_stated: Optional[str] = None              # 'gross_gain' | 'net_of_fees'

    @property
    def conventions_all_stated(self) -> bool:
        return all((self.hurdle_basis_stated, self.compounding_stated, self.hurdle_type_stated,
                    self.pref_method_stated, self.carry_base_stated))

    def carry_bases(self) -> List[Tuple[str, Optional[Decimal]]]:
        """The carry-base values to enumerate, from the fund's OWN figures. Gross investment gain
        always; net-of-fees profit additionally when it is available AND materially different."""
        if self.carry_base_stated == 'net_of_fees' and self.net_profit_cr is not None:
            return [('net_of_fees', self.net_profit_cr)]
        if self.carry_base_stated == 'gross_gain':
            return [('gross_gain', self.total_profit_cr)]
        bases = [('gross_gain', self.total_profit_cr)]
        if self.net_profit_cr is not None and self.net_profit_cr != self.total_profit_cr:
            bases.append(('net_of_fees', self.net_profit_cr))
        return bases


@dataclass
class WaterfallVerdict:
    status: str                         # hard_tie | consistent_inferred | held_underdetermined | discrepancy | held_inputs
    computed_cr: Optional[Decimal]      # the number we would emit (None when held)
    reported_cr: Optional[Decimal]
    tolerance_cr: Optional[Decimal]
    inferred_convention: str
    matching: List[Scenario]
    scenarios: List[Scenario]
    detail: str
    range_cr: Optional[Tuple[Decimal, Decimal]] = None


def _years_between(d0: _dt.date, d1: _dt.date) -> Decimal:
    return Decimal((d1 - d0).days) / _DAYS_PER_YEAR


def _accrue(principal: Decimal, rate: Decimal, years: Decimal, compounding: str) -> Decimal:
    """Preferred return earned on `principal` over `years` at `rate`, on the stated basis."""
    if years <= 0:
        return Decimal('0')
    if compounding == 'annual_compound':
        return principal * ((Decimal('1') + rate) ** years - Decimal('1'))
    return principal * rate * years                      # 'simple'


def _accrue_events(events: List[Tuple[_dt.date, Decimal]], rate: Decimal, as_of: _dt.date,
                   compounding: str) -> Decimal:
    """Preferred return accrued on the RUNNING outstanding-capital balance between dated events
    (+contribution raises the base, −return-of-capital lowers it), to as_of. This is the
    'distributions reduce the accrual base' convention."""
    pref = Decimal('0')
    balance = Decimal('0')
    prev: Optional[_dt.date] = None
    for d, amt in sorted(events, key=lambda x: x[0]):
        if prev is not None and balance > 0 and d > prev:
            pref += _accrue(balance, rate, _years_between(prev, d), compounding)
        balance += amt
        if balance < 0:
            balance = Decimal('0')
        prev = d
    if prev is not None and balance > 0 and as_of > prev:
        pref += _accrue(balance, rate, _years_between(prev, as_of), compounding)
    return pref


def preferred_return(inputs: WaterfallInputs, basis: str, compounding: str,
                     pref_method: str = 'contributed') -> Optional[Decimal]:
    """Date-driven preferred return accrued to as_of, on the DRAWN or COMMITTED basis, under the
    chosen accrual METHOD.

    drawn / contributed          → each drawdown accrues from its own call date, no reduction.
    drawn / net_of_distributions → accrues on the running (contributed − returned) balance, so
                                   return-of-capital distributions lower the base.
    committed                    → the full commitment accrues from the FIRST CLOSE (earliest
                                   drawdown as a disclosed proxy); the committed base is not
                                   reduced by distributions, so the accrual method does not apply."""
    if inputs.as_of is None or inputs.hurdle_rate is None:
        return None
    r, as_of = inputs.hurdle_rate, inputs.as_of
    if basis == 'committed':
        if inputs.committed_capital_cr is None or not inputs.calls:
            return None
        first_close = min(d for d, _ in inputs.calls)
        return _accrue(inputs.committed_capital_cr, r, _years_between(first_close, as_of), compounding)
    if basis != 'drawn' or not inputs.calls:
        return None
    if pref_method == 'net_of_distributions':
        events = list(inputs.calls) + [(d, -amt) for d, amt in (inputs.distributions or ())]
        return _accrue_events(events, r, as_of, compounding)
    return sum((_accrue(amt, r, _years_between(d, as_of), compounding) for d, amt in inputs.calls),
               Decimal('0'))


def european_carry(total_profit: Optional[Decimal], pref: Optional[Decimal],
                   carry_rate: Optional[Decimal], catch_up_rate: Optional[Decimal],
                   hurdle_type: str) -> Optional[Decimal]:
    """GP carried interest under a European whole-fund waterfall, given total profit G, the
    preferred-return amount H, carry rate k and catch-up rate q.

      1. return of capital (implicit — G is already profit ABOVE returned capital)
      2. LP preferred: LPs take H
      3. GP catch-up (SOFT hurdle only): GP takes fraction q of the next dollars until GP holds
         k of (pref + catch-up).  Full-catch-up pool X = k·H/(q−k); with q=1,k=0.2 → X=0.25H.
      4. residual: split k to GP.

    HARD hurdle → no catch-up tier; the preferred return is a permanent LP carve-out, so GP
    carry = k·max(0, G−H).  SOFT hurdle with full catch-up collapses to k·G.  These two are the
    materially different numbers the term MUST decide (proves the engine is term-driven)."""
    if total_profit is None or carry_rate is None:
        return None
    G = total_profit if total_profit > 0 else Decimal('0')
    H = pref if pref is not None else Decimal('0')
    if H < 0:
        H = Decimal('0')
    k = carry_rate
    if G <= H:
        return Decimal('0')                              # profit hasn't cleared the hurdle → no carry
    q = catch_up_rate
    if hurdle_type == 'hard' or q is None or q <= k:
        return k * (G - H)                               # hard hurdle (or degenerate catch-up)
    # soft hurdle with catch-up rate q
    x_full = k * H / (q - k)                             # pool needed to fully catch the GP up
    catch_pool = G - H
    if catch_pool <= x_full:
        return q * catch_pool                            # catch-up not completed
    gp_catchup = q * x_full
    residual = G - H - x_full
    return gp_catchup + k * residual


def compute_scenarios(inputs: WaterfallInputs) -> List[Scenario]:
    """One Scenario per (basis × compounding × pref_method × carry_base × hurdle_type), enumerating
    ONLY the axes the terms leave unstated (a stated axis is fixed, not swept). Dedup guards drop
    convention combinations that are IDENTICAL to one already emitted (so the table is the true
    distinct-convention space, never padded with look-alikes)."""
    bases = (inputs.hurdle_basis_stated,) if inputs.hurdle_basis_stated else _BASES
    comps = (inputs.compounding_stated,) if inputs.compounding_stated else _COMPOUNDINGS
    htypes = (inputs.hurdle_type_stated,) if inputs.hurdle_type_stated else _HURDLE_TYPES
    pmeths = (inputs.pref_method_stated,) if inputs.pref_method_stated else _PREF_METHODS
    cbases = inputs.carry_bases()
    out: List[Scenario] = []
    for b in bases:
        for cp in comps:
            for pm in pmeths:
                # net_of_distributions differs from contributed ONLY for drawn basis WITH distributions;
                # otherwise it is identical → skip the duplicate.
                if pm == 'net_of_distributions' and (b == 'committed' or not inputs.distributions):
                    continue
                pref = preferred_return(inputs, b, cp, pm)
                for cbl, cbv in cbases:
                    for ht in htypes:
                        carry = european_carry(cbv, pref, inputs.carry_rate, inputs.catch_up_rate, ht)
                        note = ('committed pref from earliest drawdown (first-close proxy)'
                                if b == 'committed' else '')
                        out.append(Scenario(b, cp, ht, pm, cbl, pref, carry, note))
    return out


def _q(v: Decimal) -> Decimal:
    return (v / _DISTINCT_Q).quantize(Decimal('1')) * _DISTINCT_Q


def adjudicate(scenarios: List[Scenario], reported_cr: Optional[Decimal], *,
               conventions_all_stated: bool,
               rel_tol: Decimal = _APPROX_REL_TOL,
               amb_tol: Decimal = _AMBIGUITY_REL_TOL,
               abs_floor: Decimal = _APPROX_ABS_FLOOR) -> WaterfallVerdict:
    """Compare each independently-computed scenario to the reported figure and resolve into one
    honest status.  NEVER selects a convention BY the reported number: the reported number only
    partitions the PRE-COMPUTED table into reproduce / near / far.  A single value is accepted
    as an inferred match ONLY if it reproduces reported AND is the sole distinct value even NEAR
    it (the dominance test) — otherwise a near-tie runner-up forces a HOLD."""
    usable = [s for s in scenarios if s.carry_cr is not None]
    if reported_cr is None or not usable:
        return WaterfallVerdict('held_inputs', None, reported_cr, None, '', [], scenarios,
                                'reported accrued carry or computed scenarios unavailable — held')
    tol = max(reported_cr.copy_abs() * rel_tol, abs_floor)
    amb = max(reported_cr.copy_abs() * amb_tol, abs_floor * 2)
    matches = [s for s in usable if (s.carry_cr - reported_cr).copy_abs() <= tol]
    contenders = [s for s in usable if (s.carry_cr - reported_cr).copy_abs() <= amb]
    lo_all, hi_all = min(s.carry_cr for s in usable), max(s.carry_cr for s in usable)

    if conventions_all_stated:
        s = usable[0]
        if matches:
            return WaterfallVerdict('hard_tie', s.carry_cr, reported_cr, tol, s.label, matches, scenarios,
                                    f'stated conventions → independent carry {s.carry_cr} ties reported '
                                    f'{reported_cr} within ±{tol}')
        return WaterfallVerdict('discrepancy', s.carry_cr, reported_cr, tol, s.label, [], scenarios,
                                f'stated conventions → independent carry {s.carry_cr} does NOT reproduce '
                                f'reported {reported_cr} (±{tol}) — held + flag')

    # conventions UNSTATED → the scenario table adjudicates
    if not contenders:
        return WaterfallVerdict('discrepancy', None, reported_cr, tol, '', [], scenarios,
                                f'NO plausible convention comes near reported {reported_cr} (±{amb}); computed '
                                f'range [{lo_all}, {hi_all}] across {len(usable)} scenarios — real discrepancy, '
                                f'held + flag', range_cr=(lo_all, hi_all))

    distinct_near = sorted({_q(s.carry_cr) for s in contenders})
    if len(distinct_near) >= 2:                       # ≥2 materially-different conventions bracket the approx figure
        clo = min(s.carry_cr for s in contenders)
        chi = max(s.carry_cr for s in contenders)
        convs = '; '.join(f'{s.label}→{s.carry_cr}' for s in sorted(contenders, key=lambda x: x.carry_cr))
        return WaterfallVerdict('held', None, reported_cr, tol, '', contenders, scenarios,
                                f'UNDERDETERMINED: {len(distinct_near)} materially different conventions sit within '
                                f'the ±{amb} "approx" band of reported {reported_cr} — the reported figure does NOT '
                                f'uniquely identify the convention; point HELD, range [{clo}, {chi}] published '
                                f'[{convs}]', range_cr=(clo, chi))

    if matches:                                       # a single value near reported AND it reproduces within ±tol
        val = matches[0].carry_cr
        convs = '; '.join(sorted({s.label for s in matches}))
        return WaterfallVerdict('consistent_inferred', val, reported_cr, tol, convs, matches, scenarios,
                                f'consistent-under-inferred-convention (NOT independently verified): computed {val} '
                                f'reproduces reported {reported_cr} (±{tol}) under [{convs}]; every other convention '
                                f'is clearly off')

    near = min(contenders, key=lambda s: (s.carry_cr - reported_cr).copy_abs())   # single, near but not reproducing
    return WaterfallVerdict('held', None, reported_cr, tol, near.label, contenders, scenarios,
                            f'nearest convention [{near.label}] computes {near.carry_cr}, close to reported '
                            f'{reported_cr} but outside the ±{tol} reproduce band — held, not confirmed')


# ══════════════════════════════════════════════════════════════════════════════════
# PIPELINE WRAPPER  (assembles primitives from the CIR; emits SOFT reconciliation rows)
# ══════════════════════════════════════════════════════════════════════════════════
def _status_for(verdict: WaterfallVerdict, conventions_stated: bool):
    """Map an adjudication status → (check class, check status).

    `discrepancy` is LOUD — a HARD INDETERMINATE (not a soft footnote): if NO plausible convention
    across the full 5-axis space reproduces the reported figure, either the fund's number is wrong or
    our enumeration is incomplete — both demand attention, so the run holds PARTIAL and the row is
    surfaced prominently. It is INDETERMINATE, never a HARD FAIL: it holds the run, it does not BLOCK
    delivery of everything else (add-on #5 — one held figure cannot sink the workbook).

    `held` (underdetermined / near-miss) and `consistent_inferred` stay SOFT: an "approx" carry
    provision that we can bound but not pin must never block, only disclose."""
    from . import reconcile
    return {
        'hard_tie':             (reconcile.HARD, reconcile.PASS),
        'consistent_inferred':  (reconcile.SOFT, reconcile.PASS),
        'held':                 (reconcile.SOFT, reconcile.INDETERMINATE),
        'held_inputs':          (reconcile.SOFT, reconcile.INDETERMINATE),
        'discrepancy':          (reconcile.HARD, reconcile.INDETERMINATE),
    }[verdict.status]


def reconcile_waterfall(inputs: WaterfallInputs, *, reported_carry: Optional[Decimal],
                        reported_carry_cell: str = '', source_fp: str = ''):
    """Run the scenario table and emit the accrued-carry reconciliation as check + disclosure
    rows.  Returns (checks, disclosures, verdict|None).  Fail-closed at every missing primitive."""
    from . import reconcile
    checks: List[dict] = []
    disc: List[dict] = []

    # ── fail-closed input gates (never a guessed default) ──
    if inputs.carry_rate is None:
        disc.append({'kind': 'waterfall', 'detail': 'carry rate not confirmed — accrued-carry waterfall held'})
        return checks, disc, None
    wt = inputs.waterfall_type or ''
    if wt == 'american_deal_by_deal':
        disc.append({'kind': 'waterfall', 'detail': 'American deal-by-deal waterfall — per-deal entitlement is '
                     'not reconstructable from whole-fund primitives (no per-deal profit split); accrued carry held'})
        return checks, disc, None
    if wt not in ('european_whole_fund',):
        disc.append({'kind': 'waterfall', 'detail': f'waterfall type unresolved ({wt!r}) — accrued carry held '
                     '(fail-closed, never assume European)'})
        return checks, disc, None
    if inputs.hurdle_rate is None:
        disc.append({'kind': 'waterfall', 'detail': 'hurdle/preferred-return term missing — accrued carry held '
                     '(fail-closed, no default hurdle)'})
        return checks, disc, None
    if inputs.total_profit_cr is None:
        disc.append({'kind': 'waterfall', 'detail': 'total ITD gain (realised + unrealised) unavailable — '
                     'accrued carry held'})
        return checks, disc, None
    if not inputs.calls or inputs.as_of is None:
        disc.append({'kind': 'waterfall', 'detail': 'dated drawdowns or as-of date unavailable — preferred '
                     'return cannot accrue on real dates; accrued carry held'})
        return checks, disc, None

    scenarios = compute_scenarios(inputs)
    verdict = adjudicate(scenarios, reported_carry,
                         conventions_all_stated=inputs.conventions_all_stated)
    cls, status = _status_for(verdict, inputs.conventions_all_stated)

    extra = {
        'lhs': '' if verdict.computed_cr is None else str(verdict.computed_cr),
        'rhs': '' if reported_carry is None else str(reported_carry),
        'verdict': verdict.status,
        'inferred_convention': verdict.inferred_convention,
        'tolerance': '' if verdict.tolerance_cr is None else str(verdict.tolerance_cr),
        'independently_verified': verdict.status == 'hard_tie',
        'detail': verdict.detail,
    }
    checks.append(reconcile._result('waterfall_accrued_carry_reconciliation', cls, status, **extra))

    # full scenario table → disclosures (complete transparency; the reader sees every convention)
    for s in scenarios:
        if s.carry_cr is None:
            continue
        disc.append({'kind': 'waterfall_scenario',
                     'detail': f'{s.label}: pref={s.pref_cr}, accrued_carry={s.carry_cr}'
                               + (f' [{s.note}]' if s.note else '')})
    disc.append({'kind': 'waterfall_verdict', 'detail': verdict.detail})
    if inputs.conventions_all_stated is False:
        disc.append({'kind': 'waterfall_convention_gap',
                     'detail': 'FIVE LPA sub-conventions are NOT stated and each moves the number: hurdle BASIS '
                               '(committed vs drawn), COMPOUNDING (simple vs compound), hard-vs-soft, PREF-ACCRUAL '
                               'METHOD (contributed vs net-of-distributions), and CARRY BASE (gross gain vs '
                               'net-of-fees). The figure above is adjudicated across all of them and is NOT a hard '
                               'independent tie — the LPA must be consulted to pin the conventions'})
    # auditability: name the sub-conventions still held FIXED (so the published range is honestly
    # labelled conditional on them, not presented as an absolute bound)
    if inputs.net_profit_cr is None:
        disc.append({'kind': 'waterfall_unmodeled',
                     'detail': 'carry-base = net-of-fees NOT enumerated (net-of-fees profit unavailable this run) — '
                               'only gross-gain base modelled; the true range may be wider'})
    disc.append({'kind': 'waterfall_fixed_conventions',
                 'detail': f'FIXED sub-conventions (disclosed, not swept): day-count = Actual/365.25; '
                           f'valuation/measurement date = as_of {inputs.as_of}. The range is conditional on these'})
    # NAV cross-annotation: the fund's LP-net NAV embeds this accrued-carry provision, so an
    # underdetermined/held carry propagates a sensitivity onto the NAV that must not be hidden.
    if verdict.status in ('held', 'discrepancy') and verdict.range_cr is not None:
        lo, hi = verdict.range_cr
        disc.append({'kind': 'nav_carry_sensitivity',
                     'detail': f'the LP-net NAV subtracts this accrued-carry provision; because the provision is '
                               f'{verdict.status} across [{lo}, {hi}] (width {hi - lo}), the LP NAV carries that '
                               f'same ±sensitivity — do not present LP NAV as firm while its carry component is held'})
    if verdict.status == 'discrepancy':
        disc.append({'kind': 'waterfall_discrepancy',
                     'detail': f'LOUD FLAG — reported accrued carry {reported_carry} is reproduced by NO plausible '
                               f'convention in the {len([s for s in scenarios if s.carry_cr is not None])}-scenario '
                               f'space; either the reported figure is wrong or the enumeration is incomplete. '
                               f'Investigate before relying on the carry / NAV.'})
    return checks, disc, verdict


# ══════════════════════════════════════════════════════════════════════════════════
# CLAWBACK  (rides the waterfall entitlement)
# ══════════════════════════════════════════════════════════════════════════════════
@dataclass
class ClawbackResult:
    status: str                                  # clean_zero | exposure | exposure_range | held
    clawback_cr: Optional[Decimal]
    range_cr: Optional[Tuple[Decimal, Decimal]]
    carry_received_cr: Optional[Decimal]
    detail: str


def clawback_exposure(*, carry_received: Optional[Decimal], received_complete: bool,
                      entitled_point: Optional[Decimal], entitled_range: Optional[Tuple[Decimal, Decimal]],
                      holdback_rate: Optional[Decimal] = None) -> ClawbackResult:
    """GP clawback = max(0, cumulative carry RECEIVED − carry ENTITLED at true-up), net of any
    escrow/holdback reserve.

    FAIL-CLOSED: an incomplete / unreadable received history is HELD, never asserted 0.

    KEY CORRECTNESS POINT: when carry RECEIVED is definitively 0, clawback is provably 0 REGARDLESS
    of the entitlement (0 − anything_nonneg ≤ 0) — so a clean zero holds even while the accrued
    entitlement itself is underdetermined/held (as it is on this fund)."""
    if carry_received is None or not received_complete:
        return ClawbackResult('held', None, None, carry_received,
                              'GP carry-received history incomplete/unreadable — clawback HELD (never asserted 0)')
    if carry_received <= 0:
        return ClawbackResult('clean_zero', Decimal('0'), None, carry_received,
                              'no GP carry distributed to date → clawback provably 0 regardless of entitlement')

    def _net(cb: Decimal) -> Decimal:
        if holdback_rate is not None and holdback_rate > 0:
            return max(Decimal('0'), cb - holdback_rate * carry_received)   # escrowed portion reduces net exposure
        return cb

    if entitled_point is not None:
        cb = _net(max(Decimal('0'), carry_received - entitled_point))
        if cb <= 0:
            return ClawbackResult('clean_zero', Decimal('0'), None, carry_received,
                                  f'carry received {carry_received} ≤ entitlement {entitled_point} — no clawback')
        return ClawbackResult('exposure', cb, None, carry_received,
                              f'GP clawback exposure {cb}: received {carry_received} exceeds entitlement {entitled_point}')
    if entitled_range is not None:
        lo, hi = entitled_range
        cb_hi = _net(max(Decimal('0'), carry_received - lo))               # lower entitlement → higher clawback
        cb_lo = _net(max(Decimal('0'), carry_received - hi))
        if cb_hi <= 0:
            return ClawbackResult('clean_zero', Decimal('0'), None, carry_received,
                                  f'carry received {carry_received} ≤ entitlement range [{lo}, {hi}] — no clawback')
        return ClawbackResult('exposure_range', None, (cb_lo, cb_hi), carry_received,
                              f'GP clawback exposure bounded [{cb_lo}, {cb_hi}] — entitlement underdetermined '
                              f'[{lo}, {hi}], point held')
    return ClawbackResult('held', None, None, carry_received,
                          'carry received > 0 but entitlement not computable — clawback HELD')


def carry_received_from_distributions(dist_records) -> Tuple[Optional[Decimal], bool]:
    """Σ GP carry actually distributed, from the distributions ledger's gp_carry column. Returns
    (sum_cr, complete). complete=False if any gp_carry figure is HELD (fail-closed); a ledger that
    is entirely absent → (None, False) so the clawback holds rather than assuming zero."""
    from .cir import Figure
    if not dist_records:
        return None, False
    total = Decimal('0')
    complete = True
    for rec in dist_records:
        f = rec.fields.get('gp_carry')
        if isinstance(f, Figure):
            if f.confirmed:
                total += f.value_cr
            else:
                complete = False
        # a distributions row with no gp_carry value = 0 carry taken that payout (explicit ledger present)
    return (total if complete else None), complete


def reconcile_clawback(*, carry_received: Optional[Decimal], received_complete: bool,
                       verdict: Optional[WaterfallVerdict], holdback_rate: Optional[Decimal] = None):
    """Emit the clawback reconciliation as SOFT rows. `verdict` (the waterfall WaterfallVerdict, or
    None if the waterfall held) supplies the entitlement point/range. Returns (checks, disclosures)."""
    from . import reconcile
    entitled_point = entitled_range = None
    if verdict is not None:
        if verdict.status in ('hard_tie', 'consistent_inferred'):
            entitled_point = verdict.computed_cr
        elif verdict.range_cr is not None:
            entitled_range = verdict.range_cr
    res = clawback_exposure(carry_received=carry_received, received_complete=received_complete,
                            entitled_point=entitled_point, entitled_range=entitled_range,
                            holdback_rate=holdback_rate)
    status_map = {'clean_zero': reconcile.PASS, 'exposure': reconcile.FAIL,
                  'exposure_range': reconcile.FAIL, 'held': reconcile.INDETERMINATE}
    extra = {
        'clawback': '' if res.clawback_cr is None else str(res.clawback_cr),
        'range': '' if res.range_cr is None else f'[{res.range_cr[0]}, {res.range_cr[1]}]',
        'carry_received': '' if res.carry_received_cr is None else str(res.carry_received_cr),
        'verdict': res.status,
        'detail': res.detail,
    }
    checks = [reconcile._result('gp_clawback_reconciliation', reconcile.SOFT, status_map[res.status], **extra)]
    disc = [{'kind': 'clawback', 'detail': res.detail}]
    if holdback_rate is not None and holdback_rate > 0:
        moot = ' (moot at zero distributions)' if (carry_received or Decimal('0')) <= 0 else ''
        disc.append({'kind': 'clawback_holdback',
                     'detail': f'escrow/holdback reserve {holdback_rate} of gross carry reduces net clawback '
                               f'exposure{moot}'})
    return checks, disc


# ── primitive assembly from the already-extracted CIR objects ──────────────────────
def _term_value(canonical_terms, concept):
    if canonical_terms is None:
        return None
    t = canonical_terms.fields.get(concept)
    from .fund_terms import FundTerm
    if isinstance(t, FundTerm) and t.confirmed and t.value is not None:
        return t.value
    return None


def _waterfall_type(canonical_terms):
    if canonical_terms is None:
        return ''
    from .fund_terms import FundTerm
    t = canonical_terms.fields.get('waterfall')
    if isinstance(t, FundTerm):
        return t.qualifiers.get('waterfall_type', '') or ''
    return ''


def _dated_amounts(records, amount_concept):
    """[(date, amount_cr), …] from ledger records carrying a dated confirmed money column."""
    from .cir import Figure
    from . import nav
    out: List[Tuple[_dt.date, Decimal]] = []
    for rec in (records or []):
        d = nav._parse_date(rec.fields.get('date'))
        amt = rec.fields.get(amount_concept)
        if d is not None and isinstance(amt, Figure) and amt.confirmed:
            out.append((d, amt.value_cr))
    out.sort(key=lambda x: x[0])
    return out


def build_waterfall_inputs(*, canonical_terms, nav_inputs, capital, committed_base,
                           call_records, dist_records=None, as_of):
    """Assemble WaterfallInputs + the reported accrued carry from the CIR objects the pipeline
    already holds.  Returns (WaterfallInputs, reported_carry_cr, reported_carry_cell)."""
    from .cir import Figure
    from . import nav

    # gross-gain carry base, net-of-fees carry base, and the REPORTED accrued carry all come from
    # the P&L-bearing NAV source: gross = realised gain + unrealised gain; net-of-fees = Σ ITD P&L.
    pnl_src = next((ni for ni in (nav_inputs or []) if ni.realised and ni.unrealised), None)
    if pnl_src is None:
        pnl_src = next((ni for ni in (nav_inputs or []) if ni.carry), None)
    total_profit = net_profit = reported_carry = None
    reported_cell = ''
    if pnl_src is not None:
        if pnl_src.realised and pnl_src.unrealised:
            total_profit = pnl_src.realised[0] + pnl_src.unrealised[0]
        if pnl_src.pnl:
            net_profit = sum((v for _, v, _ in pnl_src.pnl), Decimal('0'))
        if pnl_src.carry:
            reported_carry, reported_cell = pnl_src.carry[0], pnl_src.carry[1]

    called = None
    if capital is not None:
        cf = capital.fields.get('called')
        if isinstance(cf, Figure) and cf.confirmed:
            called = cf.value_cr

    inputs = WaterfallInputs(
        carry_rate=_term_value(canonical_terms, 'carried_interest'),
        hurdle_rate=_term_value(canonical_terms, 'hurdle_rate'),
        catch_up_rate=_term_value(canonical_terms, 'catch_up'),
        waterfall_type=_waterfall_type(canonical_terms),
        total_profit_cr=total_profit,
        drawn_capital_cr=called,
        committed_capital_cr=committed_base,
        calls=tuple(_dated_amounts(call_records, 'amount')),
        as_of=nav._as_of_date(as_of) if as_of else None,
        distributions=tuple(_dated_amounts(dist_records, 'net')),
        net_profit_cr=net_profit,
    )
    return inputs, reported_carry, reported_cell
