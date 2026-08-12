"""
U4 — The Alias Ledger + entity resolution (S6). The worst failure the system can
produce is silent: every figure correctly extracted, then attached to the WRONG
company. No arithmetic check catches that, so this stage is built to fail closed.

Resolution order (all reusing the shared gate — no second copy of the safety
logic):
  1. Alias Ledger hit — authoritative, BUT re-validated against THIS run's
     investment schedule. An alias approved months ago can go stale (the fund
     exited/renamed the investment); a stale hit is escalated, never trusted
     blindly. The hard constraint "every entity must exist in the schedule" is
     never overridden by the ledger.
  2. Deterministic candidate scoring over ALL observed identifiers (filename,
     classifier entity, cell/sheet hints) vs the schedule → gate.decide_by_ranking
     (absolute floor AND margin). Weak-but-wide → no_match → HALT.
  3. Model adjudication ONLY on an ambiguous outcome, over a STRUCTURALLY CLOSED
     set (candidate ids + 'none' — a returned id outside the set is discarded, so
     the model cannot invent an entity). Its pick is fed BACK through the floor
     before acceptance (a confident model pick is not evidence).
  4. Human confirmation on anything still unresolved; the decision writes back to
     the ledger so it is asked once.

Also enforced here: duplicate detection keyed on (entity, period, statement_type)
→ HOLD both for human (never auto-pick a file); and reverse coverage — an
investment in the schedule with no MIS file this run is emitted as a disclosed
gap, not silently omitted.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from . import gate
from .namematch import acronym as _acronym  # noqa: F401  (re-export, kept for callers)
from .namematch import similarity
from .namematch import tokens as _tokens  # noqa: F401  (re-export, kept for callers)

# Explicit decision policy (not hidden thresholds — the gate makes them visible).
MATCH_FLOOR = 0.60      # a candidate must clear this absolute similarity to be a match
MATCH_GAP = 0.15        # and beat the runner-up by this margin

_STORE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                      'media', 'preingest3_alias_ledger.json')
_lock = threading.Lock()


@dataclass
class Resolution:
    status: str                       # 'resolved' | 'held'
    entity_id: Optional[str] = None
    tier: str = ''                    # gate tier
    reasons: List[str] = field(default_factory=list)
    identifiers: List[str] = field(default_factory=list)
    period: Optional[str] = None
    statement_type: Optional[str] = None
    source_label: str = ''


# ── the ledger store (client-scoped identifier → entity id) ──────────────
class AliasLedger:
    def __init__(self, org: str = 'default', path: str = None):
        self.org = org
        self.path = path or _STORE

    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path) as fh:
                    return json.load(fh)
            except Exception:
                return {}
        return {}

    def _save(self, data):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self.path)

    @staticmethod
    def _entry_id(v):
        return v.get('id') if isinstance(v, dict) else v      # back-compat with bare-string entries

    def lookup(self, identifiers: List[str]) -> Optional[str]:
        table = self._load().get(self.org, {})
        for ident in identifiers:
            key = ' '.join(_tokens(ident))
            if key and key in table:
                return self._entry_id(table[key])
        return None

    def learn(self, identifiers: List[str], entity_id: str, *, provenance: str = 'auto'):
        """Persist identifier→entity. Every entry is TAGGED with its provenance
        ('auto' = machine-resolved, 'human' = reviewer-confirmed) so a bad auto
        resolution can be distrusted and purged (purge_auto) rather than
        authoritatively poisoning every future run through the cache."""
        with _lock:
            data = self._load()
            table = data.setdefault(self.org, {})
            for ident in identifiers:
                key = ' '.join(_tokens(ident))
                if key:
                    # a human confirmation is never overwritten by a later auto guess
                    if isinstance(table.get(key), dict) and table[key].get('src') == 'human' and provenance != 'human':
                        continue
                    table[key] = {'id': entity_id, 'src': provenance}
            self._save(data)

    def purge_auto(self):
        """Drop every auto-resolved entry (keep human-confirmed) — the recovery
        path when a bad run poisons the durable store."""
        with _lock:
            data = self._load()
            table = data.get(self.org, {})
            data[self.org] = {k: v for k, v in table.items()
                              if isinstance(v, dict) and v.get('src') == 'human'}
            self._save(data)


# ── candidate scoring (deterministic, over ALL identifiers) ──────────────
def score_candidates(identifiers: List[str], schedule: List[dict]) -> List[Tuple[dict, float]]:
    """schedule = [{'id','name', optional 'aliases':[...]}]. Score each investment
    by its BEST match across all observed identifiers and its own name+aliases.
    Deterministic tie-break by investment id (stable sort preserves it)."""
    scored = []
    for inv in schedule:
        names = [inv.get('name', '')] + list(inv.get('aliases') or [])
        best = max((similarity(ident, nm) for ident in identifiers for nm in names
                    if ident and nm), default=0.0)
        scored.append((inv, round(best, 4)))
    # stable, deterministic ordering: id asc first, so equal scores never flip run-to-run
    scored.sort(key=lambda c: str(c[0].get('id')))
    scored.sort(key=lambda c: -c[1])
    return scored


# ── model adjudication over a STRUCTURALLY CLOSED set ────────────────────
_ADJ_PROMPT = """Choose which fund investment this uploaded file belongs to, or none.
You MUST pick an id from the list below, or "none". You cannot name any entity outside the list.

FILE identifiers observed: {idents}

CANDIDATES:
{cands}

