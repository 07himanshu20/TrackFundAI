"""Fund-anchor merge robustness + upload-order invariance.

Regression home for the item-3 finding: a LATENT order-dependent EMIT (CPC cash HELD↔0.2229) caused by
the Valuations 'FV of holding' column being mis-read as `ownership_pct`=148%, first-seen-merged into the
whole-company anchor (cost÷ownership), flipping the figure-anchor guard. Two locks live here:

  (a) the ownership ∈ (0,1] UNIVERSAL INVARIANT — no entity owns >100% of another, so a normalised
      ownership >1 is always a misparse and is rejected;
  (b) upload order → BYTE-IDENTICAL workbook (canonical processing order), the item-3 reddening test.

This file is also the home for the scheduled merge-robustness follow-up (make EVERY first-seen attribute
— domicile first, as it is currency-critical — order-independent).
"""
import os
import glob
import tempfile
from decimal import Decimal

import pytest

from backend.dataimport.preingest3 import fund_anchor, lexicon
from backend.dataimport.preingest3.fund_anchor import (_ownership_frac, _resolve_domicile,
                                                       _resolve_ownership, build_fund_anchors)

IN = 'backend/media/preingest/trivesta/100e86d5/in'
_real = pytest.mark.skipif(not os.path.isdir(IN), reason='real fixture files not present')
INV = os.path.join(IN, 'TFAI_Investments_and_Deployment.xlsx')
VAL = os.path.join(IN, 'TFAI_Valuations_and_Exits_Q2FY26.xlsx')


# ── (a) the ownership ∈ (0,1] universal invariant ────────────────────────────
@pytest.mark.parametrize('raw, expect', [
    (0.18, 0.18), (18, 0.18), ('18%', 0.18), (26, 0.26), (0.26, 0.26),
    (100, 1.0), (1.0, 1.0),               # 100% ownership is valid (a buyout)
    (148, None), (105, None),             # >100% after normalisation → impossible → rejected
    (0, None), (-5, None),                # non-positive → rejected
])
def test_ownership_frac_enforces_0_1_invariant(raw, expect):
    got = _ownership_frac(raw)
    if expect is None:
        assert got is None
    else:
        assert got is not None and abs(got - expect) < 1e-9


def test_ownership_frac_reddening_the_148pct_misparse():
    # The exact bug: an 'FV of holding' value (148) mis-read as ownership must NOT become a 148% stake.
    # Pre-fix this returned 1.48; the invariant rejects it so the whole-company anchor stays sane.
    assert _ownership_frac(148) is None


# ── the anchor merge is order-independent on the real files ─────────────────
# The completion signal for the (c)+(d) fund-anchor-merge robustness pass. It FAILED until (c)+(d)
# landed: (c) the Valuations 'FV of holding' column was mis-read as ownership for companies whose FV≤100
# (a plausible ≤1 stake the (a) invariant cannot reject), and (d) the first-seen merge then picked it or
# the Investments value depending on file order. (c) [%-preservation kills the false-match] + (d)
# [collect-then-resolve merge: every attribute resolved order-independently] fixed it. Now GREEN.
@_real
def test_build_fund_anchors_is_order_independent():
    a = {k: (v.ownership_frac, str(v.anchor_cr)) for k, v in build_fund_anchors([INV, VAL]).items()}
    b = {k: (v.ownership_frac, str(v.anchor_cr)) for k, v in build_fund_anchors([VAL, INV]).items()}
    diffs = [(k, a[k], b.get(k)) for k in a if a[k] != b.get(k)]
    assert not diffs, f'fund-anchor merge is order-dependent: {diffs[:5]}'


