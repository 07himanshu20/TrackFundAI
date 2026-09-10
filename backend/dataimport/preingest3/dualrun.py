"""Dual-run safety gate — the byte-identical discipline we run BY HAND on every change,
turned into infrastructure, and the precondition for safely activating the model.

WHY THIS EXISTS
    All session we have proven a change safe the same way: run the full corpus before and
    after, diff every emitted value + every hold + every hold-reason + provenance, and ship
    only when the diff is exactly what we intended (usually: nothing). That is a manual
    dual-run. This module makes it an artefact: a stored GOLDEN signature of the current
    shipped output, a before/after DIFF against it, and — the load-bearing part — a
    classification of each difference BY RISK, so the one place a wrong number can enter is
    the loudest thing on the screen.

THE ONE INVARIANT IT PROTECTS ("never a wrong number")
    A difference is not automatically a regression; some are intended improvements. The gate
    does not decide right/wrong — it SURFACES, ranked, and a human ACCEPTS (updates the
    golden) or REJECTS. What it guarantees is that a NUMBER APPEARING where none was trusted
    before (a held/gap figure becoming an emitted value, or a brand-new emitted record) can
    never slip through silently: that class is CRITICAL and BLOCKS. A trusted number that
    MOVES is HIGH and blocks. A trusted number WITHDRAWN (emit → held/gap) is fail-closed
    coverage loss — surfaced, but it ships no wrong number, so it does not block.

    Risk order (advisor framing, and the project's standing rule):
      CRITICAL  held/gap → emit, or a new emitted record   ← a number appears (wrong-number door)
      HIGH      emit value changed, or emit basis changed   ← a trusted number/meaning moves
      MEDIUM    emit → held/gap, or an emitted record gone  ← coverage loss (fail-closed, safe)
      LOW       reason / provenance churn on a non-emit      ← informational
    BLOCKING = CRITICAL or HIGH.

WHY DEV-LOCAL, NOT CI
    The corpus is real customer workbooks under a gitignored media/ tree; their financial
    values must never be committed. So the golden signature is written to a gitignored path
    and the corpus run is a developer command. What IS committed and CI-tested is the pure
    diff+classify logic (test_dualrun.py, synthetic signatures) — including the reddening
    control that proves a held→emit transition fires CRITICAL. The real-corpus tripwire is a
    skip-if-absent test, matching the codebase's @_real idiom.

PRODUCTION PATH ONLY
    signature() reads a RunResult produced by pipeline.run — the REAL extraction path, never
    a reconstruction (audits-use-production-path). run_corpus() is the thin, file-name-blind
    (universal glob) driver over a directory of workbooks.

USAGE
    python -m backend.dataimport.preingest3.dualrun snapshot --golden <path>   # first baseline
    python -m backend.dataimport.preingest3.dualrun check    --golden <path>   # diff vs golden (exit 1 if blocked)
    python -m backend.dataimport.preingest3.dualrun accept   --golden <path>   # review diff, then promote
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .cir import Figure

# ── severity vocabulary ────────────────────────────────────────────────────────────
CRITICAL, HIGH, MEDIUM, LOW = 'CRITICAL', 'HIGH', 'MEDIUM', 'LOW'
_RANK = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3}
_BLOCKING = (CRITICAL, HIGH)

# a natural, gitignored home for the golden — beside the corpus, never committed
_DEFAULT_IN = 'media/preingest/trivesta/100e86d5/in'
_DEFAULT_GOLDEN = 'media/preingest/dualrun_golden.json'


def _state(f: Figure) -> str:
    if f.confirmed:
        return 'EMIT'
    if f.held:
        return 'HELD'
    if f.gap:
        return 'GAP'
    return '?'


def _row(f: Figure) -> dict:
    p = f.provenance
    return {
        'state': _state(f),
        'value_cr': '' if f.value_cr is None else str(f.value_cr),
        'basis': f.basis or '',
        'value_basis': f.value_basis or '',
        'sheet': (getattr(p, 'sheet', '') or ''),
        'cell': (getattr(p, 'cell', '') or ''),
        'hold_reason': (f.hold_reason or ''),
    }


def signature(res) -> Dict[str, dict]:
    """Full-output signature of a RunResult: a deterministic, total identity → row map.

    Identity is (domain, entity, concept) — the logical thing the figure is ABOUT, so a
    number can be tracked across runs even as its source cell moves. Collisions (rare: two
    figures for the same concept on one entity) are disambiguated by source location, then
    position, so the map is always 1:1 and stable."""
    pairs: List[Tuple[str, dict]] = []
    for r in res.cir.records:
        ent = r.entity_id or ''
        for v in r.fields.values():
            if not isinstance(v, Figure):
                continue
            pairs.append((f'{r.domain}|{ent}|{v.concept}', _row(v)))
    return _disambiguate(pairs)


def _disambiguate(pairs: List[Tuple[str, dict]]) -> Dict[str, dict]:
    groups: Dict[str, List[dict]] = defaultdict(list)
    for base, row in pairs:
        groups[base].append(row)
    out: Dict[str, dict] = {}
    for base in sorted(groups):
        grp = groups[base]
        if len(grp) == 1:
            out[base] = grp[0]
            continue
        grp = sorted(grp, key=lambda r: (r['sheet'], r['cell'], r['value_cr'], r['state']))
        for i, row in enumerate(grp):
            k = f"{base}@{row['sheet']}!{row['cell']}"
            if k in out:
                k = f'{k}#{i}'
            out[k] = row
    return out


# ── diff + classification ──────────────────────────────────────────────────────────
@dataclass
class Change:
    key: str
    kind: str               # NEW | REMOVED | CHANGED
    severity: str
    before: Optional[dict]
    after: Optional[dict]
    detail: str


@dataclass
class DiffReport:
    changes: List[Change] = field(default_factory=list)
    n_golden: int = 0
    n_current: int = 0

    @property
    def blocks(self) -> bool:
        return any(c.severity in _BLOCKING for c in self.changes)

    def by_severity(self, sev: str) -> List[Change]:
        return [c for c in self.changes if c.severity == sev]


def _classify(before: Optional[dict], after: Optional[dict]) -> Optional[Tuple[str, str]]:
    """(severity, detail) for one identity, or None if nothing changed. Encodes the risk
    order above: a number appearing is the wrong-number door (CRITICAL); a trusted number or
    its meaning moving is HIGH; a trusted number withdrawn is fail-closed coverage loss
    (MEDIUM); everything else is informational (LOW)."""
    if before is None:                                    # new identity
        if after['state'] == 'EMIT':
            return CRITICAL, f"new EMIT {after['value_cr']} @ {after['sheet']}!{after['cell']}"
        return LOW, f"new {after['state']}"
    if after is None:                                     # identity gone
        if before['state'] == 'EMIT':
            return MEDIUM, f"EMIT {before['value_cr']} removed (coverage loss)"
        return LOW, f"{before['state']} removed"

    bs, as_ = before['state'], after['state']
    if bs == 'EMIT' and as_ == 'EMIT':
        if before['value_cr'] != after['value_cr']:
            return HIGH, f"value {before['value_cr']} → {after['value_cr']}"
        if (before['basis'], before['value_basis']) != (after['basis'], after['value_basis']):
            return HIGH, (f"basis {before['basis']}/{before['value_basis']} → "
                          f"{after['basis']}/{after['value_basis']} (same value, meaning moved)")
        if (before['sheet'], before['cell']) != (after['sheet'], after['cell']):
            return LOW, (f"same value, source moved {before['sheet']}!{before['cell']} → "
                         f"{after['sheet']}!{after['cell']}")
        return None                                       # identical

    if as_ == 'EMIT':                                     # held/gap → emit : a NUMBER APPEARS
        return CRITICAL, f"{bs} → EMIT {after['value_cr']} @ {after['sheet']}!{after['cell']}"
    if bs == 'EMIT':                                      # emit → held/gap : coverage loss
        return MEDIUM, f"EMIT {before['value_cr']} → {as_}"
    if before.get('hold_reason', '') != after.get('hold_reason', ''):
        return LOW, f"{bs} → {as_} (hold-reason changed)"
    if bs != as_:
        return LOW, f"{bs} → {as_}"
    return None


def diff(golden: Dict[str, dict], current: Dict[str, dict]) -> DiffReport:
    rep = DiffReport(n_golden=len(golden), n_current=len(current))
    for key in sorted(set(golden) | set(current)):
        b, a = golden.get(key), current.get(key)
        res = _classify(b, a)
        if res is None:
            continue
        sev, detail = res
        kind = 'CHANGED' if (b is not None and a is not None) else ('NEW' if b is None else 'REMOVED')
        rep.changes.append(Change(key, kind, sev, b, a, detail))
    rep.changes.sort(key=lambda c: (_RANK[c.severity], c.key))
    return rep


def format_report(rep: DiffReport) -> str:
    lines = [f'DUAL-RUN DIFF   golden={rep.n_golden} rows   current={rep.n_current} rows   '
             f'changes={len(rep.changes)}']
    for sev in (CRITICAL, HIGH, MEDIUM, LOW):
        grp = rep.by_severity(sev)
        if not grp:
            continue
        loud = '  ◀── WRONG-NUMBER DOOR' if sev == CRITICAL else ''
        lines.append('')
        lines.append(f'── {sev} ({len(grp)}) ──{loud}')
        for c in grp:
            lines.append(f'  [{c.kind:7}] {c.key}')
            lines.append(f'            {c.detail}')
    lines.append('')
    lines.append('VERDICT: ' + ('BLOCKED — CRITICAL/HIGH changes need explicit human accept'
                                 if rep.blocks else 'clean of blocking changes'))
    return '\n'.join(lines)


# ── golden persistence (dev-local, gitignored path) ─────────────────────────────────
def save_golden(path: str, sig: Dict[str, dict]) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, 'w') as fh:
        json.dump(sig, fh, indent=1, sort_keys=True)


def load_golden(path: str) -> Dict[str, dict]:
    if not os.path.isfile(path):
        return {}
    with open(path) as fh:
        return json.load(fh)


# ── real-corpus driver (production path; file-name-blind) ───────────────────────────
def run_corpus(in_dir: str, *, as_of: str = '2026-06-30', org: str = 'dualrun', rate_card=None):
    """Run the REAL pipeline over every .xlsx in in_dir (universal glob — no file names
    hardcoded). Returns the RunResult. Production path, never a reconstruction."""
    from .pipeline import run as pipeline_run
    from .ratecard import default_inr_card
    rc = rate_card or default_inr_card(as_of)
    files = [(os.path.splitext(f)[0], os.path.join(in_dir, f))
             for f in sorted(os.listdir(in_dir))
             if f.lower().endswith('.xlsx') and not f.startswith('~$')]
    return pipeline_run(files, as_of=as_of, org=org, rate_card=rc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Dual-run safety gate over the real corpus.')
    ap.add_argument('cmd', choices=['snapshot', 'check', 'accept'])
    ap.add_argument('--golden', default=_DEFAULT_GOLDEN)
    ap.add_argument('--in', dest='in_dir', default=_DEFAULT_IN)
    ap.add_argument('--as-of', default='2026-06-30')
    args = ap.parse_args(argv)

    if args.cmd == 'snapshot':
        sig = signature(run_corpus(args.in_dir, as_of=args.as_of))
        save_golden(args.golden, sig)
        print(f'snapshot: wrote {len(sig)} rows → {args.golden}')
        return 0

    golden = load_golden(args.golden)
    if not golden:
        print(f'no golden at {args.golden} — run `snapshot` first')
        return 2
    cur = signature(run_corpus(args.in_dir, as_of=args.as_of))
    rep = diff(golden, cur)
    print(format_report(rep))

    if args.cmd == 'accept':
        save_golden(args.golden, cur)
        print(f'\naccept: promoted current → golden ({len(cur)} rows) at {args.golden}')
        return 0
    return 1 if rep.blocks else 0


if __name__ == '__main__':
    raise SystemExit(main())
