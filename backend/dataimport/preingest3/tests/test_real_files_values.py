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
    # operating revenue: located row is 'Total revenue' (aggregate) but a directly-stated 'Total revenue
    # from operations' row exists → RELOCATE to it (rule #1, clean cite). TTM 40,476,676 → ₹4.0477 Cr,
    # verified: = the stated operating-revenue row, and = Total revenue − Total other income.
    _close(f['revenue'], '4.0477', basis='TTM', n_cells=12)     # 12-month sum of the operating-revenue row
    assert 'operating revenue' in (f['revenue'].provenance.note or '')       # relocation disclosed
    assert f['revenue'].provenance.row_label == 'Total revenue from operations'
    _close(f['ebitda'], '-0.4304', basis='TTM', n_cells=12)     # EBITDA line, not EBIT — row 72
    # CASH RECOVERED (R4, advisor 2026-08-14): the deterministic binder lands `cash` on D22
    # "Cash Collected" — a period FLOW, not the cash STOCK — which the stock-vs-flow guard (U5)
    # correctly refuses. R4 then rebinds cash to its balance-sheet equivalent, the "Closing
    # balance" (row 98), located via closing_cash's OWN lexicon (_find_equivalent_stock_row —
    # NOT the _DISAMBIG cross-concept contest, which would tie 'Closing Cash Balance' and regress
    # CPC). The emitted ₹1.0621 Cr is the LATEST closing bank balance (BB98 = 10,620,993), the
    # point-in-time stock the prior comment always named as the correct answer — now reachable on
    # the DETERMINISTIC path, not only via the model locator. Fail-closed: had no non-flow balance
    # existed, cash would still HOLD (see test_find_equivalent_stock_row negative control).
    _close(f['cash'], '1.0621', basis='point_in_time', cell='BB98')


def test_find_equivalent_stock_row_recovers_balance_and_reddens_when_absent():
    """R4 unit + NEGATIVE CONTROL. The dedicated closing_cash locator finds a real 'Closing
    balance' stock row (so a flow-bound cash can recover), while SKIPPING flow lines ('Cash
    Collected') and opening lines ('Opening balance'). The load-bearing proof is the negative
    control: with only flow rows present it returns None, so the recovery cannot spuriously fire
    and cash stays HELD — the fail-closed floor the guard promises."""
    import types
    from backend.dataimport.preingest3.extract import _find_equivalent_stock_row
    axis = [types.SimpleNamespace(col=1), types.SimpleNamespace(col=2)]
    rows_full = [
        ['Particulars', 'Apr', 'May'],
        ['Cash Collected', 5, 6],        # flow — must be skipped
        ['Opening balance', 90, 100],    # opening — must be skipped (_AGG_ANTI)
        ['Closing balance', 100, 110],   # the real stock — must be found
    ]
    assert _find_equivalent_stock_row(rows_full, 0, 'closing_cash', 1, len(rows_full), axis) == 3
    # NEGATIVE CONTROL: no non-flow balance → None → a flow-bound cash STAYS HELD (fail-closed)
    rows_flowonly = [['Particulars', 'Apr', 'May'], ['Cash Collected', 5, 6], ['Cash outflow', 7, 8]]
    assert _find_equivalent_stock_row(rows_flowonly, 0, 'closing_cash', 1, 3, axis) is None
    # an opening-only sheet must never be mistaken for the closing stock
    rows_openonly = [['Particulars', 'Apr', 'May'], ['Opening balance', 90, 100]]
    assert _find_equivalent_stock_row(rows_openonly, 0, 'closing_cash', 1, 2, axis) is None


def test_closing_cash_stays_out_of_disambig_so_cpc_cash_binds_via_contest():
    """R4 DESIGN LOCK (advisor 2026-08-14). The R4 recovery uses a DEDICATED locator precisely so
    closing_cash need NOT enter the cross-concept _DISAMBIG set. Were it leaked in, CPC's 'Closing
    Cash Balance including Fix' would tie against `cash` → margin-fail → CPC's cash bind regresses
    to a gap. Pin the invariant so that regression REDDENS here, not silently in production."""
    from backend.dataimport.preingest3.extract import _DISAMBIG
    for anchor in ('closing_cash', 'opening_cash', 'receipts', 'payments'):
        assert anchor not in _DISAMBIG, (
            f'{anchor} leaked into _DISAMBIG — a cash-flow anchor in the cross-concept contest '
            f'regresses stock binds (e.g. CPC cash). Use _find_equivalent_stock_row instead.')


