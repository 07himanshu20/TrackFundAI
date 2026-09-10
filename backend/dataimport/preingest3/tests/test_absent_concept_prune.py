"""Absent/held-concept PRUNE in _model_fill (the AI-on latency root-cause fix) — proven with a
DETERMINISTIC fake locator (no live model), because the live model's call count is non-deterministic
(43 vs 117 cold on the same files) and cannot A/B a latency fix.

Root cause: a concept leaves `remaining` only on a clean EMIT, so a concept that is absent OR
located-but-HELD (first-seen fail-closed) stays in `remaining` and the loop probes EVERY statement
sheet for it — one serial locate call each (measured: one file = 24 futile calls). Fix: once a
concept's home statement-kinds' BEST sheets have been tried without a clean emit, it is pruned
(deep whole-file search is the finder's job). Generous cross-kind guarantee: every legitimate
statement home is tried first (cash → income+BS+CFS). Conscious speed-for-reach trade on a
SECONDARY same-kind tab.

These prove BOTH directions: WITH the prune the futile hunt collapses to ~one call; the negative
control (kind classifier disabled → no prune) reddens back to one call PER SHEET."""
import types

import pytest

from backend.dataimport.preingest3 import extract, gate, llm
from backend.dataimport.preingest3.cir import Figure, Provenance
from backend.dataimport.preingest3.ratecard import default_inr_card

_RC = default_inr_card('2026-06-30')

_INCOME = [
    ['Income Statement (INR Cr)', 'Apr-25', 'May-25', 'Jun-25'],
    ['Revenue', 10, 11, 12],
    ['COGS', 4, 4, 5],
    ['Gross Profit', 6, 7, 7],
    ['OPEX', 3, 4, 4],
    ['EBITDA', 3, 3, 3],
]
_CFS = [
    ['Cash Flow Statement (INR Cr)', 'Apr-25', 'May-25', 'Jun-25'],
    ['Operating Cash Flow', 8, 9, 10],
    ['Investing Cash Flow', -2, -1, -1],
    ['Financing Cash Flow', 1, 1, 1],
    ['Net Change in Cash', 7, 9, 10],
    ['Closing Cash', 5, 6, 7],
]


def _ident(fp='cfp'):
    return types.SimpleNamespace(content_fp=fp, layout_fp='lfp-' + fp)


def _gap_fields():
    pv = Provenance(source_file='t.xlsx', content_fingerprint='cfp', sheet='', cell='', row_label='')
    f = {'company': 'TestCo'}
    for c in extract.MIS_CONCEPTS:
        f[c] = Figure(c, None, None, pv, gap=True)
    return f


def _payload(rowmap):
    items = ', '.join(f'{{"concept":"{c}","form":"direct","row":{r},"row_label":"{lbl}","rows":[]}}'
                      for c, (r, lbl) in rowmap.items())
    return '{"located":[' + items + ']}'


def _sheet(name):
    return types.SimpleNamespace(sheet=name, currency_hints=['INR'])


def _fill(prof, provider, monkeypatch=None):
    """Run _model_fill; if monkeypatch is given, count SHEETS PROBED (= locate_rows calls, the unit
    the prune reduces — llm.calls counts the several model calls locate_rows makes per sheet)."""
    llm.set_model_provider(provider)
    counter = {'sheets': 0}
    if monkeypatch is not None:
        real = extract.locator.locate_rows
        monkeypatch.setattr(extract.locator, 'locate_rows',
                            lambda *a, **k: (counter.__setitem__('sheets', counter['sheets'] + 1)
                                             or real(*a, **k)))
    fields = _gap_fields()
    diags = extract._model_fill(prof, _ident(), list(extract.MIS_CONCEPTS),
                                entity='TestCo', domicile=None, anchor_cr=None,
                                rate_card=_RC, fields=fields, source_label='t.xlsx')
    return fields, diags, counter['sheets']


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    saved = llm._MODEL_PROVIDER
    monkeypatch.setattr(llm, '_rl_sleep', lambda *a, **k: None)
    monkeypatch.setattr(llm, '_CACHE_DIR', str(tmp_path / 'calls'))       # cold, isolated call cache
    monkeypatch.setattr(gate, '_RELI_PATH', str(tmp_path / 'reli.json'))
    monkeypatch.setattr(gate, '_AUDIT_PATH', str(tmp_path / 'audit.json'))
    llm.new_metrics()
    yield
    llm._MODEL_PROVIDER = saved


