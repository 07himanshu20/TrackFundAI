"""Phase D — fund OUTPUT sections (Fund_Summary + LP_Register) in the workbook.

Proves the four output-layer disciplines, on synthetic CIRs (deterministic) and the
real fund files:
  • FAIL-CLOSED total — a fund total whose tie-out reconciliation HELD, or whose input
    is held, renders INCOMPLETE, never a live number (guarantee #1 at the fund layer);
  • CUMULATIVE not annualised — called/distributed show as lifetime ₹Cr with basis
    'cumulative'; the annualisation machinery stays off them;
  • NEW sheets only — an MIS-only workbook gains NO fund sheets (MIS byte-identical);
  • PROVENANCE on the LP table — every LP figure traces to its source cell.
"""
import os
from decimal import Decimal

import pytest

from backend.dataimport.preingest3 import assemble, pipeline
from backend.dataimport.preingest3.cir import CIR, Record, Figure, Provenance
from backend.dataimport.preingest3.ratecard import default_inr_card

IN = 'backend/media/preingest/trivesta/100e86d5/in'
_real = pytest.mark.skipif(not os.path.isdir(IN), reason='real fixture files not present')


def _pv(sheet, cell):
    return Provenance(source_file='terms', content_fingerprint='', sheet=sheet, cell=cell)


def _fund_cir(*, hold_called=False, hold_commitment=False, hold_capital_called=False, called_tie='pass'):
    """Synthetic fund CIR: capital account (600/70) + 2 LPs that Σ to 600/70/1000,
    with the reconciliation checks the summary reads. Alpha 400/400/50, Beta 600/200/20.
    The hold_* flags simulate the two INCOMPLETE paths independently: a held capital
    figure (flows), a held LP commitment figure (corpus), a held LP called figure (Σ row)."""
    cir = CIR(as_of='2026-06-30', rate_card_id='rc')
    cir.add(Record('fund_financials', entity_id='fund', fields={'fund': 'fund',
        'called': Figure('called', Decimal('600'), None, _pv('Drawdowns', 'D11'), basis='cumulative', held=hold_capital_called),
        'distributed': Figure('distributed', Decimal('70'), None, _pv('Distributions', 'G9'), basis='cumulative')}))
    for name, com, cal, dis, r in (('Alpha LP', 400, 400, 50, 6), ('Beta LP', 600, 200, 20, 7)):
        cir.add(Record('lp_register', entity_id=name, fields={'lp_name': name, 'lp_type': 'Inst',
            'commitment': Figure('commitment', Decimal(str(com)), None, _pv('LP list', f'E{r}'), basis='point_in_time', held=hold_commitment),
            'called': Figure('called', Decimal(str(cal)), None, _pv('LP list', f'F{r}'), basis='cumulative', held=hold_called),
            'distributed': Figure('distributed', Decimal(str(dis)), None, _pv('LP list', f'G{r}'), basis='cumulative')}))
    cir.checks = [
        {'id': 'lp_called_ties_to_capital_account', 'class': 'hard', 'status': called_tie, 'lhs': '600', 'rhs': '600'},
        {'id': 'lp_distributed_ties_to_capital_account', 'class': 'hard', 'status': 'pass', 'lhs': '70', 'rhs': '70'},
        {'id': 'commitments_sum', 'class': 'hard', 'status': 'pass', 'lhs': '1000', 'rhs': '1000'},
        {'id': 'lp_total_row_multiset', 'class': 'hard', 'status': 'pass'},
    ]
    return cir


def _rows(wb, sheet):
    return [tuple(r) for r in wb[sheet].iter_rows(values_only=True)]


def _summary_row(wb, label):
    return next(r for r in _rows(wb, 'Fund_Summary') if r and r[0] == label)


# ── happy path: reconciled totals, cumulative basis ──────────────────────────
def test_fund_summary_shows_reconciled_cumulative_totals():
    wb = assemble.build(_fund_cir())
    called = _summary_row(wb, 'Capital Called (cumulative)')
    assert called[1] == 600.0 and called[2] == 'cumulative' and called[4] == 'reconciled'
    assert _summary_row(wb, 'Distributions (cumulative)')[1] == 70.0
    corpus = _summary_row(wb, 'Total Commitment / Corpus')
    assert corpus[1] == 1000.0 and corpus[2] == 'point_in_time' and corpus[4] == 'reconciled'


def test_cumulative_flows_are_not_annualized():
    # the value is the lifetime cumulative (600), NOT an annualised run-rate. The basis
    # column says 'cumulative', and the header disclaims annualisation.
    wb = assemble.build(_fund_cir())
    assert _summary_row(wb, 'Capital Called (cumulative)')[1] == 600.0
    hdr = _rows(wb, 'Fund_Summary')[1][0]
    assert 'NOT annualised' in hdr


# ── FAIL-CLOSED negative controls ────────────────────────────────────────────
def test_summary_incomplete_when_tieout_held_NEGATIVE_CONTROL():
    # cross-slice reconciliation held (indeterminate) → the called total is not a number.
    wb = assemble.build(_fund_cir(called_tie='indeterminate'))
    called = _summary_row(wb, 'Capital Called (cumulative)')
    assert called[1] == assemble.INCOMPLETE and 'did not reconcile' in called[4]
    # the reconciled concepts are unaffected — one held tie-out doesn't blank the rest
    assert _summary_row(wb, 'Total Commitment / Corpus')[1] == 1000.0