def test_figure_anchor_guard_exempts_profit_and_reddens():
    """The centralised figure-anchor magnitude guard (ONE predicate `_figure_anchor_hold_reason` now
    shared by BOTH emit paths — collapse + re-source — so they cannot drift). A profit/burn concept is
    EXEMPT: a near-breakeven EBITDA orders below company valuation is legitimate, not a wrong row. A
    size-scaling concept (revenue/cash) is NOT exempt and still holds when orders from scale. REDDENING
    PAIRS — same tiny value, only the concept differs → exempt=emit / non-exempt=hold — proving the
    exemption is load-bearing, not a blanket disable of the guard."""
    from backend.dataimport.preingest3.extract import _figure_anchor_hold_reason
    anchor = Decimal('82')
    # too-SMALL (|value| << anchor): profit exempt (None ⇒ emits); size-scaling concepts still hold
    assert _figure_anchor_hold_reason('ebitda', Decimal('-0.0117'), anchor) is None      # near-breakeven emits
    assert _figure_anchor_hold_reason('net_income', Decimal('0.01'), anchor) is None      # profit exempt
    assert _figure_anchor_hold_reason('revenue', Decimal('0.0117'), anchor) is not None   # revenue this tiny holds
    assert _figure_anchor_hold_reason('cash', Decimal('0.0117'), anchor) is not None      # cash this tiny holds
    # too-BIG (>3 orders above): profit fully exempt (matches the re-source path's long-standing
    # behaviour); a non-profit concept this far above scale still holds
    assert _figure_anchor_hold_reason('ebitda', Decimal('100000'), anchor) is None
    assert _figure_anchor_hold_reason('revenue', Decimal('100000'), anchor) is not None
    # in-range / exact-zero / no-anchor never fire
    assert _figure_anchor_hold_reason('revenue', Decimal('5.3'), anchor) is None
    assert _figure_anchor_hold_reason('revenue', Decimal('0'), anchor) is None
    assert _figure_anchor_hold_reason('revenue', Decimal('0.0001'), None) is None


def test_aliste_ebitda_recovered_and_depends_on_the_profit_exemption(monkeypatch):
    """R1d recovery + LOAD-BEARING control. Aliste EBITDA was FALSELY held: the figure-anchor guard in the
    deterministic COLLAPSE path was a duplicate that lacked the profit exemption the re-source path already
    had (centralise-don't-duplicate drift). Centralised, EBITDA now EMITS the real FYTD −₹0.0117 Cr — the
    11-month Apr-25..Feb-26 sum (Mar-26=0 placeholder excluded), = the sheet's OWN 'YTD' column to the
    rupee. Revenue likewise 11-month FYTD (basis='partial', NOT mislabelled TTM). Cash stays correctly held
    (BS 3-block ambiguity). REDDENING: neutralise the exemption set → the near-zero EBITDA reverts to a
    figure-anchor hold, proving the recovery is the exemption and not a bare GT edit."""
    import backend.dataimport.preingest3.extract as ex
    f = _extract(_anchors(), 'aliste', 'AVF_2026_03_26_P_Aliste_MIS_Feb26.xlsx')
    _close(f['revenue'], '5.3052', basis='partial', n_cells=11)   # 11-month FYTD (partial, not TTM)
    _close(f['ebitda'], '-0.0117', basis='partial', n_cells=11)   # the recovery
    assert f['cash'].held, 'Aliste cash is a correct HOLD (real value on the ambiguous BS 3-block)'
    monkeypatch.setattr(ex, '_FIGURE_SANITY_EXEMPT', frozenset())
    f2 = _extract(_anchors(), 'aliste', 'AVF_2026_03_26_P_Aliste_MIS_Feb26.xlsx')
    assert f2['ebitda'].held and 'orders from company scale' in (f2['ebitda'].hold_reason or ''), \
        'without the profit exemption the near-zero EBITDA must revert to a figure-anchor hold (RED)'


def test_ldc_mis_values_trace_to_cells():
    f = _extract(_anchors(), 'ldc', 'AVF_2026_03_20_P_LDC_Detailed_MIS_Feb26.xlsx')
    _close(f['revenue'], '316.4753', basis='TTM', n_cells=12)   # Σ C34..N34
    _close(f['cash'], '241.9993', basis='point_in_time', cell='N43')     # latest balance
    _close(f['headcount'], 309, basis='point_in_time', cell='N49')       # latest period-end count
    # Lever 5 sub-B/sub-A2: EBITDA on the SAME basis as revenue (TTM Mar'25..Feb'26), NOT the Feb'26
    # single month (₹21.04). The reporting sheet states ONE self-describing EBITDA-equivalent — 'Profit
    # Before Tax, depreciation and ESOP' (R41) — an ESOP-ADJUSTED figure with NO plain/standard EBITDA on
    # the reporting basis (the Org-P-L split is not derivable on the TTM window — FY26, no Mar'25). So the
    # plain/comparable `ebitda` HOLDS and the ESOP-adjusted figure moves to the `ebitda_adjusted` companion
    # (₹113.6891 Cr TTM, tagged ESOP) — the comparable EBITDA column is never contaminated by an adjusted
    # number. Ties to the Org P-L PBT+Depn+ESOP identity every overlapping month.
    assert f['ebitda'].held and f['ebitda'].value_cr is None, \
        f"LDC plain/standard ebitda must HOLD (only an adjusted proxy exists), got {f['ebitda'].value_cr}"
    assert 'no plain/standard' in (f['ebitda'].hold_reason or '').lower()
    adj = f['ebitda_adjusted']
    _close(adj, '113.6891', basis='TTM', n_cells=12)                     # Σ C41..N41
    assert adj.adjustment_type == 'ESOP', f'expected ESOP tag, got {adj.adjustment_type!r}'
    assert adj.provenance.row_label == 'Profit Before Tax, depreciation and ESOP'