# ── (c) the %-preservation root fix: the ownership matcher no longer eats a non-% column ──
@pytest.mark.parametrize('header, expect', [
    ('FV of holding', 'none'),          # THE bug: 'holding %'→'holding' used to whole-word-match this
    ('Fair Value of Holding', 'none'),
    ('Cost of holding', 'none'),
    ('Equity', 'none'),                 # bare 'Equity' used to get a FALSE *exact* match (equity %→equity)
    ('Equity Shares', 'none'),
    ('Holding %', 'exact'),             # a legitimate ownership header MUST still match
    ('Ownership %', 'exact'),
    ('Equity %', 'exact'),
    ('ownership', 'exact'),
    ('shareholding', 'exact'),
])
def test_ownership_matcher_requires_the_percent_signal(header, expect):
    # reddens if normalise_label ever stops preserving '%' (the collapse returns and the false-match re-arms)
    assert lexicon.match_strength(header, 'ownership_pct') == expect


def test_fv_of_holding_is_still_fair_value_after_the_fix():
    # the fix removes 'FV of holding' from OWNERSHIP only — it must remain a fair-value column
    assert lexicon.match_strength('FV of holding', 'fair_value') == 'contains'
    assert lexicon.match_strength('IRR%(Gross)', 'irr') != 'none'   # irr column still locatable


# ── (d) deterministic, order-independent, fail-closed resolvers (negative controls) ──
def test_resolve_ownership_prefers_the_cost_bearing_schedule_on_disagreement():
    # disagreement → prefer the deployment (cost-bearing) schedule's stake, never first-seen
    assert _resolve_ownership([(0.30, False), (0.22, True)]) == 0.22
    assert _resolve_ownership([(0.22, True), (0.30, False)]) == 0.22   # order-independent
    assert _resolve_ownership([(0.30, False), (0.22, False)]) == 0.22  # no cost sheet → deterministic min
    assert _resolve_ownership([(0.26, True)]) == 0.26
    assert _resolve_ownership([]) is None


def test_resolve_domicile_holds_only_on_a_real_currency_conflict():
    # different currencies → fail-closed (None, conflict=True) → routes to U6 currency hold
    assert _resolve_domicile(['India', 'Malaysia']) == (None, True)
    assert _resolve_domicile(['Malaysia', 'India']) == (None, True)      # order-independent
    # same currency (spelling variant) → NOT a conflict (no over-hold), deterministic pick
    dom, conflict = _resolve_domicile(['Bangalore, India', 'India'])
    assert conflict is False and dom is not None
    assert _resolve_domicile(['India', 'India']) == ('India', False)
    assert _resolve_domicile([]) == (None, False)


@_real
def test_cpc_anchor_is_the_value_audited_307_both_orders():
    # value-audited: CPC ownership 26% (Portfolio!H13), cost 80 → whole-company anchor 80/0.26 = 307.69.
    # Pre-fix, VAL-first order gave ownership 1.48 → bare-cost anchor 80, wrongly UN-holding CPC cash.
    for paths in ([INV, VAL], [VAL, INV]):
        ca = next(c for k, c in build_fund_anchors(paths).items() if 'CPC' in (c.company or ''))
        assert ca.ownership_frac == 0.26
        assert abs(ca.anchor_cr - Decimal('307.6923076923076923076923077')) < Decimal('0.01')


# ── (b) THE item-3 reddening lock: shuffled upload order → byte-identical workbook ──
@pytest.mark.slow
@_real
def test_pipeline_output_is_upload_order_invariant_byte_identical():
    from backend.dataimport.preingest3 import pipeline, master_workbook as mw
    from backend.dataimport.preingest3.ratecard import default_inr_card
    files = [(os.path.basename(p), p) for p in sorted(glob.glob(os.path.join(IN, '*.xlsx')))]
    manifest = sorted(f for f, _ in files)            # keep the file-list header order-independent too

    def workbook_bytes(order):
        res = pipeline.run(list(order), as_of='2026-06-30', org='det',
                           rate_card=default_inr_card('2026-06-30'))
        wb = mw.build_master(res.cir, rate_card=default_inr_card('2026-06-30'), files=manifest)
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=True) as tf:
            mw.save_reproducible(wb, tf.name)
            with open(tf.name, 'rb') as fh:
                return fh.read()

    forward = workbook_bytes(files)
    reversed_ = workbook_bytes(list(reversed(files)))
    assert forward == reversed_, ('workbook differs under shuffled upload order — canonical order or the '
                                  'anchor merge is not order-independent (determinism lock over a moving value)')
