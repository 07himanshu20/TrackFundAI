"""Σ-divisions roll-up identity — the SHARED consolidated-selection verifier (Increment 3b).

Proves the mechanism reconcile.collapse_value_duplicates + reconcile.sigma_consolidated_pick
that extract._best_sheet (today) and the S4 model-locator (Step 6) both call. The pick is pure
arithmetic over supplied values: the UNIQUE tab whose revenue ≈ Σ(sibling revenues) is the
consolidated; 0 or ≥2 satisfiers → ABSTAIN (None), fail-closed. Naming never admits a candidate;
exact-value dedup (guardrail-1: ALL populated columns, non-zero) collapses a repeated sub-group.

The CPM real-file shape is pinned here in miniature: two tabs equal on the representative column
but differing on ONE column are NOT collapsed (guardrail-1 held) → the resolver correctly abstains.
That is the calibrated reason CPM stays a strict-xfail in test_real_files_routing until Increment 4.
"""
from decimal import Decimal

import pytest

from backend.dataimport.preingest3 import reconcile

D = Decimal


def _pick_from_vectors(vectors):
    """Mirror extract._sigma_consolidated_pick: dedup, latest-column representative, pick."""
    kept = reconcile.collapse_value_duplicates(vectors)
    values = {lab: vec[max(vec)] for lab, vec in kept.items()}
    return kept, reconcile.sigma_consolidated_pick(values)


def test_unique_consolidated_is_picked():
    # PL = HC + AN + OG + SV + LS exactly → PL is the sole satisfier of R(X) ≈ Σ(others).
    values = {'PL': D('154.7'), 'HC': D('106.9'), 'AN': D('25.5'),
              'OG': D('13.15'), 'SV': D('5.15'), 'LS': D('4.0')}
    res = reconcile.sigma_consolidated_pick(values)
    assert res['pick'] == 'PL' and res['satisfiers'] == ['PL']


def test_pick_is_value_based_not_name_based():
    # The roll-up is named 'ZZZ'; a division is named 'Consolidated'. Naming plays NO role —
    # only the arithmetic identity admits, so the true roll-up wins regardless of labels.
    values = {'Consolidated': D('25.5'), 'ZZZ': D('154.7'), 'B': D('106.9'),
              'C': D('13.15'), 'D': D('5.15'), 'E': D('4.0')}
    assert reconcile.sigma_consolidated_pick(values)['pick'] == 'ZZZ'


def test_exact_duplicate_subgroup_is_collapsed_then_unique():
    # A sub-group tab byte-identical to a division (equal on EVERY column) double-counts Σ(others);
    # value-dedup collapses it (sorted-min rep 'HC' kept), restoring the unique PL pick.
    vectors = {
        'PL':         {1: D('50'), 2: D('154.7')},
        'HC':         {1: D('96'), 2: D('106.9')},
        'Healthcare': {1: D('96'), 2: D('106.9')},   # identical on ALL columns → collapse into HC
        'AN':         {1: D('12'), 2: D('25.5')},
        'OG':         {1: D('6'),  2: D('13.15')},
        'SV':         {1: D('2'),  2: D('5.15')},
        'LS':         {1: D('1.5'), 2: D('4.0')},
    }
    kept, res = _pick_from_vectors(vectors)
    assert 'Healthcare' not in kept and 'HC' in kept      # sorted-min representative kept
    assert res['pick'] == 'PL'


def test_guardrail1_one_differing_column_blocks_collapse_cpm_shape():
    # THE CALIBRATED CPM SHAPE (Healthcare vs HC): equal on the representative column but differing
    # on ONE mixed-basis column (96 vs 6.3, a 15× gap). Guardrail-1 (identical on ALL populated
    # columns) CORRECTLY declines to collapse them — relaxing it to 'all-but-one' would trade the
    # clean rule for a cosmetic flip. Healthcare double-counts HC → PL not unique → ABSTAIN. This
    # is exactly why CPM stays fail-closed on 'AN' until Increment 4 neutralises that column.
    vectors = {
        'PL':         {1: D('50'),  2: D('154.7')},
        'HC':         {1: D('96'),  2: D('106.9')},
        'Healthcare': {1: D('6.3'), 2: D('106.9')},   # differs on col 1 → NOT a duplicate
        'AN':         {1: D('12'),  2: D('25.5')},
        'OG':         {1: D('6'),   2: D('13.15')},
        'SV':         {1: D('2'),   2: D('5.15')},
        'LS':         {1: D('1.5'), 2: D('4.0')},
    }
    kept, res = _pick_from_vectors(vectors)
    assert 'Healthcare' in kept and 'HC' in kept          # both survive — guardrail-1 held
    assert res['pick'] is None                            # fail-closed abstain (double-count)