def test_instaastro_mis_values_trace_to_cells():
    f = _extract(_anchors(), 'instaastro', 'AVF_2026_03_18_P_InstaAstro_MIS_Feb_2026.xlsx')
    _close(f['ebitda'], '-8.8686', basis='TTM', n_cells=12)
    _close(f['cash'], '17.7422', basis='point_in_time', cell='BG79')
    # Lever 5 (clean-operating construction): the 'Total Revenues' aggregate folds in non-operating Other
    # Incomes (non-zero in some of the 12 TTM months), so the total itself is not emittable AS operating
    # revenue — but its leaf components RECONCILE to the total in every column, so clean operating revenue
    # = Σ(operating components) = Total − Other Incomes is CONSTRUCTED and emitted (₹104.2639 Cr TTM,
    # cited to the operating component cells; cell='' as a multi-cell sum). Was HELD pre-Lever-5.
    _close(f['revenue'], '104.2639', basis='TTM')


def test_agnikul_mis_values_trace_to_cells():
    # '(₹ Millions)' declared unit (plural — must be detected). Cash a point-in-time balance,
    # headcount a point-in-time count. REVENUE is HELD, fail-closed: Agnikul's 'Total Income' is
    # Interest-on-FDs + Other Income — BOTH non-operating (a pre-revenue firm living on treasury
    # interest), with NO stated 'Revenue from operations' row. Emitting Total Income AS revenue is a
    # silent relabel (forbidden); operating revenue = Total Income − Other Income is a subtraction the
    # additive provenance model cannot cite → held with a disclosure. (Nothing to recover even once
    # signed-provenance lands — a pre-revenue company has no operating revenue.)
    f = _extract(_anchors(), 'agnikul', 'AVF_2026_03_12_P_Agnikul_MIS_Feb26.xlsx')
    rev = f['revenue']
    assert rev.held and rev.value_cr is None, \
        f'Agnikul revenue must HOLD (aggregate = non-operating income, no operating-revenue row), got {rev.value_cr}'
    assert 'operating revenue' in rev.hold_reason.lower() and 'other income' in rev.hold_reason.lower(), \
        f'held revenue must DISCLOSE why (not a silent relabel), naming the non-op contamination, ' \
        f'got: {rev.hold_reason!r}'
    assert rev.provenance.row_label == 'Total Income', \
        f'held revenue must still cite the aggregate it declined to emit, got {rev.provenance.row_label!r}'
    _close(f['cash'], '117.6646', basis='point_in_time', cell='B28')     # 1176.65 M → ₹117.66 Cr
    _close(f['headcount'], 299, basis='point_in_time', cell='B29')       # Feb'26 period-end


def test_agnikul_revenue_hold_is_load_bearing(monkeypatch):
    # REDDENING control for the Agnikul revenue HOLD: the hold is caused by the operating-revenue
    # disposition, nothing else. Neutralise it (force 'keep') and Agnikul REVERTS to emitting Total
    # Income (₹5.146 Cr) AS revenue — the exact silent relabel the guard prevents. Proves the fix is
    # load-bearing, not a bare test edit (mirrors test_binding_truth::test_aliste_flip_depends_on_...).
    import backend.dataimport.preingest3.extract as ex
    monkeypatch.setattr(ex, '_operating_revenue_disposition', lambda *a, **k: ('keep', None))
    f = _extract(_anchors(), 'agnikul', 'AVF_2026_03_12_P_Agnikul_MIS_Feb26.xlsx')
    rev = f['revenue']
    assert rev.value_cr is not None and abs(rev.value_cr - Decimal('5.1460')) < Decimal('0.02'), \
        'without the disposition, Agnikul must revert to the silent Total-Income-as-revenue relabel (RED)'


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
    # companies (a cross-company convention, not a CPC quirk). The critical improvement over the pre-Guard1 emit: right
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
