"""ANCHOR-GATED, scale-AWARE cross-sheet STOCK corroboration (Increment 3a) — pure, synthetic, fast.

Locks two real-data findings (advisor 2026-07-27):
  (1) the DECLARED unit lies (Agnikul BS says '₹ Millions' over absolute rupees) — the anchor's
      plausible decade REPAIRS it, so the alt corroborates scale-aware after repair.
  (2) a power-of-ten ratio is AMBIGUOUS between a lying unit and a genuinely-different value a clean
      10^k apart (division 11.7 vs consolidated 117). Scale-BLIND up-to-scale would FALSE-CONFIRM (2);
      the anchor-gated, scale-aware check HOLDS it. THE ADVERSARIAL TEST below is the gate the advisor
      required before wiring. No anchor → power-of-ten is an ambiguous disclosure, never a cross-check.

Scale legend for the {scale: cr} dicts: a native value N in ₹Cr under each candidate scale is
N×(scale/10^7) — 'absolute'=N/1e7, 'thousands'=N/1e4, 'millions'=N×0.1, etc. Tests pass the ₹Cr
directly for clarity.
"""
from decimal import Decimal

from backend.dataimport.preingest3 import reconcile
from backend.dataimport.preingest3.cir import Figure


def _fig(v):
    return Figure('cash', Decimal(str(v)), None, None)          # a confirmed ₹Cr figure


# ── primitives ───────────────────────────────────────────────────────────────
def test_scale_aware_agree_holds_a_genuine_ten_x_gap():
    assert reconcile.scale_aware_agree(Decimal('117'), Decimal('117.4')) is True     # rounding
    assert reconcile.scale_aware_agree(Decimal('117'), Decimal('11.7')) is False     # a real 10× gap
    assert reconcile.scale_aware_agree(Decimal('117'), Decimal('117000000')) is False  # the 10^6 lie, un-repaired


def test_agree_up_to_scale_is_only_a_mantissa_label_now():
    # same significant digits at different decade — TRUE, but this is NOT corroboration anymore.
    assert reconcile.agree_up_to_scale(Decimal('117.66'), Decimal('117664622.99')) is True
    assert reconcile.agree_up_to_scale(Decimal('117.66'), Decimal('143.2')) is False


def test_anchor_plausible_crs_repairs_the_lying_unit():
    # Agnikul BS cash under each scale; anchor 464 Cr. Only 'absolute' (117.66) is within 1.5 decades.
    crs = {'absolute': Decimal('117.66'), 'thousands': Decimal('117660'),
           'millions': Decimal('117660000'), 'lakhs': Decimal('11766000'), 'crore': Decimal('1176600000')}
    plausible = reconcile.anchor_plausible_crs(crs, Decimal('464'))
    assert list(plausible) == ['absolute']


# ── the Agnikul lying-unit case: repair → corroborate scale-aware ─────────────
def test_lying_unit_is_repaired_by_anchor_and_corroborates():
    # emit 117.66 (from honest Analysis). BS declares 'millions' but only 'absolute' is anchor-plausible.
    bs = ('Balance Sheet', 'millions',
          {'absolute': Decimal('117.66'), 'millions': Decimal('117660000')})
    r = reconcile.stock_corroboration('cash', _fig('117.66'), [bs], anchor_cr=Decimal('464'))
    assert r['class'] == reconcile.SOFT and r['status'] == reconcile.PASS
    assert r['cross_checked'] is True
    assert r['corroborated'] == [{'sheet': 'Balance Sheet', 'scales': ['absolute']}]


# ── THE ADVERSARIAL GATE: genuinely-different values a clean 10× apart ────────
def test_adversarial_division_vs_consolidated_is_NOT_corroborated():
    # emit = CONSOLIDATED cash 1170 Cr. Alt = a DIVISION whose real, anchor-plausible cash is 117 Cr —
    # a genuinely different, smaller number that is EXACTLY 10× down (identical mantissa). Both sit
    # inside the anchor's plausible decade (464 Cr band ≈ [14.7, 14666] Cr). Scale-blind up-to-scale
    # would CORROBORATE them (false-confirm — the hole). Anchor-gated scale-aware MUST HOLD.
    division = ('Division AN', 'absolute',
                {'absolute': Decimal('117'), 'thousands': Decimal('117000')})
    r = reconcile.stock_corroboration('cash', _fig('1170'), [division], anchor_cr=Decimal('464'))
    assert r['class'] == reconcile.HARD and r['status'] == reconcile.FAIL
    assert reconcile.blocks_run([r]) is True                    # load-bearing: the emit HOLDS
    assert r['divergences'] == [{'sheet': 'Division AN', 'plausible_cr': ['117']}]
    # the scale-blind primitive would have (wrongly) called them equal — the exact hole we closed:
    assert reconcile.agree_up_to_scale(Decimal('1170'), Decimal('117')) is True