# ══════════════════════════════════════════════════════════════════════════════════
# THE FIX + its negative control — the futile hunt collapses to ~one call
# ══════════════════════════════════════════════════════════════════════════════════
def _three_income_prof():
    return {'sheets': [_sheet(f'P&L{i}') for i in range(3)],
            'grid': {f'P&L{i}': [list(r) for r in _INCOME] for i in range(3)}}


def test_prune_collapses_the_hunt_to_one_sheet(monkeypatch):
    # every concept is located-but-HELD (first-seen) or missing → stays in `remaining`. WITHOUT the
    # prune the loop probes all 3 income sheets; WITH it, all concepts are pruned after the best
    # income sheet → the loop breaks after ONE sheet is probed.
    _f, _d, sheets = _fill(_three_income_prof(), lambda p, **k: types.SimpleNamespace(
        text=_payload({'revenue': (2, 'Revenue'), 'ebitda': (6, 'EBITDA')})), monkeypatch)
    assert sheets == 1


def test_negative_control_without_kind_no_prune_probes_every_sheet(monkeypatch):
    # disable the statement-kind classifier → no concept has a home-kind → NOTHING is pruned →
    # the loop reddens back to one probe PER SHEET (3). Proves the prune is what saves the calls.
    monkeypatch.setattr(extract, '_statement_kind', lambda *a, **k: None)
    _f, _d, sheets = _fill(_three_income_prof(), lambda p, **k: types.SimpleNamespace(
        text=_payload({'revenue': (2, 'Revenue'), 'ebitda': (6, 'EBITDA')})), monkeypatch)
    assert sheets == 3


# ══════════════════════════════════════════════════════════════════════════════════
# CROSS-KIND COVERAGE (§2.2) — cash absent from the income sheet is STILL found on the CFS,
# because cash's home kinds (income + cash_flow) are BOTH tried before it can be pruned.
# ══════════════════════════════════════════════════════════════════════════════════
def test_cross_kind_cash_on_cashflow_still_reached_when_absent_from_income():
    prof = {'sheets': [_sheet('P&L'), _sheet('Cash Flow')],
            'grid': {'P&L': [list(r) for r in _INCOME], 'Cash Flow': [list(r) for r in _CFS]}}

    def provider(prompt, **kw):
        if 'cash flow' in prompt.lower():
            return types.SimpleNamespace(text=_payload({'cash': (6, 'Closing Cash')}))
        return types.SimpleNamespace(text=_payload({'revenue': (2, 'Revenue'), 'ebitda': (6, 'EBITDA')}))

    _fields, diags, _sheets = _fill(prof, provider)
    cash_diag = next((d for d in diags if d['concept'] == 'cash'), None)
    assert cash_diag is not None, 'cash was pruned before reaching the CFS — cross-kind coverage LOST'
    assert cash_diag['sheet'] == 'Cash Flow'          # cash reached its OTHER home statement


def test_safe_fallback_when_no_recognised_kind_present(monkeypatch):
    # a file whose sheets carry no recognised statement kind → home-kinds empty → NEVER pruned →
    # full probe (byte-identical to pre-prune). Safe fallback on any layout the classifier can't read.
    monkeypatch.setattr(extract, '_statement_kind', lambda *a, **k: None)
    prof = {'sheets': [_sheet(f'Tab{i}') for i in range(2)],
            'grid': {f'Tab{i}': [list(r) for r in _INCOME] for i in range(2)}}
    _f, _d, sheets = _fill(prof, lambda p, **k: types.SimpleNamespace(
        text=_payload({'revenue': (2, 'Revenue')})), monkeypatch)
    assert sheets == 2                                 # both sheets probed — no prune