def test_summary_flow_incomplete_when_capital_figure_held_NEGATIVE_CONTROL():
    # the flow rows source from the capital-account figure — a held capital called →
    # INCOMPLETE, never a SUM that zeroes the hole.
    wb = assemble.build(_fund_cir(hold_capital_called=True))
    assert _summary_row(wb, 'Capital Called (cumulative)')[1] == assemble.INCOMPLETE
    assert _summary_row(wb, 'Distributions (cumulative)')[1] == 70.0        # unaffected sibling


def test_summary_corpus_incomplete_when_commitment_held_NEGATIVE_CONTROL():
    # the corpus row sources from the per-LP commitment figures — any held → INCOMPLETE.
    wb = assemble.build(_fund_cir(hold_commitment=True))
    assert _summary_row(wb, 'Total Commitment / Corpus')[1] == assemble.INCOMPLETE


def test_lp_register_total_incomplete_on_held_NEGATIVE_CONTROL():
    wb = assemble.build(_fund_cir(hold_called=True))
    sigma = next(r for r in _rows(wb, 'LP_Register') if r and r[0] == 'Σ (hole-aware)')
    assert sigma[3] == assemble.INCOMPLETE          # called column (index 3) held → INCOMPLETE
    assert sigma[2] == 1000.0                        # commitment still complete


# ── provenance on the LP table ───────────────────────────────────────────────
def test_lp_register_carries_provenance_cells():
    wb = assemble.build(_fund_cir())
    body = [r for r in _rows(wb, 'LP_Register') if r and r[0] == 'Alpha LP'][0]
    assert body[2] == 400.0 and body[5] == 'LP list!E6'      # commitment value + its cell
    assert body[6] == 'LP list!F6' and body[7] == 'LP list!G6'


# ── NEW sheets only — MIS byte-identical ─────────────────────────────────────
def test_mis_only_workbook_has_no_fund_sheets():
    pv = Provenance(source_file='x', content_fingerprint='', sheet='S', cell='A1')
    mis = CIR(as_of='2026-02-28', rate_card_id='rc')
    mis.add(Record('mis', entity_id='Acme', fields={'company': 'Acme',
        'revenue': Figure('revenue', Decimal('10'), None, pv, basis='YTD', months=12)}))
    sheets = assemble.build(mis).sheetnames
    assert 'Fund_Summary' not in sheets and 'LP_Register' not in sheets


# ── real files (production path) ─────────────────────────────────────────────
@_real
def test_fund_output_on_real_files():
    FUND = ['TFAI_Capital_Calls_and_Distributions_draft.xlsx',
            'TFAI_Fund_Terms_and_LP_Register_wip.xlsx',
            'TFAI_Fund_Accounts_Fees_Budget_Compliance.xlsx']
    res = pipeline.run([(f, os.path.join(IN, f)) for f in FUND], as_of='2026-06-30', org='t',
                       rate_card=default_inr_card('2026-06-30'))
    wb = assemble.build(res.cir, rate_card=default_inr_card('2026-06-30'))
    assert _summary_row(wb, 'Capital Called (cumulative)')[1] == 600.0
    assert _summary_row(wb, 'Total Commitment / Corpus')[1] == 1000.0
    lp = _rows(wb, 'LP_Register')
    assert any(r and r[0] == 'TFAI GP LLP (Sponsor)' and r[5] == 'LP list!E15' for r in lp)  # GP row, cited
    sigma = next(r for r in lp if r and r[0] == 'Σ (hole-aware)')
    assert (sigma[2], sigma[3], sigma[4]) == (1000.0, 600.0, 70.0)


# ── cross-process determinism (same gate as MIS — not a same-run re-hash) ────
import subprocess
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))   # …/backend
_IN_REL = 'media/preingest/trivesta/100e86d5/in'


@pytest.mark.slow
@pytest.mark.skipif(not os.path.isdir(os.path.join(_BACKEND, _IN_REL)), reason='real fixture files not present')
def test_fund_region_is_deterministic_across_hash_seeds():
    # Fund_Summary + LP_Register + Fund_Terms + term checks must be byte-identical across
    # two PYTHONHASHSEED processes — the cross-process gate (a set/dict iteration feeding
    # term selection or check order would diverge here, invisible to a same-run re-hash).
    def _run(seed):
        env = {**os.environ, 'PYTHONHASHSEED': seed}
        p = subprocess.run([sys.executable, '-m', 'dataimport.preingest3.tests._fund_det_worker', _IN_REL],
                           cwd=_BACKEND, env=env, capture_output=True, text=True, timeout=180)
        assert p.returncode == 0, p.stderr[-500:]
        return p.stdout
    assert _run('0') == _run('99991'), 'fund region NONDETERMINISTIC across PYTHONHASHSEED'


if __name__ == '__main__':
    for n, fn in sorted(globals().items()):
        if n.startswith('test_') and callable(fn) and not n.endswith('real_files'):
            fn(); print(f'ok  {n}')
