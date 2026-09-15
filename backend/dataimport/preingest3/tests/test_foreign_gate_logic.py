"""Pure decision logic for the rate-supplied foreign coverage gate + its FAST reddening controls.

The gate's two checks live HERE (django-free, unit-testable without the corpus); the slow corpus gate
(test_foreign_coverage_gate) imports and drives them over the REAL pipeline output. So what production
is gated on is exactly what these controls prove.

WHY separate: the corpus gate is slow + skip-if-fixtures-absent, so on its own it could go UNSEEN or
only-ever-green — which the project forbids (a fail-closed gate must be proven to REDDEN against the
bug it targets). These synthetic controls make that machine-checked, deterministic, and cheap. The
same transitions were also MEASURED on the real corpus this session (domicile dropped → CSS cash HELD
→ lock reddens; MYR card without Analisa GT → Analisa emits un-GT'd → discipline reddens).
"""
from decimal import Decimal
from types import SimpleNamespace as NS

from django.test import SimpleTestCase

TOL = Decimal('0.01')


def lock_failures(figs: dict, gt: dict, tol: Decimal = TOL) -> list:
    """Ground-truthed slots that DON'T emit==GT (held/absent/gap/value-shift). Empty ⇒ lock holds.
    figs: {(company, concept): Figure-like with .confirmed/.held/.value_cr/.provenance}."""
    out = []
    for (company, concept), g in sorted(gt.items()):
        f = figs.get((company, concept))
        if f is None or not f.confirmed or f.value_cr is None:
            state = 'absent' if f is None else ('HELD' if f.held else 'GAP')
            out.append(f'  {company}/{concept}: expected EMIT {g}, got {state}')
        elif abs(f.value_cr - Decimal(g)) >= tol:
            out.append(f'  {company}/{concept}: expected {g}, got {f.value_cr} '
                       f'@ {f.provenance.sheet}!{f.provenance.cell}')
    return out


def leaked_emits(figs: dict, gt: dict, allow: dict) -> list:
    """Foreign money EMITs that are neither ground-truthed nor allowlisted. Empty ⇒ discipline holds."""
    out = []
    for (company, concept), f in sorted(figs.items()):
        if not (f.confirmed and f.value_cr is not None):
            continue
        if (company, concept) in gt or (company, concept) in allow:
            continue
        out.append(f'  {company}/{concept} = {f.value_cr} @ {f.provenance.sheet}!{f.provenance.cell}')
    return out


# ── fast reddening controls ─────────────────────────────────────────────────────────
_GT = {('X', 'cash'): '17.4248'}


def _fig(value, *, held=False, sheet='S', cell='1', concept='cash'):
    v = None if value is None else Decimal(str(value))
    return NS(concept=concept, value_cr=v, confirmed=(v is not None and not held),
              held=held, gap=False, provenance=NS(sheet=sheet, cell=cell))


class LockReddens(SimpleTestCase):
    def test_reddens_when_a_gt_slot_holds(self):
        # the exact real regression: domicile drops → CSS cash HELD → lock must FAIL
        self.assertTrue(lock_failures({('X', 'cash'): _fig(None, held=True)}, _GT))

    def test_reddens_when_value_shifts(self):
        # e.g. the stale-January mis-bind (24.17) instead of the May value
        self.assertTrue(lock_failures({('X', 'cash'): _fig('24.17')}, _GT))

    def test_reddens_when_slot_absent(self):
        self.assertTrue(lock_failures({}, _GT))

    def test_green_on_exact_match(self):
        self.assertFalse(lock_failures({('X', 'cash'): _fig('17.4248')}, _GT))

    def test_tolerance_admits_sub_paisa_noise_only(self):
        self.assertFalse(lock_failures({('X', 'cash'): _fig('17.4249')}, _GT))  # <0.01 → fine
        self.assertTrue(lock_failures({('X', 'cash'): _fig('17.44')}, _GT))     # ≥0.01 → reddens


class DisciplineReddens(SimpleTestCase):
    def test_reddens_on_ungrounded_foreign_emit(self):
        # the exact MYR case: a foreign entity emits with no GT and no allowlist → must FAIL
        self.assertTrue(leaked_emits({('Analisa', 'revenue'): _fig('8.6368', concept='revenue')}, _GT, {}))

    def test_green_when_emit_is_gt(self):
        self.assertFalse(leaked_emits({('X', 'cash'): _fig('17.4248')}, _GT, {}))

    def test_green_when_emit_is_allowlisted(self):
        allow = {('Analisa', 'revenue'): {'reason': 'no GT exists', 'review_by': '2099-01-01'}}
        self.assertFalse(leaked_emits({('Analisa', 'revenue'): _fig('8.6368', concept='revenue')}, _GT, allow))

    def test_green_when_foreign_cell_is_held(self):
        # held/uncovered foreign money is SAFE — never flagged as an unverified emit
        self.assertFalse(leaked_emits({('Analisa', 'revenue'): _fig(None, held=True, concept='revenue')}, _GT, {}))
