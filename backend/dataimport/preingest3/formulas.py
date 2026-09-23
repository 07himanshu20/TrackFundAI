"""The ONE preingest3 derivation module — formula.html §2–§5 in Decimal.

Why this file exists: the codebase already had FOUR drifting XIRR copies and three
MOIC/TVPI copies across the legacy phases (phase4_derivations, single_call_extractor,
preingest2/formulas, phase2_persister, phase3_layers). Hand-rolling a fifth inside
preingest3 would re-arm the exact drift trap. Instead every derivation the clean
pipeline needs routes through here, in Decimal, and is PARITY-GATED against the
validated preingest2/formulas.py to the rupee (tests/test_formula_parity.py) — where
formula.html and preingest2 diverge, formula.html governs and the divergence is
documented in the parity test, never silently absorbed.

This module is PURE: it takes explicit typed inputs and returns Decimals (or None
when an input is missing — never a fabricated value, never an implicit 0). The CIR
adapter that reads Figures, applies fail-closed worst-input-state propagation, and
wraps results back into provenance-carrying Figures lives in aggregate_metrics.py;
keeping the math pure is what makes the parity gate clean.

Section anchors below cite formula.html by number so a reader can diff the two.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Sequence, Tuple

D = Decimal
_Z = D('0')

# ── sanity gates (formula.html §6 "Sanity Gates") ────────────────────────────
IRR_MIN, IRR_MAX = D('-0.9999'), D('5.0')        # −99.99% .. 500%
MOIC_MIN, MOIC_MAX = D('0'), D('100')            # 0 .. 100×

# ── BASIS TAGS — the universal gross-vs-net net ──────────────────────────────
# Three gross-vs-net bugs shipped this layer (TVPI basis, §5.1 realised-gains,
# fund MOIC). Fixing each as a reddening control is necessary but not sufficient;
# the universal fix is that EVERY multiple and return figure carries its basis on
# the figure itself (Figure.value_basis), enforced by require_basis(), so neither
# the code nor a reader can conflate them at a call site. gross/net + denominator.
BASIS_GROSS_INVESTED = 'gross/invested'   # §2.1 Portfolio MOIC   (Σproceeds+ΣFV)/invested
BASIS_GROSS_COST = 'gross/cost'           # company MOIC          (realised+FV)/cost
BASIS_NET_CALLED = 'net/called'           # §2.2-2.4 TVPI, RVPI, DPI  (·+ResidualNAV)/called
BASIS_GROSS_XIRR = 'gross/xirr'           # company IRR (gross, pre-fee/carry)
BASIS_NET_XIRR = 'net/xirr'               # §2.6 fund Net IRR (net to LP)
BASIS_NET = 'net'                         # a net-of-carry cash amount (LP distributions); DPI/TVPI numerator
BASIS_GROSS = 'gross'                     # a gross cash amount (pre-carry: gross proceeds, gross distribution)
# fair-value perspective (§8.3): the two must never be conflated in a multiple
FV_HOLDING = 'fv/holding'                 # fair_value_of_holding — fund's stake (ILPA); MOIC/TVPI/…
FV_EQUITY = 'fv/equity'                   # fair_value — whole-company (§8.3 deal-level; NOT our contract)

# concepts that MUST carry a value_basis (a multiple/return figure without one is a bug)
REQUIRES_BASIS = frozenset({
    'moic', 'company_moic', 'portfolio_moic', 'tvpi', 'rvpi', 'dpi', 'irr', 'net_irr',
})


def require_basis(concept: str, value_basis) -> None:
    """Fail-closed guard: a multiple/return concept MUST declare its gross/net basis.
    Called by the aggregator when it wraps a derived figure — a missing basis is a
    programming error (raises), not a runtime data condition. This is what makes a
    fourth gross-vs-net conflation structurally impossible."""
    if concept in REQUIRES_BASIS and not value_basis:
        raise ValueError(f"multiple/return figure '{concept}' has no value_basis — "
                         f"every multiple must be tagged gross/net + denominator "
                         f"(formulas.BASIS_*) so it can never be conflated at a call site")


# ── NAMING-COLLISION GUARDS (documented pinned choices) ──────────────────────
# COLLISION 1 — "Residual NAV" means two different things in formula.html:
#   • §2.5  Residual NAV = ΣFV − carry (NET of carry, = 765.4)  → feeds TVPI/RVPI
#   • §3.1  Carry Base uses "Residual NAV" but means GROSS ΣFV (827) — using the
#           net figure here would recurse (carry depends on carry) AND break the
#           tie to the fixture's stated carry: 0.2×((81+765.4)−600) = 49.28 ≠ 61.6.
#   PINNED: carry_base() takes residual_FV (gross). The tie to the stated 61.6 is
#   the guard — swap in net and the reddening control fires (see test).
#
# COLLISION 2 — Portfolio MOIC fair-value basis (§2.1 vs §8.3 contradict):
#   • §2.1 line 383 expands with fair_value_of_holding (fund's stake, HOLDING basis)
#   • §8.3 line 1084 says Portfolio MOIC uses fair_value (whole-company EQUITY basis)
#   PINNED: HOLDING basis (FV_HOLDING) — a multiple's numerator must match its
#   denominator's basis, and our fund MOIC divides by total_invested (the fund's
#   cost), so the numerator must be the fund's stake. §8.3's equity basis is a
#   different deal-level metric, not our contract. Invisible on the fixture
#   (holding==equity at 100% ownership); real data with <100% ownership differs.
MOIC_FV_BASIS = FV_HOLDING


def _d(v) -> Optional[Decimal]:
    """Coerce to Decimal or None — never raises, never fabricates."""
    if v is None or v == '':
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def in_sanity_irr(r: Optional[Decimal]) -> bool:
    return r is not None and IRR_MIN <= r <= IRR_MAX


def in_sanity_moic(m: Optional[Decimal]) -> bool:
    return m is not None and MOIC_MIN <= m <= MOIC_MAX


# ── §2.6 / Net IRR — XIRR by bisection ───────────────────────────────────────
# IRR is inherently a fractional-exponent solve, so the NPV is evaluated in float
# and the rate returned as Decimal. This is the SINGLE xirr for the clean pipeline
# (company IRR verification AND fund Net IRR both call it). Matches preingest2's
# bisection spec: annual rate over [-0.99, 10.0], 100 iterations.
def xirr(cashflows: Sequence[Tuple[int, Decimal]]) -> Optional[Decimal]:
    """cashflows = [(date_ordinal, amount_cr), ...]; amount sign: calls negative,
    distributions/terminal positive. Returns Decimal annual rate, or None when
    there is no sign change, no convergence, or the result is outside the IRR
    sanity band (never a fabricated rate)."""
    flows = [(int(o), float(a)) for o, a in cashflows if o is not None and a is not None]
    if len(flows) < 2:
        return None
    t0 = min(o for o, _ in flows)

    def npv(rate: float) -> float:
        return sum(a / (1.0 + rate) ** ((o - t0) / 365.25) for o, a in flows)

    lo, hi = -0.99, 10.0
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:                       # no sign change → no bracketed root
        return None
    for _ in range(100):
        mid = (lo + hi) / 2.0
        fm = npv(mid)
        if abs(fm) < 1e-9:
            lo = hi = mid
            break
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    r = D(str((lo + hi) / 2.0))
    return r if in_sanity_irr(r) else None


# ── §3.2 accrued carry — feeds Residual NAV (§2.5) and Fund NAV (§5.1) ────────
# Carry base = max(0, (realised gross proceeds + residual FV on holding basis) − called).
# Accrued carry = carry_rate × base − carry already paid out. Floored at zero.
# NOTE: this is the "gains above called" base (no hurdle/catch-up) — the full
# European waterfall (§3 with hurdle/catch-up/clawback) is downstream-analytics
# scope, not pre-ingestion. The fixture's stated 61.6 corroborates THIS base
# exactly (0.2 × ((81+827)−600) = 61.6), which is the cross-check we run.
def carry_base(realised_gross: Optional[Decimal], residual_fv: Optional[Decimal],
               called: Optional[Decimal]) -> Optional[Decimal]:
    r, f, c = _d(realised_gross) or _Z, _d(residual_fv), _d(called)
    if f is None or c is None:
        return None
    return max(_Z, (r + f) - c)


def accrued_carry(carry_rate: Optional[Decimal], base: Optional[Decimal],
                  carry_paid: Optional[Decimal] = None) -> Optional[Decimal]:
    cr, b = _d(carry_rate), _d(base)
    if cr is None or b is None:
        return None
    gross = cr * b
    net = gross - (_d(carry_paid) or _Z)
    return max(_Z, net)


# ── §2.5 Residual NAV = Σ FV_holding − accrued carry (the LP-value that feeds
#    TVPI/RVPI; NEVER the gross-FV holding basis, NEVER the Fund NAV). ──────────
def residual_nav(residual_fv: Optional[Decimal],
                 accrued_carry_amt: Optional[Decimal]) -> Optional[Decimal]:
    f = _d(residual_fv)
    if f is None:
        return None
    return f - max(_Z, _d(accrued_carry_amt) or _Z)


# ── §2.1 Portfolio MOIC (GROSS) = (realised proceeds + Σ FV_holding) / invested ─
def portfolio_moic(realised_gross: Optional[Decimal], residual_fv: Optional[Decimal],
                   invested: Optional[Decimal]) -> Optional[Decimal]:
    r, f, inv = _d(realised_gross) or _Z, _d(residual_fv), _d(invested)
    if f is None or not inv:
        return None
    m = (r + f) / inv
    return m if in_sanity_moic(m) else None


# ── company MOIC (gross) = (realised + FV) / cost — per portfolio company ─────
def company_moic(realised_gross: Optional[Decimal], fair_value: Optional[Decimal],
                 cost: Optional[Decimal]) -> Optional[Decimal]:
    r, f, c = _d(realised_gross) or _Z, _d(fair_value), _d(cost)
    if f is None or not c:
        return None
    m = (r + f) / c
    return m if in_sanity_moic(m) else None


# ── §2.2 TVPI / Net MOIC = (distributions + Residual NAV) / called ────────────
def tvpi(distributions: Optional[Decimal], residual_nav_amt: Optional[Decimal],
         called: Optional[Decimal]) -> Optional[Decimal]:
    dist, rn, c = _d(distributions) or _Z, _d(residual_nav_amt), _d(called)
    if rn is None or not c:
        return None
    return (dist + rn) / c


# ── §2.3 DPI = distributions / called ────────────────────────────────────────
def dpi(distributions: Optional[Decimal], called: Optional[Decimal]) -> Optional[Decimal]:
    dist, c = _d(distributions), _d(called)
    if dist is None or not c:
        return None
    return dist / c


# ── §2.4 RVPI = Residual NAV / called ────────────────────────────────────────
def rvpi(residual_nav_amt: Optional[Decimal], called: Optional[Decimal]) -> Optional[Decimal]:
    rn, c = _d(residual_nav_amt), _d(called)
    if rn is None or not c:
        return None
    return rn / c


# ── §5.1 Fund NAV (P1 Computed) — the SIX components, exactly ─────────────────
# Fund NAV = Unrealised Value (FV) + Cash + Receivables
#            − Mgmt Fee Payable − Carry Payable − Other Liabilities
# NAV is the balance-sheet identity (Assets − Liabilities). Realised gains are
# NOT a term (2026-09-22 correction): exit proceeds already left the fund as
# distributions (captured in DPI) or sit in Cash, so a "+ Realised" term would
# double-count them — it overstated this fund's NAV by 45 (781.4 → 826.4).
# Dropping any of the SIX real terms is the bug the parity gate + component-count
# assertion exist to catch.
_NAV51_FIELDS = ('unrealised_value', 'cash', 'receivables',
                 'mgmt_fee_payable', 'carry_payable', 'other_liabilities')


def fund_nav_51(unrealised_value, cash, receivables,
                mgmt_fee_payable, carry_payable, other_liabilities) -> Optional[Decimal]:
    vals = [_d(unrealised_value), _d(cash), _d(receivables),
            _d(mgmt_fee_payable), _d(carry_payable), _d(other_liabilities)]
    if any(v is None for v in vals):
        return None
    uv, ca, rc, mfp, cp, ol = vals
    return uv + ca + rc - mfp - cp - ol


def fund_nav_51_components(unrealised_value, cash, receivables,
                           mgmt_fee_payable, carry_payable, other_liabilities):
    """The signed component list, for the actionable NAV disclosure + the
    six-component assertion. Order/signs match formula.html §5.1 verbatim
    (balance-sheet identity; realised gains are NOT a term — see fund_nav_51)."""
    return [
        ('+ Unrealised Value (FV of holding)', _d(unrealised_value)),
        ('+ Cash & Cash Equivalents', _d(cash)),
        ('+ Receivables', _d(receivables)),
        ('- Management Fee Payable', -(_d(mgmt_fee_payable) or _Z)),
        ('- Carry Payable', -(_d(carry_payable) or _Z)),
        ('- Other Liabilities', -(_d(other_liabilities) or _Z)),
    ]


# ── §6.3 Sanity Bounds — the ONLY gate formula.html specifies for Fund NAV ────
# "fund_nav ≤ 5× invested — out-of-range treated as extraction error." That is the
# whole gate. A NAV with all seven §5.1 inputs present and inside this bound is
# EMITTED (§1.3 blanks only on INSUFFICIENT inputs), never held on a roll-forward
# disagreement (the roll-forward is not a document formula). Earlier this module
# carried a 1% two-method HOLD — that was extra machinery beyond formula.html and
# has been removed; the roll-forward now lives as a Stage-4 logged cross-check.
NAV_SANITY_MULTIPLE = D('5')


def nav_sanity_ok(fund_nav: Optional[Decimal], invested: Optional[Decimal]) -> bool:
    """§6.3: Fund NAV passes iff ≤ 5× invested. Out-of-range → rejected with a
    logged reason (caller emits the reason), never silently clamped."""
    fn, inv = _d(fund_nav), _d(invested)
    if fn is None or inv is None or inv <= _Z:
        return False
    return fn <= NAV_SANITY_MULTIPLE * inv


# ── the capital-account roll-forward: the model-free identity the emitted §5.1 NAV
#    is CROSS-CHECKED against (Stage 4). Money in − out + earnings − carry. ───────
def rollforward_nav(called, distributions, pnl_itd, carry_payable) -> Optional[Decimal]:
    """LP NAV = contributions − distributions + net earnings − carry. Nothing modelled."""
    vals = [_d(called), _d(distributions), _d(pnl_itd), _d(carry_payable)]
    if any(v is None for v in vals):
        return None
    c, dist, pnl, carry = vals
    return c - dist + pnl - carry


@dataclass
class NavCrossCheck:
    """Stage-4 cross-check of the EMITTED §5.1 NAV against the roll-forward. This is a
    LOGGED DISCREPANCY (method tag + actionable reason), NOT a gate: `diverges` is
    advisory — it drives the log/hint, it never blanks a NAV the document would emit."""
    formula_nav: Optional[Decimal]
    rollforward_nav: Optional[Decimal]
    gap: Optional[Decimal]
    gap_pct: Optional[Decimal]
    diverges: bool                         # ADVISORY only (log severity / actionable hint)
    note: str = ''


# advisory band for the logged cross-check (surfaces a divergence for review; does
# NOT hold — formula.html has no two-method NAV hold).
NAV_CROSSCHECK_ADVISORY_PCT = D('1.0')


def nav_rollforward_crosscheck(formula_nav: Optional[Decimal], rollforward: Optional[Decimal],
                               *, advisory_pct: Decimal = NAV_CROSSCHECK_ADVISORY_PCT) -> NavCrossCheck:
    fn, rf = _d(formula_nav), _d(rollforward)
    if fn is None or rf is None:
        return NavCrossCheck(fn, rf, None, None, False,
                             'roll-forward cross-check unavailable (one method missing)')
    gap = fn - rf
    denom = abs(fn) if fn != _Z else (abs(rf) or D('1'))
    gap_pct = (abs(gap) / denom) * D('100')
    diverges = gap_pct > advisory_pct
    note = (f'§5.1 computed NAV {fn} vs capital-account roll-forward {rf}: Δ {gap} '
            f'({gap_pct.quantize(D("0.01"))}%). ' +
            ('material divergence — likely the as-of cash/working-capital not articulated '
             'from ITD flows, or realised-gains-vs-cash treatment; review before relying on NAV.'
             if diverges else 'within advisory band.'))
    return NavCrossCheck(fn, rf, gap, gap_pct, diverges, note)