def test_all_zero_tabs_are_never_collapsed_and_are_inert():
    # All-zero tabs carry no identity evidence → never collapsed; they add 0 to Σ and cannot
    # satisfy → inert. The unique roll-up is still picked cleanly with them present.
    vectors = {
        'PL': {1: D('154.7')}, 'HC': {1: D('106.9')}, 'AN': {1: D('25.5')},
        'OG': {1: D('13.15')}, 'SV': {1: D('5.15')}, 'LS': {1: D('4.0')},
        'Z1': {1: D('0')}, 'Z2': {1: D('0')}, 'Z3': {1: D('0')},
    }
    kept, res = _pick_from_vectors(vectors)
    assert {'Z1', 'Z2', 'Z3'} <= set(kept)                # zeros not collapsed together
    assert res['pick'] == 'PL'


def test_all_zero_representative_period_abstains_cleanly():
    # A degenerate representative period (every candidate 0 — e.g. an empty latest month from a
    # dup-period column) is NO signal: 0≈Σ(0) is trivially true for ALL, which must NEVER read as
    # "everyone is the consolidated". Abstain cleanly. (Regression guard from the CPM D2 debug.)
    res = reconcile.sigma_consolidated_pick({'A': D('0'), 'B': D('0'), 'C': D('0')})
    assert res['pick'] is None and res['satisfiers'] == []


def test_no_rollup_present_abstains():
    # Divisions only, no consolidated tab → no candidate equals Σ(others) → 0 satisfiers → hold.
    res = reconcile.sigma_consolidated_pick({'A': D('100'), 'B': D('100'), 'C': D('100')})
    assert res['pick'] is None and res['satisfiers'] == []


def test_balanced_split_is_ambiguous_and_abstains():
    # Two tabs each equal to the other (C=0) → BOTH satisfy → non-unique → hold, never a coin-flip.
    res = reconcile.sigma_consolidated_pick({'A': D('100'), 'B': D('100'), 'C': D('0')})
    assert res['pick'] is None and set(res['satisfiers']) == {'A', 'B'}


def test_eliminations_band_admits_small_intercompany_then_rejects_large():
    # A consolidated may sit modestly below Σ(siblings) from inter-division eliminations. 5% below
    # is admitted (conservative UNCALIBRATED 15% band); 30% below is out of band → abstain.
    within = reconcile.sigma_consolidated_pick({'PL': D('95'), 'HC': D('60'), 'AN': D('40')})
    assert within['pick'] == 'PL'
    beyond = reconcile.sigma_consolidated_pick({'PL': D('70'), 'HC': D('60'), 'AN': D('40')})
    assert beyond['pick'] is None


def test_dedup_representative_is_sorted_min_deterministic():
    # Duplicate group {'Alpha','Zebra'} → the sorted-min 'Alpha' is the kept representative,
    # independent of insertion order (the determinism invariant on the new dedup site).
    vectors = {'Zebra': {1: D('100')}, 'Alpha': {1: D('100')},
               'PL': {1: D('154.7')}, 'B': {1: D('54.7')}}
    kept = reconcile.collapse_value_duplicates(vectors)
    assert 'Alpha' in kept and 'Zebra' not in kept


@pytest.mark.xfail(strict=True, reason=(
    "GUARDRAIL-2 residual (documented, not yet built): a MULTI-CHILD sub-group SG = L1+L2 of "
    "DISTINCT non-zero children is NOT an exact value-duplicate of any tab, so value-dedup cannot "
    "collapse it; SG double-counts L1+L2 in Σ(others) and the true consolidated T fails uniqueness "
    "→ resolver abstains. Resolving this needs sub-group DETECTION (collapse a tab equal to the sum "
    "of a subset of siblings), a later increment. When it lands this xpasses and the strict marker "
    "auto-alerts. Note: CPM's Healthcare/HC is NOT this case (it is exact-dup blocked by one "
    "mixed-basis column — Increment 4); this is the genuine distinct-children roll-up."))
def test_multichild_subgroup_selects_consolidated():
    # T = L1 + L2 + L3 = 30 + 25 + 45 = 100; SG = L1 + L2 = 55 (distinct multi-child roll-up).
    values = {'T': D('100'), 'L1': D('30'), 'L2': D('25'), 'L3': D('45'), 'SG': D('55')}
    assert reconcile.sigma_consolidated_pick(values)['pick'] == 'T'
