"""
The shared gated-decision component. ONE implementation of floor + margin +
evidence tiers + a CLOSED audit loop + bidirectional auto-shrink + write-back —
called by BOTH triangulation (U2) and the Alias Ledger (U4), so the safety logic
stays universal and cannot drift into two copies.

Two decision shapes:
  • decide_by_evidence(signals) — for a verified value. Tier is set by the
    STRUCTURE of the evidence, never a tuned score:
        any signal FAILS / identity breaks      → HUMAN   (dissent)
        all applicable PASS, tight, exact        → AUTO    (strong)
        all applicable pass but ≥1 SOFT or N/A   → AUDIT   (medium)
    An expression/segment-split figure has label = N/A (only two signals), so it
    can reach at most AUDIT — never AUTO. A first-seen layout is graded one notch
    stricter until it earns a track record (auto-relaxes via the registry).
  • decide_by_ranking(candidates) — for choosing among ranked candidates (U4).
    Requires BOTH an absolute floor AND a margin over #2. A wide margin with a
    weak absolute top score is NOT a match (the right entity may be absent) → it
    fails closed. This is the exact silent-catastrophe guard.

MEDIUM is only safe because the audit loop is CLOSED: every AUDIT decision is
recorded, sampled for human resolution, and the outcome is written back —
confirm → promote (earns trust, gate relaxes); overturn → correct + TIGHTEN
(gate shrinks immediately). If the audit backlog goes unattended past a bound,
AUDIT auto-routes to HUMAN — the loop self-protects, so 'medium' can never decay
into silent auto-accept.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# ── tiers ────────────────────────────────────────────────────────────────
AUTO = 'auto_accept'      # strong evidence → accept, no human
AUDIT = 'audit'           # medium → accept BUT recorded in the closed audit loop
HUMAN = 'human'           # dissent / ambiguity / weak → hold for a person
_STRICTER = {AUTO: AUDIT, AUDIT: HUMAN, HUMAN: HUMAN}

# ── signal outcomes ──────────────────────────────────────────────────────
PASS = 'pass'
SOFT = 'soft'             # passed, but weakly / uncheckable
FAIL = 'fail'
NA = 'na'                 # not applicable (e.g. label for an expression)

# ── policy parameters (explicit decision policy, not hidden thresholds) ───
RELAX_AFTER_CONFIRMS = 10       # confirms with no overturn before a context relaxes
AUDIT_BACKLOG_BOUND = 50        # unresolved audits per context before AUDIT→HUMAN

_MEDIA = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'media')
_AUDIT_PATH = os.path.join(_MEDIA, 'preingest3_audit.json')
_RELI_PATH = os.path.join(_MEDIA, 'preingest3_reliability.json')
_lock = threading.Lock()


@dataclass
class Signal:
    name: str
    outcome: str            # PASS | SOFT | FAIL | NA


@dataclass
class Decision:
    tier: str
    chosen: Optional[object] = None       # for ranking decisions
    reasons: List[str] = field(default_factory=list)
    evidence: List[Signal] = field(default_factory=list)
    no_match: bool = False                # ranking: nothing cleared the floor


# ── persistence helpers ──────────────────────────────────────────────────
def _load(path) -> dict:
    if os.path.exists(path):
        try:
            with open(path) as fh:
                return json.load(fh)
        except Exception:
            return {}
    return {}


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


# ── reliability: the bidirectional auto-shrink, per context ──────────────
def reliability_mode(context: str) -> str:
    """'relaxed' once a context has earned RELAX_AFTER_CONFIRMS confirms with no
    intervening overturn; 'strict' otherwise. Strict means AUDIT routes to HUMAN
    — medium is not trusted until the context proves itself."""
    st = _load(_RELI_PATH).get(context or 'global', {})
    return 'relaxed' if st.get('mode') == 'relaxed' else 'strict'


def _bump(context: str, *, confirm: bool):
    with _lock:
        data = _load(_RELI_PATH)
        st = data.setdefault(context or 'global', {'confirms': 0, 'overturns': 0, 'mode': 'strict'})
        if confirm:
            st['confirms'] += 1
            if st['confirms'] >= RELAX_AFTER_CONFIRMS:
                st['mode'] = 'relaxed'
        else:
            # an overturn TIGHTENS immediately and resets earned trust
            st['overturns'] += 1
            st['confirms'] = 0
            st['mode'] = 'strict'
        _save(_RELI_PATH, data)


def _audit_backlog(context: str) -> int:
    data = _load(_AUDIT_PATH)
    return sum(1 for e in data.get('entries', [])
               if e.get('context') == context and not e.get('resolved'))


# ── the closed audit loop ────────────────────────────────────────────────
def audit_record(context: str, subject: str, decision: 'Decision') -> str:
    """Record an AUDIT-tier decision for later sampled resolution. Returns id."""
    key = hashlib.sha256(f'{context}|{subject}'.encode()).hexdigest()[:16]
    with _lock:
        data = _load(_AUDIT_PATH)
        entries = data.setdefault('entries', [])
        entries.append({'id': key, 'context': context, 'subject': subject,
                        'tier': decision.tier, 'reasons': decision.reasons,
                        'resolved': False, 'outcome': None})
        _save(_AUDIT_PATH, data)
    return key


def audit_sample(limit: int = 20, context: str = None) -> List[dict]:
    """Surface unresolved AUDIT decisions for a human to check (the SAMPLE step
    of the closed loop). Deterministic order for reproducibility."""
    data = _load(_AUDIT_PATH)
    pend = [e for e in data.get('entries', [])
            if not e.get('resolved') and (context is None or e.get('context') == context)]
    return sorted(pend, key=lambda e: e['id'])[:limit]


def audit_resolve(entry_id: str, confirmed: bool, by: str = 'reviewer') -> bool:
    """Resolve a sampled audit and WRITE BACK: confirm → promote (relax);
    overturn → correct + tighten. This is what closes the loop."""
    with _lock:
        data = _load(_AUDIT_PATH)
        found = None
        for e in data.get('entries', []):
            if e['id'] == entry_id and not e.get('resolved'):
                e['resolved'] = True
                e['outcome'] = 'confirm' if confirmed else 'overturn'
                e['by'] = by
                found = e
                break
        if found:
            _save(_AUDIT_PATH, data)
    if found:
        _bump(found['context'], confirm=confirmed)
        return True
    return False


def _apply_policy(raw_tier: str, context: str, first_seen: bool, subject: str,
                  reasons: List[str]) -> str:
    """Grade a first-seen layout stricter; route AUDIT→HUMAN while a context is
    unproven or its audit backlog is unattended; otherwise honour the raw tier."""
    tier = raw_tier
    if first_seen:
        tier = _STRICTER[tier]
    if tier == AUDIT:
        if reliability_mode(context) == 'strict':
            tier = HUMAN
            reasons.append('context not yet trusted (strict) — medium routed to human')
        elif _audit_backlog(context) >= AUDIT_BACKLOG_BOUND:
            tier = HUMAN
            reasons.append('audit backlog unattended — medium routed to human (loop self-protect)')
    return tier


# ── decision 1: evidence-structured (triangulation) ──────────────────────
def decide_by_evidence(signals: List[Signal], *, context: str = 'global',
                       subject: str = '', first_seen: bool = False) -> Decision:
    applicable = [s for s in signals if s.outcome != NA]
    reasons = []
    if any(s.outcome == FAIL for s in applicable):
        reasons += [f'{s.name} failed' for s in applicable if s.outcome == FAIL]
        return Decision(HUMAN, reasons=reasons, evidence=signals)
    has_soft = any(s.outcome == SOFT for s in applicable)
    has_na = any(s.outcome == NA for s in signals)
    raw = AUTO if (not has_soft and not has_na) else AUDIT
    if has_na:
        reasons.append('a signal is N/A (e.g. expression/segment-split) — capped at medium')
    if has_soft:
        reasons.append('a signal passed only softly — medium')
    tier = _apply_policy(raw, context, first_seen, subject, reasons)
    dec = Decision(tier, reasons=reasons, evidence=signals)
    if tier == AUDIT:
        audit_record(context, subject, dec)
    return dec


# ── decision 2: ranked candidates (alias ledger) ─────────────────────────
def rank(candidates: List[Tuple[object, float]], *, floor: float, gap: float):
    """The PURE floor+margin decision — no state, no audit, no I/O. Returns
    (chosen, ok, margin, reason): ok=True iff top ≥ floor AND top beats the runner-
    up by ≥ gap. This is the single source of the 'confident unique winner' rule,
    reused by decide_by_ranking (stateful entity resolution) AND by any bulk,
    deterministic ranking (e.g. per-row concept binding) that must NOT write to the
    audit log. Fail closed: a weak or ambiguous top is ok=False."""
    ranked = sorted(candidates, key=lambda c: -c[1])
    if not ranked or ranked[0][1] < floor:
        top_s = ranked[0][1] if ranked else 0.0
        return None, False, 0.0, f'top {top_s:.2f} < floor {floor} — no confident match'
    top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = top[1] - second
    if margin < gap:
        return top[0], False, margin, f'top two within {margin:.2f} (< gap {gap}) — ambiguous'
    return top[0], True, margin, f'top {top[1]:.2f} ≥ floor {floor}, margin {margin:.2f} ≥ {gap}'


def decide_by_ranking(candidates: List[Tuple[object, float]], *, floor: float,
                      gap: float, context: str = 'global', subject: str = '',
                      first_seen: bool = False) -> Decision:
    """Stateful entity-resolution decision over `rank()`: adds tier policy + the
    closed audit loop. Weak top → no_match; ambiguous margin → HUMAN; else AUTO
    (possibly downgraded to AUDIT by reliability policy)."""
    chosen, ok, margin, reason = rank(candidates, floor=floor, gap=gap)
    if chosen is None:
        return Decision(HUMAN, no_match=True, reasons=[reason + ' (right entity may be absent)'])
    if not ok:
        return Decision(HUMAN, chosen=chosen, reasons=[reason])
    reasons = [reason]
    tier = _apply_policy(AUTO, context, first_seen, subject, reasons)
    dec = Decision(tier, chosen=chosen, reasons=reasons)
    if tier == AUDIT:
        audit_record(context, subject or str(chosen), dec)
    return dec


def stats() -> dict:
    a = _load(_AUDIT_PATH).get('entries', [])
    return {'audit_total': len(a), 'audit_unresolved': sum(1 for e in a if not e.get('resolved')),
            'contexts': _load(_RELI_PATH)}