# ── no-anchor: power-of-ten is AMBIGUOUS, never a cross-check ─────────────────
def test_anchor_cannot_disambiguate_falls_to_ambiguous():
    # ADVISOR POINT 2 (the over-reach guard, symmetric to the false-confirm): a mid-range value where
    # BOTH the lakhs (300 Cr) and millions (3000 Cr) readings sit inside the anchor's ±1.5-decade band
    # (anchor 464 → [14.7, 14666] Cr). The anchor CANNOT pick the decade, so even though the emit (300)
    # matches the lakhs reading, corroboration must DECLINE → ambiguous, never a confident cross-check
    # (picking a decade it can't justify is exactly the confident-when-it-shouldn't failure mode).
    alt = ('Some Sheet', 'lakhs',
           {'absolute': Decimal('0.003'), 'thousands': Decimal('3'), 'lakhs': Decimal('300'),
            'millions': Decimal('3000'), 'crore': Decimal('30000')})
    plausible = reconcile.anchor_plausible_crs(alt[2], Decimal('464'))
    assert set(plausible) == {'lakhs', 'millions'}                    # two decades both in-band
    r = reconcile.stock_corroboration('cash', _fig('300'), [alt], anchor_cr=Decimal('464'))
    assert r['class'] == reconcile.DISCLOSURE and r['status'] == 'scale_unresolved'
    assert r['cross_checked'] is False
    assert r['ambiguous'][0]['why'] == 'anchor cannot disambiguate scale'
    assert reconcile.blocks_run([r]) is False                        # declines, never holds on ambiguity


def test_no_anchor_power_of_ten_is_ambiguous_disclosure_not_cross_checked():
    alt = ('Balance Sheet', 'millions',
           {'millions': Decimal('117660000'), 'absolute': Decimal('117.66')})
    r = reconcile.stock_corroboration('cash', _fig('117.66'), [alt], anchor_cr=None)
    assert r['class'] == reconcile.DISCLOSURE and r['status'] == 'scale_unresolved'
    assert r['cross_checked'] is False


def test_no_anchor_same_declared_scale_corroborates_scale_aware():
    # both honest at the same declared scale → corroborate even without an anchor.
    alt = ('Cashflow', 'absolute', {'absolute': Decimal('117.7')})
    r = reconcile.stock_corroboration('cash', _fig('117.66'), [alt], anchor_cr=None)
    assert r['status'] == reconcile.PASS and r['cross_checked'] is True


# ── divergence, single-source, held emit ─────────────────────────────────────
def test_genuine_mantissa_divergence_holds_the_emit():
    alt = ('Balance Sheet', 'absolute', {'absolute': Decimal('143.2')})
    r = reconcile.stock_corroboration('cash', _fig('117.66'), [alt], anchor_cr=Decimal('464'))
    assert r['status'] == reconcile.FAIL and reconcile.blocks_run([r]) is True


def test_single_source_is_disclosed_not_counted_as_cross_checked():
    r = reconcile.stock_corroboration('cash', _fig('17.74'), [], anchor_cr=Decimal('50'))
    assert r['class'] == reconcile.DISCLOSURE and r['status'] == 'single_source'
    assert r['cross_checked'] is False and reconcile.blocks_run([r]) is False


def test_unanchorable_alt_is_ambiguous_not_a_false_corroboration():
    # an alt whose every scale is >1.5 decades from the anchor → cannot confirm nor deny → disclosed.
    alt = ('Weird Sheet', 'absolute', {'absolute': Decimal('0.0001'), 'thousands': Decimal('0.1')})
    r = reconcile.stock_corroboration('cash', _fig('117.66'), [alt], anchor_cr=Decimal('464'))
    assert r['class'] == reconcile.DISCLOSURE and r['status'] == 'scale_unresolved'
    assert r['cross_checked'] is False


def test_held_emit_is_indeterminate_never_a_false_corroboration():
    held = Figure('cash', None, None, None, held=True)
    r = reconcile.stock_corroboration('cash', held, [('BS', 'absolute', {'absolute': Decimal('117.66')})])
    assert r['status'] == reconcile.INDETERMINATE
