"""
S8 — Reconciliation (U7). Runs the check catalogue over the CIR and materialises
EVERY check into the output (pass or fail), turning the audit trail into part of
the product. Three classes:

  hard        — an identity that must hold. Failure blocks the run — BUT a hard
                check whose inputs are HELD is INDETERMINATE (also holds), never a
                false pass by treating a hole as 0, never a false fail on a hole.
  soft        — an expected correspondence between independently sourced figures.
                Never blocks; ALWAYS published with both figures and the variance
                (a 13%-inside-tolerance variance is the most useful fact, not
                something to hide).
  disclosure  — a known limitation (gap, stale period, estimated rate, held item,
                investment without an MIS). Always published.

Hard checks are ROUNDING-AWARE: native→scale→FX→₹Cr leaves sub-paisa residuals,
so equality is tested within a single rounding epsilon (build rule: rounding
defined once) — no phantom hard failures.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import List, Optional, Tuple

from .contract import TOLERANCES
from .cir import Figure

HARD, SOFT, DISCLOSURE = 'hard', 'soft', 'disclosure'
PASS, FAIL, INDETERMINATE = 'pass', 'fail', 'indeterminate'

_EPS = Decimal(str(TOLERANCES['hard_abs_cr']))        # rounding-aware epsilon, defined once
_SOFT_PCT = Decimal(str(TOLERANCES['soft_pct']))

# ── ANCHOR-GATED, scale-AWARE cross-sheet STOCK corroboration (Increment 3a) ──
# TWO real-data findings shape this (advisor 2026-07-27):
#  (1) the DECLARED unit LIES: Agnikul's Balance Sheet header reads "(All figures are in
#      ₹ Millions)" over ABSOLUTE-rupee values — a 10^6 lie the unit reader faithfully
#      repeats. Normalising from the declared unit gives 117,664,622 Cr for a 117.66 Cr
#      stock. So a naive absolute compare FALSELY diverges and would hold a pinned emit.
#  (2) but a power-of-ten ratio is AMBIGUOUS between "same value, unit lied by 10^k" and
#      "genuinely DIFFERENT values a clean 10^k apart" — a DIVISION's cash (11.7 Cr) vs
#      the CONSOLIDATED (117 Cr) is exactly 10×, mantissa identical. Treating any
#      power-of-ten as corroboration FALSE-CONFIRMS the second case — precisely the world
#      Σ-divisions (Increment 3b) operates in. Scale-BLIND up-to-scale is therefore wrong.
# RESOLUTION — the ANCHOR (whole-company cost/FV magnitude) breaks the ambiguity, because
# it is the only external scale-truth. Corroboration is anchor-gated and scale-AWARE:
#   • only anchor-PLAUSIBLE scale interpretations of an alt sheet are admissible (within
#     ±_ANCHOR_BAND_ORDERS decades of the anchor). This REPAIRS the lie: for Agnikul BS
#     only 'absolute' is plausible (117.66 Cr; 'millions' → 10^8 Cr is 5.4 orders off),
#     so the alt is read at 117.66 and corroborates the 117.66 emit scale-AWARE.
#   • a genuine 10^k gap is NOT masked: a division's only plausible cash stays genuinely
#     smaller → no scale-aware match → the emit HOLDS (SAFE direction; false-confirm is
#     the danger, false-hold merely costs coverage until 3b makes the check scope-aware).
#   • NO anchor → a power-of-ten gap is genuinely undecidable → an AMBIGUOUS disclosure
#     ('mantissa-corroborated, scale-unresolved'), NEVER a clean corroboration and NOT
#     counted as cross-checked (advisor: unanchored power-of-ten ≠ confirmation).
_ANCHOR_BAND_ORDERS = Decimal('1.5')     # mirrors units._ANCHOR_ORDERS — the plausible-decade gate
_STOCK_SCALEAWARE_TOL = Decimal('0.005')
# same stock, same date, at its TRUE scale → agrees to rounding. Tighter than the
# up-to-scale mantissa band below (advisor: 2% was quietly generous for a genuine match).
# Agnikul Analysis 117.6646 vs BS-absolute 117.6646… agree to ~1e-7. On 30-file revisit list.
_STOCK_MANTISSA_TOL = Decimal('0.02')    # significant-digit band — used ONLY to LABEL the
# no-anchor AMBIGUOUS case (same digits, unresolved decade), never as a corroboration.


def _result(cid, cls, status, **extra):
    r = {'id': cid, 'class': cls, 'status': status}
    r.update(extra)
    return r


def hard_equal(cid: str, lhs: Optional[Figure], rhs, *, lhs_label='', rhs_label='') -> dict:
    """Hard identity lhs ≈ rhs within the rounding epsilon. If any operand is a
    held/gap figure → INDETERMINATE (held), never pass/fail on a hole."""
    def val(x):
        if isinstance(x, Figure):
            return None if not x.confirmed else x.value_cr
        return None if x is None else Decimal(str(x))
    lv, rv = val(lhs), val(rhs)
    if lv is None or rv is None:
        return _result(cid, HARD, INDETERMINATE, lhs=lhs_label, rhs=rhs_label,
                       detail='an input is held/absent — cannot assert; run holds')
    var = lv - rv
    ok = abs(var) <= _EPS
    return _result(cid, HARD, PASS if ok else FAIL, lhs=str(lv), rhs=str(rv),
                   variance=str(var), tolerance=str(_EPS))


def hard_sum_equal(cid: str, parts: List[Figure], total, **kw) -> dict:
    """Σparts ≈ total, hole-aware: any held part → indeterminate (never sum a
    hole as 0)."""
    from .aggregate import sum_as_reported
    agg = sum_as_reported(parts)
    if not agg.complete or agg.value is None:
        return _result(cid, HARD, INDETERMINATE,
                       detail=f'{len(agg.held)} held part(s) — sum indeterminate, run holds',
                       held=agg.held)
    return hard_equal(cid, _lit(agg.value), total, **kw)


def _lit(v) -> Figure:
    """Wrap a computed Decimal as a confirmed Figure so it flows through the
    same rounding-aware comparator."""
    return Figure('sum', v, None, None)


def soft_correspondence(cid: str, a: Optional[Figure], b, *, a_label='', b_label='') -> dict:
    """Soft check — never blocks, ALWAYS published with the variance and its
    percentage, even (especially) when inside tolerance."""
    def val(x):
        if isinstance(x, Figure):
            return None if not x.confirmed else x.value_cr
        return None if x is None else Decimal(str(x))
    av, bv = val(a), val(b)
    if av is None or bv is None:
        return _result(cid, SOFT, INDETERMINATE, a=a_label, b=b_label,
                       detail='an input is held/absent — correspondence not computed')
    var = av - bv
    pct = (var / bv * 100) if bv != 0 else None
    within = (abs(var) <= abs(bv) * _SOFT_PCT) if bv != 0 else (var == 0)
    return _result(cid, SOFT, PASS if within else 'variance', a=str(av), b=str(bv),
                   variance=str(var), variance_pct=(str(pct.quantize(Decimal('0.1'))) if pct is not None else None),
                   within_tolerance=within,
                   detail='published regardless of tolerance (soft check)')


def disclosure(cid: str, detail: str, **extra) -> dict:
    return _result(cid, DISCLOSURE, 'disclosed', detail=detail, **extra)


def blocks_run(results: List[dict]) -> bool:
    """The run holds on any hard FAIL or any hard INDETERMINATE (a hole in a hard
    identity is not a pass)."""
    return any(r['class'] == HARD and r['status'] in (FAIL, INDETERMINATE) for r in results)


def scale_gap(v1, v2) -> Optional[int]:
    """k such that v1 ≈ v2·10^k — the DECLARED-scale disagreement between two
    independently-sourced values of the same stock. None if either is 0 or the signs
    differ (a sign flip is a real divergence, never a scale artifact)."""
    if v1 is None or v2 is None:
        return None
    a, b = Decimal(str(v1)), Decimal(str(v2))
    if a == 0 or b == 0 or (a < 0) != (b < 0):
        return None
    return int(round(math.log10(abs(a) / abs(b))))


def agree_up_to_scale(v1, v2, tol: Decimal = _STOCK_MANTISSA_TOL) -> bool:
    """True iff v1 and v2 are the SAME significant digits at a possibly-different
    (possibly-lying) declared scale: their ratio is within `tol` of an integer power of
    ten. Used ONLY to LABEL the no-anchor ambiguous case — NEVER as a corroboration,
    because a clean power-of-ten is equally a division-vs-consolidated real difference."""
    if v1 is None or v2 is None:
        return False
    a, b = Decimal(str(v1)), Decimal(str(v2))
    if a == 0 or b == 0:
        return a == b
    if (a < 0) != (b < 0):                      # sign flip → genuine divergence
        return False
    k = scale_gap(a, b)
    ref = abs(b) * (Decimal(10) ** k)           # b·10^k ≈ a if same significant digits
    if ref == 0:
        return abs(a) == 0
    return abs(abs(a) - ref) / ref <= tol


def scale_aware_agree(v1, v2, tol: Decimal = _STOCK_SCALEAWARE_TOL) -> bool:
    """True iff v1 and v2 are the same value at the SAME scale (absolute ₹Cr), to
    rounding. This is the real corroboration test — a genuine 10× gap FAILS it."""
    if v1 is None or v2 is None:
        return False
    a, b = Decimal(str(v1)), Decimal(str(v2))
    if (a < 0) != (b < 0):
        return False
    ref = max(abs(a), abs(b))
    if ref == 0:
        return a == b
    return abs(a - b) / ref <= tol


def agree_within_orders(v1, v2, orders: Decimal = Decimal('0.1')) -> bool:
    """True iff v1 and v2 are within `orders` decades of each other (|log10(|v1|/|v2|)| ≤ orders) — a
    LOOSE 'same figure allowing rounding / minor definitional differences' band (default 0.1 orders
    ≈ 26%), DISTINCT from the tight scale_aware_agree used for value corroboration. This is the ONE
    home for the orders-band agreement test (previously hand-rolled inline in extract._sheet_corroborated,
    a drift risk). Caller guarantees both values are truthy (non-zero, non-None); magnitudes are compared
    on |·| so a sign difference is NOT treated as a divergence here (the corroboration use-case is
    magnitude agreement across sheets, not sign)."""
    return abs(math.log10(abs(float(v1)) / abs(float(v2)))) <= float(orders)


def _orders_from(c: Decimal, anchor: Decimal) -> float:
    if c <= 0 or anchor <= 0:
        return 99.0
    return abs(math.log10(float(c) / float(anchor)))


def anchor_plausible_crs(scale_crs: dict, anchor_cr,
                         band: Decimal = _ANCHOR_BAND_ORDERS) -> dict:
    """The subset of {scale: cr} interpretations of a sheet whose magnitude sits within
    `band` decades of the whole-company anchor — the admissible readings after the anchor
    repairs a lying unit. Empty if the anchor rules them all out (unanchorable sheet)."""
    if anchor_cr is None:
        return dict(scale_crs)
    a = Decimal(str(anchor_cr))
    return {sc: c for sc, c in scale_crs.items()
            if c is not None and _orders_from(Decimal(str(c)), a) <= float(band)}


def stock_corroboration(cid: str, emitted: Optional[Figure],
                        alts: List[Tuple[str, str, dict]], *, anchor_cr=None,
                        tol: Decimal = _STOCK_SCALEAWARE_TOL,
                        mantissa_tol: Decimal = _STOCK_MANTISSA_TOL) -> dict:
    """Cross-sheet corroboration of an emitted STOCK against the SAME concept
    independently sourced on OTHER sheets. ANCHOR-GATED and scale-AWARE (see module note).

    Each alt is (label, declared_scale, {scale: cr}) — the caller precomputes the alt's
    ₹Cr under every candidate scale (units.SCALE_TO_ABS × FX); this fn stays pure.

      emitted held/gap  → INDETERMINATE (nothing to corroborate; already held)
      no alts           → 'single_source' DISCLOSURE (un-cross-checked — advisor catch A)
      per alt:
        anchor present  → corroborates iff SOME anchor-plausible interpretation matches
                          the emit scale-AWARE; else if a plausible interpretation exists
                          but none matches → genuine divergence (HOLD); if the anchor
                          rules every scale out → ambiguous (skip, disclosed).
        no anchor       → corroborates iff the DECLARED-scale reading matches scale-aware;
                          else a power-of-ten (up-to-scale) match → AMBIGUOUS disclosure
                          (scale-unresolved, NOT a cross-check); else divergence (HOLD).
      any divergence → HARD FAIL (emit holds); else ≥1 corroboration → SOFT PASS
      (cross-checked); else only ambiguous/unanchorable → DISCLOSURE (not cross-checked).
    """
    if not isinstance(emitted, Figure) or not emitted.confirmed:
        return _result(cid, SOFT, INDETERMINATE,
                       detail='emit held/absent — nothing to corroborate')
    ev = Decimal(str(emitted.value_cr))
    if not alts:
        return _result(cid, DISCLOSURE, 'single_source', cross_checked=False,
                       detail='single-source stock — un-cross-checked (no independent '
                              'sheet carries this concept; rests on existence+label+scale)')
    corroborated, diverged, ambiguous = [], [], []
    for lbl, decl_scale, scale_crs in alts:
        if anchor_cr is not None:
            plausible = anchor_plausible_crs(scale_crs, anchor_cr, _ANCHOR_BAND_ORDERS)
            if not plausible:
                ambiguous.append({'sheet': lbl, 'why': 'no anchor-plausible scale — unanchorable'})
            elif len(plausible) >= 2:
                # ADVISOR POINT 2: the anchor is too COARSE to pin the alt's scale (e.g. lakhs &
                # millions BOTH within the band for a mid-range figure). Declining a confident verdict
                # here is the over-reach guard symmetric to the false-confirm we closed: corroborate
                # ONLY if every plausible reading agrees (degenerate), else it is AMBIGUOUS — never a
                # confident cross-check and never a divergence strong enough to HOLD the emit.
                if all(scale_aware_agree(ev, c, tol) for c in plausible.values()):
                    corroborated.append({'sheet': lbl, 'scales': sorted(plausible)})
                else:
                    ambiguous.append({'sheet': lbl, 'why': 'anchor cannot disambiguate scale',
                                      'plausible_cr': [str(c) for c in plausible.values()]})
            elif any(scale_aware_agree(ev, c, tol) for c in plausible.values()):
                corroborated.append({'sheet': lbl, 'scales': sorted(plausible)})
            else:
                diverged.append({'sheet': lbl,
                                 'plausible_cr': [str(c) for c in plausible.values()]})
        else:
            dc = scale_crs.get(decl_scale)
            if scale_aware_agree(ev, dc, tol):
                corroborated.append({'sheet': lbl, 'scales': [decl_scale]})
            elif agree_up_to_scale(ev, dc, mantissa_tol):
                ambiguous.append({'sheet': lbl, 'k': scale_gap(ev, dc),
                                  'why': 'power-of-ten gap, no anchor to resolve scale'})
            else:
                diverged.append({'sheet': lbl, 'declared_cr': str(dc)})
    if diverged:
        return _result(cid, HARD, FAIL, cross_checked=True, emitted=str(ev),
                       detail=f'stock diverges from {len(diverged)} independent source(s) '
                              f'at an anchor-plausible scale — emit holds',
                       divergences=diverged, ambiguous=ambiguous)
    if corroborated:
        return _result(cid, SOFT, PASS, cross_checked=True, emitted=str(ev),
                       n_sources=len(corroborated) + 1, corroborated=corroborated,
                       ambiguous=ambiguous,
                       detail=f'corroborated scale-aware on {len(corroborated)} independent '
                              f'source(s)' + (f'; {len(ambiguous)} scale-unresolved' if ambiguous else ''))
    return _result(cid, DISCLOSURE, 'scale_unresolved', cross_checked=False,
                   emitted=str(ev), ambiguous=ambiguous,
                   detail='only mantissa-corroborated (scale-unresolved) or unanchorable '
                          'sources — NOT counted as cross-checked')


# ── Σ-DIVISIONS roll-up identity — the SHARED consolidated-selection VERIFIER (Increment 3b) ──
# A multi-division file presents N same-shape sibling tabs (one per division) PLUS the
# consolidated roll-up, indistinguishable by shape/coverage/GL-fraction/family-verdict. The
# consolidated is the UNIQUE tab C whose additive top-line R(C) ≈ Σ(R of every OTHER sibling):
# a division fails because its 'others' still include the big roll-up (Σ inflated out of band);
# the consolidated matches because its 'others' are exactly the parts that sum to it. Pure
# arithmetic over supplied values — no sheet NAMES, no self-declared "consolidated" label (a
# division tab can be named 'Consolidated'; the roll-up can be named 'PL'); naming and whole≥part
# only CORROBORATE, they never admit a candidate.
#
# This is the ONE verifier both consolidated-selection entry points SHARE (the permanent
# deterministic tier — NOT scaffolding). TODAY the deterministic tier (extract._best_sheet)
# proposes a pick and this confirms it. When the S4 model-locator turns on (Step 6) it runs ONLY
# on this tier's HOLDs (abstentions), and its proposal passes THIS SAME check before acceptance —
# the model augments the residual, it never overturns a confirmed deterministic pick.
_SIGMA_ELIM_BAND = Decimal('0.15')   # UNCALIBRATED, conservative: a consolidated may sit up to
# 15% below Σ(siblings) from inter-division eliminations (consolidated = Σparts − inter-co sales).
_SIGMA_ROUND_TOL = Decimal('0.01')   # a consolidated cannot EXCEED Σ(siblings) beyond rounding.


def _sigma_satisfies(rx: Decimal, sigma_others: Decimal,
                     elim_band: Decimal, round_tol: Decimal) -> bool:
    if sigma_others <= 0:
        return rx == 0
    return sigma_others * (1 - elim_band) <= rx <= sigma_others * (1 + round_tol)


def collapse_value_duplicates(vectors: dict) -> dict:
    """GUARDRAIL 1 (non-trivial alias gate). Collapse tabs whose additive-flow vector is
    IDENTICAL on the SAME populated columns AND non-zero on ≥1 column — a sub-group tab that
    merely REPEATS a division's numbers (CPM: Healthcare≡HC, AnaCOM≡AN) would otherwise
    double-count Σ(others) and make the roll-up test abstain. VALUE-based only (never names).
    All-zero tabs are NEVER collapsed (an all-zero vector carries no evidence of identity, and
    is inert in the sum anyway). The kept representative of each group is the sorted-min label →
    deterministic, independent of the order sheets are presented in.

    vectors: {label: {col_order: Decimal}}. Returns {representative_label: vector}."""
    reps, seen = {}, {}
    for lab in sorted(vectors):
        vec = vectors[lab]
        pop = {k: v for k, v in vec.items() if v is not None}
        if any(v != 0 for v in pop.values()):
            key = tuple(sorted((k, str(v)) for k, v in pop.items()))
            if key in seen:                    # exact duplicate of an already-kept sorted-min rep
                continue
            seen[key] = lab
        reps[lab] = vec
    return reps


def sigma_consolidated_pick(values: dict, *, elim_band: Decimal = _SIGMA_ELIM_BAND,
                            round_tol: Decimal = _SIGMA_ROUND_TOL) -> dict:
    """The Σ-divisions uniqueness test over {label: representative_flow}. The UNIQUE satisfier of
    R(X) ≈ Σ(others) is the consolidated → that pick; 0 satisfiers (no roll-up present — a file
    of divisions only) or ≥2 (balanced split, or a residual multi-child sub-group double-count)
    → pick=None (ABSTAIN: the deterministic tie-break decides now, the model at Step 6 later).
    Values must be PRE-DEDUPED by the caller (collapse_value_duplicates); this stays pure."""
    usable = {k: Decimal(str(v)) for k, v in values.items() if v is not None}
    if len(usable) < 3:                        # need a roll-up + ≥2 parts to be a Σ at all
        return {'pick': None, 'satisfiers': [], 'reason': 'too few comparable siblings'}
    total = sum(usable.values(), Decimal(0))
    if total <= 0:                             # an all-zero/degenerate representative period is NO
        return {'pick': None, 'satisfiers': [],   # signal — every X trivially "satisfies" 0≈Σ(0); abstain
                'reason': 'representative period all-zero/degenerate — no roll-up signal'}
    sat = sorted(k for k, rx in usable.items()
                 if _sigma_satisfies(rx, total - rx, elim_band, round_tol))
    if len(sat) == 1:
        pick = sat[0]
        return {'pick': pick, 'satisfiers': sat, 'reason': 'unique Σ-divisions satisfier',
                'ge_all': all(usable[pick] >= v for v in usable.values())}   # corroborator only
    return {'pick': None, 'satisfiers': sat,
            'reason': ('no roll-up present (0 satisfiers)' if not sat
                       else f'{len(sat)} satisfiers — ambiguous, hold')}


# ── UNIFIED cross-sheet N-location reconciler (model-phase finder prerequisite, step B) ──────────
# The one primitive the whole-file finder needs: a concept X located at N places across sheets →
# ONE deterministic disposition. Today three FRAGMENTED, concept-specific pieces do slices of this
# (stock_corroboration = same-concept agree/diverge for money STOCKS; sigma_consolidated_pick =
# multi-scope roll-up selection; extract._sheet_corroborated = income agree). This generalises them.
# BUILT STANDALONE + GATED FIRST (step B); routing the three existing call-sites through it is the
# COMMITTED step A (its own byte-identical gate) — so the two paths coexist only transiently, never
# as permanent duplication. Pure: the caller normalises every value_cr to ₹Cr (one frame) and tags
# each location's SCOPE; this fn makes no I/O and reads no sheet names for admission.
def _loc_tag(l: dict) -> dict:
    return {'sheet': l.get('sheet'), 'cell': l.get('cell'), 'scope': l.get('scope'),
            'value_cr': str(l['value_cr'])}


def _all_pairwise_agree(vals: List[Decimal], tol: Decimal) -> bool:
    # STRICT (fail-closed): 'agree' requires EVERY pair to agree scale-aware — any one disagreement
    # drops out of the agree-emit path into hold/multiscope. Never emit over a disagreement.
    for i in range(len(vals)):
        for j in range(i + 1, len(vals)):
            if not scale_aware_agree(vals[i], vals[j], tol):
                return False
    return True


def reconcile_locations(cid: str, concept: str, locations: List[dict], *, anchor_cr=None,
                        tol: Decimal = _STOCK_SCALEAWARE_TOL) -> dict:
    """Reconcile the SAME concept read at N locations across sheets into ONE disposition. Each
    location is a dict {'sheet','cell','value_cr','scope','kind'} with value_cr ALREADY normalised
    to ₹Cr by the caller (pure fn). Dispositions (result['disposition']):

      • 'none'       — no location carries a value (INDETERMINATE; value_cr=None).
      • 'single'     — exactly one valued location → passes through, cross_checked=False
                       (single-source DISCLOSURE — un-cross-checked; advisor catch A).
      • 'agree'      — all valued locations agree scale-aware → emit the agreed value,
                       cross_checked=True (SOFT PASS, corroborated on N independent sources).
      • 'conflict'   — valued locations that SHOULD agree (same/one scope) disagree → HOLD
                       (value_cr=None; never ship an unreconciled number — HARD FAIL).
      • 'multiscope' — valued locations differ AND span ≥2 distinct scopes → the UNIQUE Σ-identity
                       satisfier (R(X) ≈ Σ others = the consolidated) is emitted; 0 or ≥2 satisfiers
                       → HOLD (value_cr=None). Naming never admits a scope — only the Σ-identity does.

    Determinism: locations are processed in (sheet, cell) sort order; the emitted value is the
    sort-first source's ₹Cr (all agree scale-aware, so any is representative)."""
    locs = sorted((l for l in locations if l.get('value_cr') is not None),
                  key=lambda l: (str(l.get('sheet') or ''), str(l.get('cell') or '')))
    if not locs:
        return _result(cid, DISCLOSURE, INDETERMINATE, disposition='none', value_cr=None,
                       cross_checked=False, sources=[], detail=f'{concept}: no located value')
    if len(locs) == 1:
        v = Decimal(str(locs[0]['value_cr']))
        return _result(cid, DISCLOSURE, 'single_source', disposition='single', value_cr=v,
                       cross_checked=False, sources=[_loc_tag(locs[0])],
                       detail=f'{concept}: single-source — un-cross-checked (rests on existence+label+scale)')
    vals = [Decimal(str(l['value_cr'])) for l in locs]
    if _all_pairwise_agree(vals, tol):
        return _result(cid, SOFT, PASS, disposition='agree', value_cr=vals[0], cross_checked=True,
                       n_sources=len(locs), sources=[_loc_tag(l) for l in locs],
                       detail=f'{concept}: corroborated scale-aware on {len(locs)} independent sources')
    distinct_scopes = {l.get('scope') for l in locs if l.get('scope') is not None}
    if len(distinct_scopes) < 2:                      # same (or unlabelled) scope but values differ
        return _result(cid, HARD, FAIL, disposition='conflict', value_cr=None, cross_checked=True,
                       sources=[_loc_tag(l) for l in locs], values=[str(v) for v in vals],
                       detail=f'{concept}: {len(locs)} same-scope sources disagree — unreconciled, held')
    # ≥2 distinct scopes → the Σ-consolidated identity is the ONLY admissible declaration (labels never admit)
    by_label = {f"{l.get('scope')}:{l.get('sheet') or ''}:{l.get('cell') or ''}": Decimal(str(l['value_cr']))
                for l in locs}
    pk = sigma_consolidated_pick(by_label)
    if pk['pick'] is not None:
        return _result(cid, SOFT, PASS, disposition='multiscope', value_cr=by_label[pk['pick']],
                       cross_checked=True, n_sources=len(locs), pick=pk['pick'],
                       sources=[_loc_tag(l) for l in locs],
                       detail=f'{concept}: multi-scope resolved to the Σ-identity consolidated')
    return _result(cid, HARD, FAIL, disposition='multiscope', value_cr=None, cross_checked=True,
                   sources=[_loc_tag(l) for l in locs], reason=pk['reason'],
                   detail=f'{concept}: multi-scope, no unique Σ-consolidated satisfier — held ({pk["reason"]})')


def summarize(results: List[dict]) -> dict:
    return {
        'hard_pass': sum(1 for r in results if r['class'] == HARD and r['status'] == PASS),
        'hard_fail': sum(1 for r in results if r['class'] == HARD and r['status'] == FAIL),
        'hard_indeterminate': sum(1 for r in results if r['class'] == HARD and r['status'] == INDETERMINATE),
        'soft_variance': sum(1 for r in results if r['class'] == SOFT and r['status'] == 'variance'),
        'disclosures': sum(1 for r in results if r['class'] == DISCLOSURE),
        'blocked': blocks_run(results),
    }
