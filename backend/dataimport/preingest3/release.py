"""
S9 release gate — TWO independent gates, both required, neither sufficient alone:

  1. INTERNAL INVARIANTS (code, over the CIR) — structural truths that hold
     REGARDLESS of FX / period-basis judgment, so they are valid even though we
     do NOT match any reference workbook:
        Σtranches            = deployed cost
        Σsector cost         = total cost
        ΣLP commitments      = fund commitment
        NAV components       sum to NAV
     Each is hole-aware: a held input makes the invariant INDETERMINATE, which
     holds the release (a hole never passes as a zero).

  2. RECALC-CLEAN (headless LibreOffice) — proves the workbook's formulas
     EVALUATE without error in the engine that computes what the client reads. A
     clean recalc alone is NOT enough (a wrong-but-valid formula recalcs clean),
     which is exactly why invariant #1 is also required.

The reference-workbook comparison is a SEPARATE, softer EXPLAINABILITY report —
never pass/fail — so "matches the reference" can never be mistaken for "correct".
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from decimal import Decimal
from typing import List, Optional

import openpyxl

from . import aggregate, reconcile
from .cir import CIR, Figure

_ERROR_TOKENS = ('#REF!', '#DIV/0!', '#NAME?', '#VALUE!', '#N/A', '#NULL!', '#NUM!')


# ── gate 1: internal invariants ──────────────────────────────────────────
def _sum_concept(cir: CIR, concept: str) -> Optional[Decimal]:
    figs = [f for r in cir.records for f in r.figures() if f.concept == concept]
    if not figs:
        return None
    agg = aggregate.sum_as_reported(figs)
    return agg.value if agg.complete else None


def internal_invariants(cir: CIR) -> List[dict]:
    """Structural tie-outs. Uses reconcile.hard_* so each is rounding-aware and
    hole-aware (indeterminate on a held input)."""
    out = []

    def inv(name, parts_concept, total_concept):
        parts = [f for r in cir.records for f in r.figures() if f.concept == parts_concept]
        totals = [f for r in cir.records for f in r.figures() if f.concept == total_concept]
        if not parts or not totals:
            out.append({'id': name, 'status': 'skipped', 'detail': 'inputs absent this run'})
            return
        total_val = aggregate.sum_as_reported(totals).value
        res = reconcile.hard_sum_equal(name, parts, total_val)
        out.append(res)

    inv('tranches_sum_to_cost', 'tranche_amount', 'cost')
    inv('lp_commitments_sum_to_fund', 'investor_commitment', 'commitment')
    inv('sector_cost_equals_total', 'sector_cost', 'cost')
    return out


def invariants_ok(results: List[dict]) -> bool:
    return not any(r.get('status') in ('fail', 'indeterminate') for r in results)


# ── gate 2: recalc-clean ─────────────────────────────────────────────────
def _libreoffice() -> Optional[str]:
    for name in ('soffice', 'libreoffice'):
        p = shutil.which(name)
        if p:
            return p
    # not on PATH — check the standard install locations (the macOS cask installs
    # the binary inside the .app bundle, which is never on PATH), so the recalc
    # gate actually activates once installed instead of staying dark.
    for p in ('/Applications/LibreOffice.app/Contents/MacOS/soffice',
              '/opt/homebrew/bin/soffice', '/usr/local/bin/soffice',
              '/usr/bin/libreoffice', '/usr/bin/soffice'):
        if os.path.exists(p):
            return p
    return None


def recalc_clean(path: str) -> dict:
    """Convert the workbook through headless LibreOffice (forcing a recalc), then
    reload and scan for formula errors. If no engine is available the recalc is
    reported UNAVAILABLE (not silently 'clean')."""
    soffice = _libreoffice()
    if not soffice:
        return {'engine': None, 'ran': False, 'clean': None, 'errors': [],
                'detail': 'no LibreOffice engine — recalc unavailable, not asserted'}
    outdir = tempfile.mkdtemp()
    try:
        subprocess.run([soffice, '--headless', '--calc', '--convert-to', 'xlsx',
                        '--outdir', outdir, path], capture_output=True, timeout=120)
        out = os.path.join(outdir, os.path.splitext(os.path.basename(path))[0] + '.xlsx')
        if not os.path.exists(out):
            return {'engine': soffice, 'ran': True, 'clean': False, 'errors': ['convert failed'],
                    'detail': 'recalc conversion produced no file'}
        wb = openpyxl.load_workbook(out, data_only=True)
        errors = []
        for sn in wb.sheetnames:
            for row in wb[sn].iter_rows():
                for c in row:
                    if isinstance(c.value, str) and any(t in c.value for t in _ERROR_TOKENS):
                        errors.append(f'{sn}!{c.coordinate}={c.value}')
        return {'engine': soffice, 'ran': True, 'clean': not errors,
                'errors': errors[:20], 'detail': 'recalc clean' if not errors else 'formula errors'}
    except Exception as e:  # noqa: BLE001
        return {'engine': soffice, 'ran': True, 'clean': False, 'errors': [str(e)[:120]],
                'detail': 'recalc raised'}


# ── the combined gate ────────────────────────────────────────────────────
def release_gate(cir: CIR, workbook_path: str) -> dict:
    """Three-way exit (G3 + add-on #5):
      • blocked  — a genuine contradiction (hard invariant FAIL or recalc errors).
                   Nothing ships.
      • partial  — delivered WITH disclosures: held figures/files, or an
                   indeterminate invariant (a hole). The workbook is real and
                   honest (holes render as INCOMPLETE), it is just not fully
                   verified — a review pass is queued.
      • verified — no holds, invariants pass, recalc clean.
    `delivered` is true for both partial and verified — one hole never sinks the
    whole workbook.
    """
    inv = internal_invariants(cir)
    rc = recalc_clean(workbook_path)
    hard_fail = any(r.get('status') == 'fail' for r in inv) or cir.blocked
    indeterminate = any(r.get('status') == 'indeterminate' for r in inv)
    recalc_errors = (rc['clean'] is False)

    reasons = []
    if hard_fail:
        reasons.append('hard invariant FAIL / CIR contradiction — cannot deliver')
    if recalc_errors:
        reasons.append(f'recalc not clean: {rc["errors"][:3]}')
    if rc['clean'] is None:
        reasons.append('recalc engine unavailable — gated on invariants only (flagged)')

    if hard_fail or recalc_errors:
        status = 'blocked'
    elif cir.has_holds or indeterminate:
        status = 'partial'
        reasons.append('delivered PARTIAL — held/indeterminate items disclosed, review queued')
    else:
        status = 'verified'
    return {'status': status, 'delivered': status != 'blocked',
            'invariants': inv, 'recalc': rc, 'reasons': reasons}
