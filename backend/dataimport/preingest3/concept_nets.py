"""Concept-Identity data-nets — Phase 1.5, model-OFF (concept_identity_build_spec v1.1 §3).

LAYER A §3 — the three deterministic nets that gate a MERGE (never an emit; §2/R3 — a single-source
figure emits on its own verified provenance with identity disclosed `asserted`; only a MERGE of two
sources needs a net to promote it to `confirmed`).

The centralization discipline (§0.1): DETECTION is universal — one mechanism per net, for every base.
RESOLUTION is a DECLARED policy per (base, event) read from contract.CI_RESOLUTION_POLICY — folding the
proto-nets down to one "disagree→hold" rule would silently flatten fund_anchor's MAX and fund_terms'
corroborate (a regression). The split keeps each proto-net's behaviour exact while unifying detection.

Net 2 reads a base's declared `identity_class` (§1.1) and applies the matching conservation LAW from a
law registry. A NEW identity-bearing base gets Net-2 coverage by declaring its class in the taxonomy
DATA — nobody edits Net 2 (the Net-2 twin of control CI-12). A base whose class is `none` (free flow)
has Net 2 DECLARED-inactive, never silently absent.

No model, no I/O, pure over Decimals + the taxonomy. On doubt the nets HOLD (fail-closed); they never
fabricate or pick a number — resolution is a labelled disposition the caller acts on, not a value.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple

from .concept_identity import ConceptKey, identity_class_of, ladder_rank, resolution_policy_of

# ── net-event ids (also the resolution-policy event keys in CI_RESOLUTION_POLICY) ──
NET1 = 'net1_collision'
NET2 = 'net2_movement'
NET3 = 'net3_context'
CORROBORATE = 'net1_corroborate'

# ── outcomes ──
AGREE = 'agree'                # Net 1: overlapping sources agree within tolerance
COLLIDE = 'collide'            # Net 1: overlapping sources disagree → fired
RECONCILED = 'reconciled'      # Net 2: every movement satisfies the base's conservation law
UNRECONCILED = 'unreconciled'  # Net 2: a movement has no explaining flows / breaks the ladder → fired
HOMOGENEOUS = 'homogeneous'    # Net 3: sources share one layout class (positive promotion signal)
HETEROGENEOUS = 'heterogeneous'  # Net 3: sources span >1 known layout class → doubt fired
INACTIVE = 'inactive'         # Net 2: identity_class 'none' — DECLARED-inactive, not silently absent
UNKNOWN = 'unknown'           # Net 3: layout class unknown → no signal either way (no fire)
CLEAN = 'clean'               # nothing to check (fewer than two overlapping observations)

_FIRED = (COLLIDE, UNRECONCILED, HETEROGENEOUS)

# Default detection tolerances (contract.TOLERANCES hard_abs_cr). Increment 3 subsumption passes each
# proto-net's own band (e.g. fund_terms' _RATE_TOL=5e-4 for rates) so behaviour is preserved exactly.
_DEF_TOL_ABS = Decimal('0.01')
_DEF_TOL_FRAC = Decimal('0')

# lifecycle ordinal — orders opening→…→closing within one period so flow_balance (opening + flows =
# closing) reads in the right direction when the period label ties.
_LIFECYCLE_ORDER = {'opening': 0, 'period': 1, 'as_of': 2, 'closing': 3, 'cumulative': 4, 'terminal': 5}


@dataclass(frozen=True)
class Observation:
    """One asserted (value @ period) for a ConceptKey, from one source, with its layout context (Net 3).
    `flows` = signed movement components feeding THIS step (Net 2 roll_forward / flow_balance): a tuple of
    (label, amount), positive = increases the base, negative = decreases it. Empty when the law needs none
    (e.g. a bare NAV concatenation — which is exactly the unreconciled case Net 2 must catch)."""
    key: ConceptKey
    period: Optional[str] = None
    value: Optional[Decimal] = None
    source: str = ''
    layout_fp: Optional[str] = None
    flows: Tuple[Tuple[str, Decimal], ...] = ()


@dataclass(frozen=True)
class NetVerdict:
    net: str
    outcome: str
    base: Optional[str] = None
    resolution: Optional[str] = None       # declared policy for a FIRED/agreeing net; None when N/A
    overlap_periods: Tuple = ()
    detail: str = ''
    evidence: Tuple = ()

    def fired(self) -> bool:
        return self.outcome in _FIRED


def _dec(v) -> Optional[Decimal]:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _agree(a: Decimal, b: Decimal, tol_abs: Decimal, tol_frac: Decimal) -> bool:
    d = abs(a - b)
    if d <= tol_abs:
        return True
    m = max(abs(a), abs(b))
    return m > 0 and (d / m) <= tol_frac


def _period_key(p):
    """Sortable key that tolerates a None period mixed with str periods."""
    return (p is None, p)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# NET 1 — Collision. Two+ sources asserted under the SAME ConceptKey overlapping on the SAME period
# must agree within tolerance. Disagree → COLLIDE, disposition = the declared per-(base) policy
# (default hold_both; cost/fair_value → max). Agree → the corroborate policy. Detection universal.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def net1_collision(observations, *, tol_abs: Decimal = _DEF_TOL_ABS,
                   tol_frac: Decimal = _DEF_TOL_FRAC) -> NetVerdict:
    # A collision is per-CONCEPT: two sources under the SAME ConceptKey at the same period. Grouping by
    # (key, period) — not period alone — so distinct keys that legitimately co-occur (e.g. opening vs
    # closing cash in one period) are never compared against each other.
    obs = [o for o in observations if _dec(o.value) is not None]
    rep_base = obs[0].key.base if obs else None
    by_kp: Dict = defaultdict(list)
    key_base: Dict = {}
    for o in obs:
        kc = o.key.canonical()
        by_kp[(kc, o.period)].append(_dec(o.value))
        key_base[kc] = o.key.base
    overlaps = {kp: vs for kp, vs in by_kp.items() if len(vs) >= 2}
    if not overlaps:
        return NetVerdict(NET1, CLEAN, rep_base, None, ())
    overlap_periods = tuple(sorted({p for (_kc, p) in overlaps}, key=_period_key))
    colliding = [(kc, p) for (kc, p), vs in overlaps.items()
                 if not _agree(max(vs), min(vs), tol_abs, tol_frac)]
    if colliding:
        cbase = key_base[colliding[0][0]]
        pol = resolution_policy_of(cbase, NET1) or 'hold_both'
        return NetVerdict(NET1, COLLIDE, cbase, pol, overlap_periods,
                          detail=f"{cbase}: sources disagree", evidence=tuple(p for _kc, p in colliding))
    pol = resolution_policy_of(rep_base, CORROBORATE) or 'corroborate'
    return NetVerdict(NET1, AGREE, rep_base, pol, overlap_periods,
                      detail=f"agrees across {len(overlap_periods)} overlapping period(s)")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# NET 2 — Unreconciled movement. Reads the base's declared identity_class and applies its conservation
# LAW from the registry. Detection universal (a new base = a taxonomy edit, not code); resolution is
# the declared per-(base) policy (default hold). identity_class 'none' → declared-inactive.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _law_movement(obs, tol_abs, tol_frac):
    """roll_forward / flow_balance: each step's Δvalue must equal the sum of the flows into that step.
    Steps ordered by (period, lifecycle) so a bare concatenation with no flows (Δ≠0, flows=0) fails."""
    def _ord(o):
        lc = o.key.q().get('lifecycle')
        return (o.period is None, o.period, _LIFECYCLE_ORDER.get(lc, 1))
    series = sorted((o for o in obs if _dec(o.value) is not None), key=_ord)
    unexplained = []
    for i in range(1, len(series)):
        move = _dec(series[i].value) - _dec(series[i - 1].value)
        explained = sum((_dec(f) or Decimal('0') for _lbl, f in series[i].flows), Decimal('0'))
        if not _agree(move, explained, tol_abs, tol_frac):
            unexplained.append((series[i].period, str(move), str(explained)))
    if unexplained:
        return UNRECONCILED, f"{len(unexplained)} movement(s) unexplained by flows", tuple(unexplained)
    return RECONCILED, "every movement explained by flows", ()


def _law_ladder(obs, tol_abs, tol_frac):
    """monotone_ladder (capital): within a period the rungs must respect committed ≥ called ≥ drawn ≥
    paid_in ≥ distributed. A lower rung exceeding a higher rung (e.g. distributed > committed) is an
    unreconciled wrong-merge. Rungs off the ordered ladder (e.g. 'undrawn', a complement) are skipped —
    they carry no monotone position, a declared boundary, not a silent pass."""
    by_period = defaultdict(list)
    for o in obs:
        v = _dec(o.value)
        rung = o.key.q().get('capital_status')
        r = ladder_rank(rung) if rung else None
        if v is None or r is None:
            continue
        by_period[o.period].append((r, rung, v))
    violations = []
    for p, items in by_period.items():
        items.sort(key=lambda t: t[0])          # ascending rank = committed first (largest expected)
        for i in range(1, len(items)):
            _, rung_hi, v_hi = items[i - 1]
            _, rung_lo, v_lo = items[i]
            if v_lo - v_hi > tol_abs and not _agree(v_lo, v_hi, tol_abs, tol_frac):
                violations.append((p, rung_hi, str(v_hi), rung_lo, str(v_lo)))
    if violations:
        return UNRECONCILED, f"{len(violations)} ladder-order violation(s)", tuple(violations)
    return RECONCILED, "capital rungs respect the monotone ladder", ()


_IDENTITY_LAWS = {
    'roll_forward': _law_movement,
    'flow_balance': _law_movement,
    'monotone_ladder': _law_ladder,
}


def net2_movement(observations, *, tol_abs: Decimal = _DEF_TOL_ABS,
                  tol_frac: Decimal = _DEF_TOL_FRAC) -> NetVerdict:
    obs = list(observations)
    base = obs[0].key.base if obs else None
    idclass = identity_class_of(base)
    if idclass == 'none':
        return NetVerdict(NET2, INACTIVE, base, None,
                          detail=f"{base}: identity_class none — Net 2 declared-inactive")
    law = _IDENTITY_LAWS.get(idclass)
    if law is None:                               # a declared class with no registered law → hold, don't guess
        return NetVerdict(NET2, UNRECONCILED, base, resolution_policy_of(base, NET2) or 'hold',
                          detail=f"{base}: identity_class '{idclass}' has no registered law")
    outcome, detail, ev = law(obs, tol_abs, tol_frac)
    res = (resolution_policy_of(base, NET2) or 'hold') if outcome == UNRECONCILED else None
    return NetVerdict(NET2, outcome, base, res, detail=f"{base} [{idclass}]: {detail}", evidence=ev)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# NET 3 — Heterogeneous source context. Sources feeding one canonical concept whose layout fingerprints
# (U3) span more than one class raise a doubt flag before promotion. HOMOGENEOUS is the positive signal
# promotion can rest on; unknown fingerprints yield no signal (never a false homogeneity).
# ══════════════════════════════════════════════════════════════════════════════════════════════
def net3_context(observations) -> NetVerdict:
    obs = list(observations)
    base = obs[0].key.base if obs else None
    fps = tuple(sorted({o.layout_fp for o in obs if o.layout_fp is not None}))
    any_unknown = any(o.layout_fp is None for o in obs)
    if len(fps) >= 2:
        return NetVerdict(NET3, HETEROGENEOUS, base, None,
                          detail=f"{len(fps)} distinct layout classes", evidence=fps)
    if any_unknown or not fps:
        return NetVerdict(NET3, UNKNOWN, base, None, detail="layout class unknown", evidence=fps)
    return NetVerdict(NET3, HOMOGENEOUS, base, None, detail="single layout class", evidence=fps)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# PROMOTION (§3): asserted → confirmed requires CONVERGENCE of independent signals — value agreement
# across every overlapping period PLUS either identity-law closure (Net 2) or context homogeneity
# (Net 3). One coincidence never promotes: a single overlapping period may promote ONLY on hard law
# closure (arithmetic identity), never on context alone (control #4). Any fired net blocks.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def promote(net1: NetVerdict, net2: NetVerdict, net3: NetVerdict) -> bool:
    if net1.base is None:                         # UNRESOLVED base (decompose could not name it) → held
        return False                              # model-off, NEVER merged (§2 fail-closed): two distinct
        #                                           unknowns that coincide are not one concept — a mislabel
        #                                           must become a HOLD, never a confident-but-wrong merge.
    if net1.fired() or net2.fired() or net3.fired():
        return False
    if net1.outcome != AGREE:                     # no real cross-source overlap → stays asserted (§2)
        return False
    law_closure = net2.outcome == RECONCILED
    homogeneous = net3.outcome == HOMOGENEOUS
    multi_period = len(net1.overlap_periods) >= 2
    # hard arithmetic closure is not a coincidence → promotes even single-period; context homogeneity
    # is a softer signal → needs multi-period agreement to rule out a lone coincidental match.
    return law_closure or (multi_period and homogeneous)


def run_nets(observations, *, tol_abs: Decimal = _DEF_TOL_ABS, tol_frac: Decimal = _DEF_TOL_FRAC):
    """Run all three nets over a candidate MERGE group and return (net1, net2, net3, promoted).
    A convenience for callers/audit-replay; each net is independently usable."""
    n1 = net1_collision(observations, tol_abs=tol_abs, tol_frac=tol_frac)
    n2 = net2_movement(observations, tol_abs=tol_abs, tol_frac=tol_frac)
    n3 = net3_context(observations)
    return n1, n2, n3, promote(n1, n2, n3)
