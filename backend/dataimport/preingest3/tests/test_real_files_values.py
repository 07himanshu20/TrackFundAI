"""VALUE-level guard on the real files — the check bijection/routing cannot give.

Bijection proves no company is claimed twice; routing proves no explosion. NEITHER
proves the emitted NUMBER is right — a record can sit under its own company and still
carry a wrong scale, wrong period, or the wrong metric line (EBIT read as EBITDA).
Those regression classes have bitten this project. So this asserts specific emitted
figures trace to the RIGHT cell(s) with the RIGHT value (₹Cr) and the RIGHT basis for
the companies that reliably emit on the deterministic path. Run on every change —
count-clean and bijection-clean are necessary, never sufficient.

CITE-EVIDENCE (advisor 2026-07-26): every assertion cites the cells that RECONSTRUCT
the value, via the universal provenance model:
  • point-in-time / single-total → provenance.cell is the VALUE cell, derived_from=[it].
  • collapsed FLOW (TTM/partial sum) → cell='' (no single value cell), derived_from is the
    summed range; basis carries the operation.
`_close` enforces that model structurally, so a value that cites a single cell but is
actually a 12-month sum (the misleading case) fails here. The universal reconstruction
check across ALL files lives in test_cite_evidence.py.

Fast: extracts a few company files directly. Skipped if the fixture files aren't present.
"""
import os
from decimal import Decimal

import pytest

from backend.dataimport.preingest3 import fund_anchor
from backend.dataimport.preingest3.extract import extract_company
from backend.dataimport.preingest3.ratecard import default_inr_card
from backend.dataimport.preingest3.cir import Figure

IN = 'backend/media/preingest/trivesta/100e86d5/in'
pytestmark = pytest.mark.skipif(not os.path.isdir(IN), reason='real fixture files not present')

FUND = ['TFAI_Investments_and_Deployment.xlsx', 'TFAI_Valuations_and_Exits_Q2FY26.xlsx']


def _anchors():
    return fund_anchor.build_fund_anchors([os.path.join(IN, f) for f in FUND])


def _anchor_for(anchors, token):
    return next(ca for k, ca in anchors.items() if token in k)


def _figs(rec):
    return {v.concept: v for v in rec.fields.values() if isinstance(v, Figure)}


def _extract(anchors, token, fname):
    ca = _anchor_for(anchors, token)
    rec = extract_company(ca.company, os.path.join(IN, fname),
                          rate_card=default_inr_card('2026-02-28'), entity=ca.company,
                          domicile=ca.domicile, anchor_cr=ca.anchor_cr)
    return _figs(rec)


def _close(fig, expected, *, basis=None, cell=None, n_cells=None):
    """Assert value ≈ expected AND the provenance cites reconstructable evidence under the
    universal model. Structural invariant: cell=='' IFF the value is a multi-cell sum."""
    assert fig.value_cr is not None and abs(fig.value_cr - Decimal(str(expected))) < Decimal('0.02'), \
        f'{fig.concept}={fig.value_cr} expected≈{expected}'
    assert fig.provenance.derived_from, f'{fig.concept}: emits but cites no evidence cells'
    is_sum = len(fig.provenance.derived_from) > 1
    assert (fig.provenance.cell == '') == is_sum, \
        (f'{fig.concept}: provenance model broken — cell={fig.provenance.cell!r} but '
         f'{len(fig.provenance.derived_from)} derived cells (a sum must cite cell="")')
    if basis is not None:
        assert fig.basis == basis, f'{fig.concept} basis={fig.basis} expected {basis}'
    if cell is not None:
        assert fig.provenance.cell == cell, \
            f'{fig.concept} value cell {fig.provenance.cell}, expected {cell} (wrong-column regression)'
    if n_cells is not None:
        assert len(fig.provenance.derived_from) == n_cells, \
            f'{fig.concept} cites {len(fig.provenance.derived_from)} cells, expected {n_cells}'


def test_fund_anchor_values_per_company():
    a = _anchors()
    ldc = _anchor_for(a, 'ldc')
    agni = _anchor_for(a, 'agnikul')
    assert ldc.cost_cr == 110 and ldc.fair_value_cr == 231
    assert agni.cost_cr == 65 and agni.fair_value_cr == 150
    assert sum((c.cost_cr for c in a.values() if c.cost_cr), Decimal('0')) == 448


def test_hubler_mis_values_trace_to_cells():
    f = _extract(_anchors(), 'hubbler', 'AVF_2026_03_11_P_Hubler_MIS_Feb26.xlsx')
    _close(f['revenue'], '4.1837', basis='TTM', n_cells=12)     # 12-month sum, row 20 (label@D20)
    _close(f['ebitda'], '-0.4304', basis='TTM', n_cells=12)     # EBITDA line, not EBIT — row 72
    # CASH re-pointed (advisor 2026-07-23): the deterministic binder lands on D22
    # "Cash Collected" — a period FLOW, not the cash STOCK. The stock-vs-flow guard
    # (U5) HOLDS it (before collapse, so it keeps the label-cell provenance D22) instead
    # of emitting the wrong ₹0.1822 Cr flow. The correct cash is the balance-sheet
    # "Closing balance" (row 98), reachable only via the model locator, never this bind.
    cash = f['cash']
    assert cash.value_cr is None and cash.held, 'Hubler cash must HOLD, not emit the Cash-Collected flow'
    assert 'flow-labelled' in (cash.hold_reason or ''), cash.hold_reason
    assert 'D22' in (cash.provenance.note or '')     # the flow row (label@D22) it correctly refused


