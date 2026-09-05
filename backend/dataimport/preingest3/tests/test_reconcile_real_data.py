"""REAL-DATA validation of reconcile.reconcile_locations (A′ step 2) — the finder's reconciler proven
on genuine located values BEFORE the finder depends on it, so a later finder bug stays isolated from a
primitive bug. Locations are built the SAME way the finder must (production alt-gathering + anchor
normalization: _alt_stock_sources → anchor_plausible_crs), NOT synthetic.

Corpus coverage (measured): the real value-level dispositions present are AGREE (Agnikul cash: 4
independent sheets, anchor-repaired, all ≈₹117.66 Cr), SINGLE (LDC/InstaAstro cash: one carrier), and
— by REDDENING on real magnitudes — CONFLICT (swap one real source to a different real cash value →
must HOLD). MULTISCOPE does NOT occur at the VALUE level in this corpus: the multi-division situations
(CPM/CSS) are SHEET-SELECTION, owned by reconcile.sigma_consolidated_pick; the value-level multiscope
branch is covered by the synthetic reddening tests in test_reconcile_locations.py. This is a measured
gap, not an omission — no real value-level multiscope was fabricated.

Slow: extracts a few MIS files. Skipped if the fixtures are absent."""
import os
from decimal import Decimal

import pytest

from backend.dataimport.preingest3 import fund_anchor, units, reconcile
from backend.dataimport.preingest3.extract import (
    profile_file, _best_sheet, MIS_CONCEPTS, _alt_stock_sources, _workbook_mentions_inr, extract_company)
from backend.dataimport.preingest3.ratecard import default_inr_card
from backend.dataimport.preingest3.cir import Figure

IN = 'backend/media/preingest/trivesta/100e86d5/in'
pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(not os.path.isdir(IN), reason='real fixture files not present')]

FUND = ['TFAI_Investments_and_Deployment.xlsx', 'TFAI_Valuations_and_Exits_Q2FY26.xlsx']
AGNIKUL = 'AVF_2026_03_12_P_Agnikul_MIS_Feb26.xlsx'
LDC = 'AVF_2026_03_20_P_LDC_Detailed_MIS_Feb26.xlsx'
INSTA = 'AVF_2026_03_18_P_InstaAstro_MIS_Feb_2026.xlsx'
_CARD = default_inr_card('2026-02-28')


def _ca(token):
    anchors = fund_anchor.build_fund_anchors([os.path.join(IN, f) for f in FUND])
    return next((c for k, c in anchors.items() if token in k.lower()), None)


def _cash_locations(name, fn, token):
    """Real cash locations for `name`, normalized to a single anchor-plausible ₹Cr per source — the
    exact caller-normalization the finder must perform before calling reconcile_locations."""
    ca = _ca(token)
    path = os.path.join(IN, fn)
    prof = profile_file(name, path)
    _n, _c, sheet, rows, ax, lc, found = _best_sheet(prof, MIS_CONCEPTS)
    geo = units.expected_currency(ca.domicile if ca else None)
    inr_m = _workbook_mentions_inr(prof)
    rec = extract_company(name, path, rate_card=_CARD, entity=name,
                          domicile=(ca.domicile if ca else None),
                          anchor_cr=(ca.anchor_cr if ca else None), use_model=False)
    figs = {v.concept: v for v in rec.fields.values() if isinstance(v, Figure)}
    locs = []
    cashf = figs.get('cash')
    if isinstance(cashf, Figure) and cashf.confirmed:
        locs.append({'sheet': sheet, 'cell': cashf.provenance.cell or 'emit',
                     'value_cr': cashf.value_cr, 'scope': None})
    for lbl, _decl, scale_crs in _alt_stock_sources(prof, 'cash', sheet, _CARD, geo, inr_m):
        pl = reconcile.anchor_plausible_crs(scale_crs, ca.anchor_cr if ca else None)
        distinct = sorted({str(v) for v in pl.values()})
        if len(distinct) == 1:                          # unambiguous anchor-plausible reading → a clean location
            locs.append({'sheet': lbl, 'cell': '', 'value_cr': Decimal(distinct[0]), 'scope': None})
    return locs


def test_real_agree_agnikul_cash_four_sources():
    locs = _cash_locations('Agnikul', AGNIKUL, 'agnikul')
    assert len(locs) >= 3, f'expected a multi-source real cash agree case, got {locs}'
    r = reconcile.reconcile_locations('agnikul_cash', 'cash', locs)
    assert r['disposition'] == 'agree' and r['cross_checked'] is True
    assert abs(r['value_cr'] - Decimal('117.6646')) < Decimal('0.01')
    assert r['n_sources'] == len(locs)


@pytest.mark.parametrize('name,fn,token,val', [
    ('LDC', LDC, 'ldc', '241.9993'),
    ('InstaAstro', INSTA, 'instaastro', '17.7422'),
])
def test_real_single_source_cash(name, fn, token, val):
    locs = _cash_locations(name, fn, token)
    assert len(locs) == 1, f'{name}: expected single-source real cash, got {locs}'
    r = reconcile.reconcile_locations(f'{name}_cash', 'cash', locs)
    assert r['disposition'] == 'single' and r['cross_checked'] is False
    assert abs(r['value_cr'] - Decimal(val)) < Decimal('0.01')


def test_real_conflict_reddening_agnikul_cash():
    # REDDENING on REAL magnitudes: Agnikul's independent cash sources genuinely agree (≈₹117.66 Cr);
    # swap ONE to a different REAL cash value (LDC's ₹241.9993 Cr) at the same (unlabelled) scope →
    # the reconciler MUST hold (conflict), never silently pick one. Proves the hold is load-bearing
    # on real data, not just on the synthetic fixtures.
    locs = _cash_locations('Agnikul', AGNIKUL, 'agnikul')
    assert len(locs) >= 2
    locs[1] = {**locs[1], 'value_cr': Decimal('241.9993')}
    r = reconcile.reconcile_locations('agnikul_cash_perturbed', 'cash', locs)
    assert r['disposition'] == 'conflict' and r['value_cr'] is None
    assert r['value_cr'] not in (Decimal('117.6646'), Decimal('241.9993'))
