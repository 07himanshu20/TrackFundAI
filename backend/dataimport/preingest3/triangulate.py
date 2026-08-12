"""
S5b — Deterministic Triangulation (U2). Three INDEPENDENT signals, all read by
code from the open file — none passes through the model, which is what makes
them independent (repetition is not verification, build rule #6). All three
agree → freeze. Any one dissents → escalate. Confidence is COMPUTED here, never
reported by the model (build rule #4).

  Signal 1 — Existence & plausibility: the address resolves to a populated cell
             of the expected type, within a magnitude band of its peers (a row
             index or a stray year sits far outside the band and is caught).
  Signal 2 — Label: code re-reads the row label at the located cell and matches
             it to the concept via the growing lexicon — independently of what
             the model claimed. For an expression (segment split) the row label
             is a component name, so this signal is N/A and the other two carry.
  Signal 3 — Arithmetic identities: the located cells must satisfy identities the
             model was never shown (revenue−cogs=gross_profit, l+e=assets,
             opening+receipts−payments=closing, Σperiods=total), each with its
             OWN tolerance. A break is a dissent (escalate), never a hard-fail —
             real books don't tie to the rupee.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from . import gate, lexicon
from .contract import IDENTITIES, PERIOD_SUM_TOLERANCE, TOLERANCES
from .gate import Signal, PASS, SOFT, FAIL, NA, AUTO, AUDIT, HUMAN
from .locator_schema import FORM_EXPRESSION
from .profiler import _cell_type
from .quantity import to_decimal
from .reader import ReadFigure
from .statements import Statement

_BAND = Decimal(str(TOLERANCES['magnitude_band']))
_TIGHT_FRACTION = Decimal('0.25')      # identity within 25% of its tolerance = tight (strong)

SIG_EXISTENCE = 'existence'
SIG_LABEL = 'label'
SIG_IDENTITY = 'identity'


@dataclass
class Verdict:
    concept: str
    status: str                                  # gate tier: auto_accept | audit | human
    value_native: Optional[Decimal]
    signals: List[Signal] = field(default_factory=list)
    signals_passed: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    confidence: float = 0.0


def _abs_native(fig: ReadFigure) -> Optional[Decimal]:
    if not fig.read_ok or fig.quantity is None:
        return None
    return fig.quantity.absolute_native()


# ── Signal 1 ─────────────────────────────────────────────────────────────
def _band_ok(value: Decimal, peers: List[Decimal]) -> bool:
    mags = [abs(p) for p in peers if p is not None and p != 0]
    if abs(value) == 0 or len(mags) < 2:
        return True                     # nothing to compare against → don't fail
    lo, hi = min(mags) / _BAND, max(mags) * _BAND
    return lo <= abs(value) <= hi


# ── Signal 2 ─────────────────────────────────────────────────────────────
def _read_row_label(grid, sheet, st: Statement, row0: int) -> str:
    rows = grid.get(sheet) or []
    if st.label_col is None or row0 >= len(rows):
        return ''
    row = rows[row0]
    v = row[st.label_col] if st.label_col < len(row) else None
    return str(v).strip() if _cell_type(v) == 'text' else ''


# ── Signal 3 ─────────────────────────────────────────────────────────────
def _tol(target: Decimal, tol_rel, tol_abs_cr) -> Decimal:
    # native-unit tolerance: relative to magnitude, with a small relative floor
    # (tol_abs_cr is a post-normalisation crore floor; intra-statement we lean on
    # tol_rel, which is currency/scale-agnostic).
    base = max(abs(target), Decimal('1'))
    return base * Decimal(str(tol_rel))


def evaluate_identities(figs: Dict[str, ReadFigure]) -> List[dict]:
    """Run every identity whose concepts are all present & read. Returns a list
    of {name, expr_sum, target, variance, ok, tol, severity, concepts}."""
    results = []
    for idn in IDENTITIES:
        concepts = [c for c, _ in idn['expr']] + [idn['equals']]
        vals = {c: _abs_native(figs[c]) for c in concepts if c in figs}
        if any(vals.get(c) is None for c in concepts):
            continue                    # not enough located to test — no signal
        expr_sum = sum((Decimal(sign) * vals[c] for c, sign in idn['expr']), Decimal('0'))
        target = vals[idn['equals']]
        variance = expr_sum - target
        tol = _tol(target, idn['tol_rel'], idn['tol_abs_cr'])
        ok = abs(variance) <= tol
        tight = abs(variance) <= tol * _TIGHT_FRACTION
        results.append({'name': idn['name'], 'expr_sum': expr_sum, 'target': target,
                        'variance': variance, 'ok': ok, 'tight': tight, 'tol': tol,
                        'severity': idn['severity'], 'concepts': concepts})
    return results


def period_sum_check(period_total_fig: ReadFigure, records_by_concept, grid,
                     st: Statement) -> Optional[dict]:
    """Σ(period columns) == stated total, read structurally from the total's own
    row (the single most common layout error). Returns a result dict or None."""
    rec = records_by_concept.get('period_total')
    if rec is None or not rec.resolved or not period_total_fig.read_ok:
        return None
    _sheet, tcol, trow = rec.resolved[0]
    rows = grid.get(st.sheet) or []
    if trow >= len(rows):
        return None
    row = rows[trow]
    start = (st.label_col + 1) if st.label_col is not None else 0
    period_vals = [to_decimal(row[c]) for c in range(start, min(tcol, len(row)))]
    period_vals = [v for v in period_vals if v is not None]
    if len(period_vals) < 2:
        return None
    s = sum(period_vals, Decimal('0'))
    target = period_total_fig.raw
    tol = _tol(target, PERIOD_SUM_TOLERANCE['tol_rel'], PERIOD_SUM_TOLERANCE['tol_abs_cr'])
    var = s - target
    return {'name': 'period_sum', 'expr_sum': s, 'target': target,
            'variance': var, 'ok': abs(var) <= tol, 'tight': abs(var) <= tol * _TIGHT_FRACTION,
            'tol': tol, 'severity': PERIOD_SUM_TOLERANCE['severity'], 'concepts': ['period_total']}


def triangulate(st: Statement, records_by_concept: dict, figs: Dict[str, ReadFigure],
                grid: Dict[str, List[List[Any]]], *, context: str = 'global',
                first_seen: bool = False) -> dict:
    """Return {'verdicts': {concept: Verdict}, 'identities': [...]}. Each verdict's
    status is a gate TIER: auto_accept | audit | human. `context` (a layout
    fingerprint) and `first_seen` drive the shared gate's stricter-until-proven
    grading; both come from the extractor/template registry."""
    peers = [f.raw for f in figs.values() if f.read_ok and f.raw is not None]
    identities = evaluate_identities(figs)
    pt = figs.get('period_total')
    if pt is not None:
        ps = period_sum_check(pt, records_by_concept, grid, st)
        if ps:
            identities.append(ps)
    broken = {c for idn in identities if not idn['ok'] for c in idn['concepts']}
    # a concept is 'tight' if EVERY identity it participates in holds tightly
    part = {}
    for idn in identities:
        for c in idn['concepts']:
            part.setdefault(c, []).append(idn)

    have_peers = len([p for p in peers if p is not None]) >= 2

    verdicts: Dict[str, Verdict] = {}
    for concept, fig in figs.items():
        rec = records_by_concept.get(concept)

        # Signal 1 — existence & plausibility
        if not fig.read_ok:
            s_exist = Signal(SIG_EXISTENCE, FAIL)
        elif not have_peers:
            s_exist = Signal(SIG_EXISTENCE, SOFT)      # can't band-check → soft
        else:
            s_exist = Signal(SIG_EXISTENCE, PASS if _band_ok(fig.raw, peers) else FAIL)

        # Signal 2 — label (N/A for an expression / segment-split)
        if rec is not None and rec.form == FORM_EXPRESSION:
            s_label = Signal(SIG_LABEL, NA)
        elif rec is not None and rec.resolved:
            found = _read_row_label(grid, st.sheet, st, rec.resolved[0][2])
            strength = lexicon.match_strength(found, concept)
            s_label = Signal(SIG_LABEL, {'exact': PASS, 'contains': SOFT, 'none': FAIL}[strength])
        else:
            s_label = Signal(SIG_LABEL, NA)

        # Signal 3 — identities
        if concept in broken:
            s_ident = Signal(SIG_IDENTITY, FAIL)
        elif concept not in part:
            s_ident = Signal(SIG_IDENTITY, SOFT)       # no identity to test → soft
        else:
            s_ident = Signal(SIG_IDENTITY, PASS if all(i['tight'] for i in part[concept]) else SOFT)

        signals = [s_exist, s_label, s_ident]
        dec = gate.decide_by_evidence(signals, context=context,
                                      subject=f'{st.sheet}:{concept}', first_seen=first_seen)
        passed = [s.name for s in signals if s.outcome == PASS]
        verdicts[concept] = Verdict(
            concept=concept, status=dec.tier,
            value_native=fig.raw if fig.read_ok else None,
            signals=signals, signals_passed=passed, reasons=dec.reasons,
            confidence=round(len(passed) / max(len([s for s in signals if s.outcome != NA]), 1), 3),
        )
    return {'verdicts': verdicts, 'identities': identities}
