"""UNIFIED cross-sheet N-location reconciler (reconcile.reconcile_locations) — the model-phase
finder prerequisite (step B, standalone). Proves the one contract: a concept read at N locations →
agree/emit | conflict/hold | multiscope/(Σ-declared-or-hold) | single/disclose | none.

Every HOLD disposition ships with a reddening control: the test asserts the reconciler emits NOTHING
(value_cr is None) in the conflict / no-Σ-satisfier cases — never silently picks one of the disagreeing
values — and that it DOES emit only in the agree / unique-Σ cases. A pure fn has no production gate to
monkeypatch, so the negative control is the explicit 'it holds, and holds to None' assertion."""
from decimal import Decimal

from backend.dataimport.preingest3 import reconcile


def _loc(sheet, cell, value_cr, scope=None, kind=None):
    return {'sheet': sheet, 'cell': cell,
            'value_cr': None if value_cr is None else Decimal(str(value_cr)),
            'scope': scope, 'kind': kind}


# ── agree ────────────────────────────────────────────────────────────────────────────────────────
def test_agree_all_sources_emit_corroborated():
    locs = [_loc('A', 'F8', '5.3000'), _loc('B', 'P15', '5.3005'), _loc('C', 'D4', '5.2995')]
    r = reconcile.reconcile_locations('rev', 'revenue', locs)
    assert r['disposition'] == 'agree' and r['cross_checked'] is True
    assert r['value_cr'] == Decimal('5.3000') and r['n_sources'] == 3


def test_agree_is_order_independent_deterministic():
    a = [_loc('A', 'F8', '5.3000'), _loc('B', 'P15', '5.3005')]
    r1 = reconcile.reconcile_locations('rev', 'revenue', a)
    r2 = reconcile.reconcile_locations('rev', 'revenue', list(reversed(a)))
    assert r1['value_cr'] == r2['value_cr'] == Decimal('5.3000')      # (sheet,cell) sort → deterministic


def test_scale_aware_tolerance_boundary():
    # inside the 0.5% band → agree; beyond it (same scope) → conflict/hold.
    inside = reconcile.reconcile_locations('c', 'cash', [_loc('A', '1', '100.0'), _loc('B', '2', '100.4')])
    assert inside['disposition'] == 'agree'
    outside = reconcile.reconcile_locations('c', 'cash', [_loc('A', '1', '100.0', scope='s'),
                                                          _loc('B', '2', '100.6', scope='s')])
    assert outside['disposition'] == 'conflict' and outside['value_cr'] is None


# ── conflict (REDDENING: must HOLD, never pick one of the disagreeing values) ──────────────────────
def test_conflict_same_scope_holds_to_none():
    locs = [_loc('PL', 'F8', '5.30', scope='consolidated'),
            _loc('TB', 'B19', '7.10', scope='consolidated')]
    r = reconcile.reconcile_locations('rev', 'revenue', locs)
    assert r['disposition'] == 'conflict'
    assert r['value_cr'] is None                                       # never ships an unreconciled number
    assert r['value_cr'] not in (Decimal('5.30'), Decimal('7.10'))     # and never silently picks one


def test_conflict_unlabelled_scope_holds():
    # scope=None on both (can't prove they're different scopes) → they SHOULD agree → conflict, held.
    locs = [_loc('A', '1', '10'), _loc('B', '2', '25')]
    r = reconcile.reconcile_locations('rev', 'revenue', locs)
    assert r['disposition'] == 'conflict' and r['value_cr'] is None


# ── multiscope ────────────────────────────────────────────────────────────────────────────────────
def test_multiscope_resolves_to_sigma_consolidated():
    # 3 divisions + a consolidated (R(C) ≈ Σ divisions) across DISTINCT scopes → pick the Σ satisfier.
    locs = [_loc('D1', '1', '30', scope='div1'), _loc('D2', '1', '40', scope='div2'),
            _loc('D3', '1', '30', scope='div3'), _loc('CONS', '1', '100', scope='consolidated')]
    r = reconcile.reconcile_locations('rev', 'revenue', locs)
    assert r['disposition'] == 'multiscope'
    assert r['value_cr'] == Decimal('100')                            # the consolidated, not a division
    assert r['value_cr'] not in (Decimal('30'), Decimal('40'))


def test_multiscope_two_block_holds_no_unique_satisfier():
    # division vs consolidated, only 2 blocks → Σ needs ≥3 siblings → no unique satisfier → HOLD.
    locs = [_loc('DIV', '1', '30', scope='division'), _loc('CONS', '1', '100', scope='consolidated')]
    r = reconcile.reconcile_locations('rev', 'revenue', locs)
    assert r['disposition'] == 'multiscope' and r['value_cr'] is None


# ── single / none ────────────────────────────────────────────────────────────────────────────────
def test_single_source_discloses_uncrosschecked():
    r = reconcile.reconcile_locations('c', 'cash', [_loc('Analysis', 'B19', '9.3253')])
    assert r['disposition'] == 'single'
    assert r['value_cr'] == Decimal('9.3253') and r['cross_checked'] is False


def test_none_when_no_value_present():
    r = reconcile.reconcile_locations('rev', 'revenue', [_loc('A', '1', None), _loc('B', '2', None)])
    assert r['disposition'] == 'none' and r['value_cr'] is None