def test_ldc_mis_values_trace_to_cells():
    f = _extract(_anchors(), 'ldc', 'AVF_2026_03_20_P_LDC_Detailed_MIS_Feb26.xlsx')
    _close(f['revenue'], '316.4753', basis='TTM', n_cells=12)   # Σ C34..N34
    _close(f['cash'], '241.9993', basis='point_in_time', cell='N43')     # latest balance
    _close(f['headcount'], 309, basis='point_in_time', cell='N49')       # latest period-end count


def test_instaastro_mis_values_trace_to_cells():
    f = _extract(_anchors(), 'instaastro', 'AVF_2026_03_18_P_InstaAstro_MIS_Feb_2026.xlsx')
    _close(f['ebitda'], '-8.8686', basis='TTM', n_cells=12)
    _close(f['cash'], '17.7422', basis='point_in_time', cell='BG79')


def test_agnikul_mis_values_trace_to_cells():
    # '(₹ Millions)' declared unit (plural — must be detected). Revenue is the same-year
    # YTD column (E5, partial months + YTD), cash a point-in-time balance, headcount a
    # point-in-time count. Locks FIX 1 (plural unit) + the value-cell provenance model.
    f = _extract(_anchors(), 'agnikul', 'AVF_2026_03_12_P_Agnikul_MIS_Feb26.xlsx')
    _close(f['revenue'], '5.1460', basis='YTD', cell='E5')       # 'Total Income' YTD FY26
    _close(f['cash'], '117.6646', basis='point_in_time', cell='B28')     # 1176.65 M → ₹117.66 Cr
    _close(f['headcount'], 299, basis='point_in_time', cell='B29')       # Feb'26 period-end


def test_agnikul_balance_sheet_family_confirms_single_entity():
    # 3c calibration, LOCKED on the REAL statement via the production path (profile_file →
    # _family_verdicts → confirm_family): Agnikul's 'Balance Sheet' tab satisfies
    # assets = liabilities + equity (residual ₹0.15 on ₹2.6bn; the file's own 'Difference'
    # row = 0.1 corroborates). This is the BS confirmer the tier selector consumes and the
    # Step-6 model will lean on when it selects a BS-family sheet.
    # SCOPE: validated for a SINGLE-ENTITY statement — Agnikul is not a consolidated group, so
    # NO minority-interest/net-assets term is present and a=l+e holds exactly WITHOUT it. The
    # consolidated case (a = l + e + NCI) is a characterized deferral (ledger), triggered when a
    # consolidated-GROUP emitting file shows the identity break with an NCI gap.
    from backend.dataimport.preingest3 import periods, family
    from backend.dataimport.preingest3 import extract as ex
    from backend.dataimport.preingest3.profiler import profile_file
    prof = profile_file('Agnikul', os.path.join(IN, 'AVF_2026_03_12_P_Agnikul_MIS_Feb26.xlsx'))
    rows = prof['grid']['Balance Sheet']
    ax = periods.detect_period_axis(rows)
    verdicts = ex._family_verdicts(rows, ax, ex._sheet_label_col(rows, ax.axis_rows[0]))
    assert verdicts['balance_sheet'] == family.CONFIRMED


def test_cpc_ebitda_resourced_from_income_statement_not_cash_flow():
    # fork-b reddening control on the production path — the Guard1→fork-b progression:
    #   (1) CPC's single best-sheet is 'CFS EL' (a CASH FLOW statement); its EBITDA line is the
    #       wrong source. Guard 1 turned that emit into a HOLD (wrong-source).
    #   (2) fork-b's per-concept-family re-source then finds EBITDA on the INCOME statement
    #       ('PL Summary') and emits it — held→correct-SOURCE. This is the architectural fix:
    #       a company's EBITDA belongs to its P&L, not its cash-flow tab.
    # The value is the same-year YTD column (F25 = 156.5 Mn = ₹15.65 Cr, basis=YTD), consistent
    # with the system's YTD-preference (locked for Agnikul revenue). The single-MONTH figure
    # (C25 = 43.91 Mn = ₹4.39 Cr) is NOT used — a YTD-vs-monthly KPI convention that spans all
    # companies, not a CPC quirk. The critical improvement over the pre-Guard1 emit: right
    # STATEMENT (PL Summary, not CFS EL) AND right BASIS (YTD, not the landmine months=1 that
    # annualisation would ×12).
    # RE-HOMED COVERAGE: the comparative-column-SUM protection this test formerly held (May-25 |
    # Dec-24 | May-24 must NOT sum to ₹79.62 Cr) is directly, file-independently exercised by
    # test_periods_ttm.py::test_gappy_snapshots_are_not_summed_take_latest.
    f = _extract(_anchors(), 'cpc', 'CPC_Monthly_MIS-_May_25_to_be_sent_to_EL.xlsx')
    eb = f['ebitda']
    assert not eb.held and eb.value_cr is not None, 'fork-b must re-source CPC EBITDA to an emit'
    assert eb.provenance.sheet == 'PL Summary', \
        f'CPC EBITDA must be re-sourced from the income statement, got {eb.provenance.sheet}'
    _close(eb, '15.6492', basis='YTD', cell='F25')


if __name__ == '__main__':
    test_fund_anchor_values_per_company()
    test_hubler_mis_values_trace_to_cells()
    test_ldc_mis_values_trace_to_cells()
    test_instaastro_mis_values_trace_to_cells()
    test_agnikul_mis_values_trace_to_cells()
    test_agnikul_balance_sheet_family_confirms_single_entity()
    test_cpc_ebitda_resourced_from_income_statement_not_cash_flow()
    print('ALL PASS — real-file values trace to the right cells with the right numbers')