Return STRICT JSON: {"choice":"<id from the list, or 'none'>","reason":"one short sentence"}
"""


def adjudicate(identifiers: List[str], candidates: List[dict], *, content_fp: str) -> Optional[str]:
    """Bounded closed-set choice. A returned id NOT in the candidate set is
    discarded (returns None) — the closed set is enforced in code, so the model
    can never inject an entity outside the list."""
    from . import llm   # lazy — the model (→api) is only needed for AMBIGUOUS
    # adjudication; deterministic ledger+scoring resolution stays import-light/offline
    valid_ids = {str(c['id']) for c in candidates}
    cand_lines = '\n'.join(f"- id={c['id']}: {c.get('name','')}" for c in candidates)
    prompt = (_ADJ_PROMPT.replace('{idents}', ' | '.join(identifiers))
              .replace('{cands}', cand_lines))
    res = llm.call_json('alias_adjudicate', f'{content_fp}|{sorted(valid_ids)}', prompt,
                        read_timeout_s=45.0)
    if res.is_error or not isinstance(res.data, dict):
        return None    # error/garbled → unresolved → routed to human (never a wrong pick)
    choice = str(res.data.get('choice') or '').strip()
    return choice if choice in valid_ids else None    # structural closure


# ── the resolver ─────────────────────────────────────────────────────────
def resolve_file(identifiers: List[str], schedule: List[dict], *, ledger: AliasLedger,
                 content_fp: str, period: str = None, statement_type: str = None,
                 first_seen: bool = False, source_label: str = '') -> Resolution:
    """Resolve one file to an investment. Fails closed on no-match."""
    identifiers = [i for i in identifiers if i]
    schedule_ids = {str(inv['id']) for inv in schedule}
    base = dict(identifiers=identifiers, period=period, statement_type=statement_type,
                source_label=source_label)

    # 1. Ledger hit — authoritative BUT re-validated against the current schedule.
    hit = ledger.lookup(identifiers)
    if hit is not None:
        if str(hit) in schedule_ids:
            return Resolution('resolved', entity_id=str(hit), tier=gate.AUTO,
                              reasons=['alias ledger hit, re-validated in schedule'], **base)
        return Resolution('held', tier=gate.HUMAN,
                          reasons=[f'STALE ledger: alias maps to {hit!r}, absent from this '
                                   f"run's schedule — escalate, do not trust"], **base)

    if not schedule:
        return Resolution('held', tier=gate.HUMAN, reasons=['no investment schedule to match against'], **base)

    # 2. Deterministic candidate scoring → shared ranking gate (floor AND margin).
    scored = score_candidates(identifiers, schedule)
    dec = gate.decide_by_ranking([(inv, sc) for inv, sc in scored], floor=MATCH_FLOOR,
                                 gap=MATCH_GAP, context='alias:' + ledger.org,
                                 subject=source_label or ' '.join(identifiers), first_seen=first_seen)
    if dec.tier in (gate.AUTO, gate.AUDIT) and dec.chosen is not None:
        eid = str(dec.chosen['id'])
        ledger.learn(identifiers, eid)               # write back — asked once
        return Resolution('resolved', entity_id=eid, tier=dec.tier, reasons=dec.reasons, **base)

    if dec.no_match:
        return Resolution('held', tier=gate.HUMAN,
                          reasons=dec.reasons + ['no candidate cleared the floor — HALT (held, disclosed)'],
                          **base)

    # 3. Ambiguous → bounded closed-set model adjudication, gated back through the floor.
    top = [inv for inv, _ in scored[:5]]
    pick = adjudicate(identifiers, top, content_fp=content_fp)
    if pick is not None and pick in schedule_ids:
        inv = next(i for i in schedule if str(i['id']) == pick)
        pick_score = max((similarity(ident, nm) for ident in identifiers
                          for nm in [inv.get('name', '')] + list(inv.get('aliases') or [])), default=0.0)
        if pick_score >= MATCH_FLOOR:
            # model pick corroborated by deterministic score → accept as AUDIT (logged, needs spot-check)
            gate.audit_record('alias:' + ledger.org, source_label or pick,
                              gate.Decision(gate.AUDIT, chosen=inv, reasons=['model-adjudicated, floor-corroborated']))
            ledger.learn(identifiers, pick)
            return Resolution('resolved', entity_id=pick, tier=gate.AUDIT,
                              reasons=[f'model adjudication corroborated by score {pick_score:.2f} ≥ floor'], **base)

    # 4. Still unresolved → human.
    return Resolution('held', tier=gate.HUMAN,
                      reasons=dec.reasons + ['ambiguous; model adjudication not corroborated — human confirm'], **base)


# ── run-level checks: duplicates + reverse coverage ──────────────────────
def find_duplicates(resolutions: List[Resolution]) -> List[dict]:
    """Collisions keyed on (entity, period, statement_type) — a company's P&L
    file and KPI file do NOT collide (different statement_type). A true same-slot
    collision is HELD for human (never auto-pick a file)."""
    seen: Dict[tuple, List[Resolution]] = {}
    for r in resolutions:
        if r.status != 'resolved':
            continue
        key = (r.entity_id, r.period, r.statement_type)
        seen.setdefault(key, []).append(r)
    dups = []
    for key, group in seen.items():
        if len(group) > 1:
            dups.append({'key': key, 'files': [g.source_label for g in group],
                         'disposition': 'HOLD for human — do not auto-pick an authoritative file'})
    return dups


def missing_investments(schedule: List[dict], resolved_entity_ids) -> List[dict]:
    """Reverse coverage — investments with NO MIS file this run, emitted as
    disclosed gaps (not silent omissions)."""
    resolved = {str(e) for e in resolved_entity_ids}
    return [{'kind': 'investment_without_mis', 'entity_id': str(inv['id']),
             'detail': f"investment {inv.get('name', inv['id'])!r} had no MIS file this run"}
            for inv in schedule if str(inv['id']) not in resolved]
