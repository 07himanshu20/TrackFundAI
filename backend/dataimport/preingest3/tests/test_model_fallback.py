"""Phase 2b — the model-locator fallback (S4-REFINED: model returns a ROW, code owns
the period column), proven OFFLINE with a fake row-locator.

The contract (2026-07-23): the model returns a ROW + the label it read, NEVER a
column or a value. Code resolves the period column (CF1 collapse over Actual-only
columns), scale, and the value. So the model can't pick a wrong column — the
wrong-PERIOD error class is gone by construction — and the identity chain is
evaluated across rows in ONE code-chosen basis, which HARDENS the safety net.

We judge by the VERDICT, not the emit. Under maximally-fail-closed a first-seen
layout HOLDS even a perfect locate, so correctness is: did the model return the
RIGHT ROW and did the three code-read signals (existence / label / identity) PASS?

  • verdict     — correct row → all three signals PASS (held, first-seen).
  • emit path   — under the explicit loosening flag an all-PASS row emits the
                  CF1-collapsed ₹Cr value (a 3-month partial sum here).
  • ADVERSARIAL — revenue pointed at the EBITDA row: the re-read label + broken
                  identity dissent → HELD; the correct ebitda still emits.
  • FREEZE-AN-ERROR (D2) — a wrong row that got cached is STILL re-triangulated
                  and HELD; the cache freezes the row, never a verdict.
  • deterministic-first — code succeeds → model never called.
  • budget      — exhausted budget → disclosed, reproducible hold.

All fake-provider; no live Gemini; deterministic and hermetic in CI.
"""
import os
import tempfile
import types

import openpyxl
import pytest

from backend.dataimport.preingest3 import extract, gate, llm
from backend.dataimport.preingest3.cir import Figure, Provenance
from backend.dataimport.preingest3.gate import PASS
from backend.dataimport.preingest3.ratecard import default_inr_card

# One statement with a REAL month axis (CF1 needs it) + a P&L chain that satisfies
# both identities on the collapsed 3-month sums:  Σrev-Σcogs=Σgp (33-13=20) and
# Σgp-Σopex=Σebitda (20-11=9). Cash/headcount are stocks → CF1 takes the latest.
ROWS = [
    ['Particulars (INR Cr)', 'Apr-25', 'May-25', 'Jun-25'],   # row0 header/axis; 'INR Cr'
    ['Revenue', 10, 11, 12],           # 1-based row 2   Σ=33
    ['COGS', 4, 4, 5],                 # row 3           Σ=13
    ['Gross Profit', 6, 7, 7],         # row 4           Σ=20
    ['OPEX', 3, 4, 4],                 # row 5           Σ=11
    ['EBITDA', 3, 3, 3],               # row 6           Σ=9
    ['Cash', 5, 6, 7],                 # row 7           latest=7
    ['Headcount', 20, 21, 22],         # row 8           latest=22
]
_RC = default_inr_card('2026-06-30')
_ROW = {'revenue': 2, 'cogs': 3, 'gross_profit': 4, 'opex': 5, 'ebitda': 6, 'cash': 7, 'headcount': 8}
_LABEL = {'revenue': 'Revenue', 'cogs': 'COGS', 'gross_profit': 'Gross Profit', 'opex': 'OPEX',
          'ebitda': 'EBITDA', 'cash': 'Cash', 'headcount': 'Headcount'}


def _prof():
    sheet = types.SimpleNamespace(sheet='P&L', currency_hints=['INR'])
    return {'sheets': [sheet], 'grid': {'P&L': [list(r) for r in ROWS]}}


def _ident(fp='cfp-test'):
    return types.SimpleNamespace(content_fp=fp, layout_fp='lfp-' + fp)


def _gap_fields():
    pv = Provenance(source_file='t.xlsx', content_fingerprint='cfp-test',
                    sheet='', cell='', row_label='')
    f = {'company': 'TestCo'}
    for c in extract.MIS_CONCEPTS:
        f[c] = Figure(c, None, None, pv, gap=True)
    return f


def _located(rowmap):
    """rowmap = {concept: 1-based-row} → a fake provider returning row-based locator
    JSON (the S4-refined shape: row + label, never a column/value)."""
    items = ', '.join(
        f'{{"concept":"{c}","form":"direct","row":{r},"row_label":"{_LABEL.get(c, c)}","rows":[]}}'
        for c, r in rowmap.items())
    payload = '{"located":[' + items + ']}'

    def provider(prompt, **kw):
        return types.SimpleNamespace(text=payload)
    return provider


def _fill(fields, ident, provider=None):
    if provider is not None:
        llm.set_model_provider(provider)
    return extract._model_fill(_prof(), ident, list(extract.MIS_CONCEPTS),
                               entity='TestCo', domicile=None, anchor_cr=None,
                               rate_card=_RC, fields=fields, source_label='t.xlsx')


def _diag(diags, concept):
    return next(d for d in diags if d['concept'] == concept)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    saved = llm._MODEL_PROVIDER
    monkeypatch.setattr(llm, '_rl_sleep', lambda *a, **k: None)
    monkeypatch.setattr(llm, '_CACHE_DIR', str(tmp_path / 'calls'))
    monkeypatch.setattr(gate, '_RELI_PATH', str(tmp_path / 'reli.json'))
    monkeypatch.setattr(gate, '_AUDIT_PATH', str(tmp_path / 'audit.json'))
    llm.new_metrics()
    yield
    llm._MODEL_PROVIDER = saved


def test_verdict_correct_row_passes_all_three_signals_even_while_held():
    # DEFAULT (maximally fail-closed) — first-seen layout HOLDS even a perfect row.
    # Correctness = the VERDICT: right row + all three independent signals PASS.
    fields = _gap_fields()
    diags = _fill(fields, _ident('cfp-verdict'), _located(_ROW))
    rev = _diag(diags, 'revenue')
    assert rev['row'] == 2                                # model returned the right ROW
    assert rev['signals'] == {'existence': PASS, 'label': PASS, 'identity': PASS}
    assert fields['revenue'].value_cr is None            # held — first-seen, by design
    assert llm.current_metrics().calls >= 1


def test_emit_path_under_explicit_loosening(monkeypatch):
    # With the explicit loosening on, an all-PASS row emits the CF1-collapsed value:
    # revenue Σ(3 months)=33, ebitda Σ=9. cash/headcount have no identity → held.
    monkeypatch.setenv('PREINGEST3_EMIT_UNPROVEN', '1')
    fields = _gap_fields()
    _fill(fields, _ident('cfp-emit'), _located(_ROW))
    assert fields['revenue'].value_cr == 33 and not fields['revenue'].held
    # revenue is a 3-month SUM → universal provenance: no single value cell (cell=''),
    # the three summed value cells are cited, and the label cell is preserved in note.
    assert fields['revenue'].provenance.cell == ''
    assert fields['revenue'].provenance.derived_from == ['B2', 'C2', 'D2']
    assert 'A2' in (fields['revenue'].provenance.note or '')
    assert fields['ebitda'].value_cr == 9 and not fields['ebitda'].held
    assert fields['cash'].value_cr is None
    assert fields['headcount'].value_cr is None


def test_REJECTS_wrong_row(monkeypatch):
    # ADVERSARIAL — revenue pointed at the EBITDA row (a real, plausible line). Even
    # with emit ON, the re-read label ('EBITDA' ≠ revenue) and the broken identity
    # dissent → revenue HELD; the correctly-located ebitda still emits.
    monkeypatch.setenv('PREINGEST3_EMIT_UNPROVEN', '1')
    wrong = dict(_ROW, revenue=6)                        # revenue → the EBITDA row
    # the model claims it read 'EBITDA' on that row (which is what's actually there)
    fields = _gap_fields()

    def prov(prompt, **kw):
        items = ', '.join(
            f'{{"concept":"{c}","form":"direct","row":{r},'
            f'"row_label":"{("EBITDA" if c=="revenue" else _LABEL.get(c,c))}","rows":[]}}'
            for c, r in wrong.items())
        return types.SimpleNamespace(text='{"located":[' + items + ']}')

    diags = _fill(fields, _ident('cfp-adv'), prov)
    rev = _diag(diags, 'revenue')
    assert 'fail' in rev['signals'].values()             # a signal dissented
    assert fields['revenue'].value_cr is None            # wrong row NOT emitted
    assert fields['ebitda'].value_cr == 9 and not fields['ebitda'].held


def test_cached_wrong_row_still_fails_closed(monkeypatch):
    # FREEZE-AN-ERROR (D2). Emit ON, so a correct row WOULD emit. Run 1 caches a wrong
    # row; run 2's provider raises if touched → cache hit proven → STILL held (the
    # cache froze the row, and triangulation re-ran on it).
    monkeypatch.setenv('PREINGEST3_EMIT_UNPROVEN', '1')
    ident = _ident('cfp-cache')
    _fill(_gap_fields(), ident, _located(dict(_ROW, revenue=6)))   # run 1 → caches wrong row

    def _boom(prompt, **kw):
        raise AssertionError('cache MISS — the frozen row was not reused')
    llm.new_metrics()
    fields2 = _gap_fields()
    _fill(fields2, ident, _boom)                                   # run 2 → cache hit
    assert llm.current_metrics().calls == 0
    assert fields2['revenue'].value_cr is None


def _write(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def test_deterministic_first_model_not_called_when_code_succeeds():
    def _boom(prompt, **kw):
        raise AssertionError('model provider must NOT be called when code succeeds')
    llm.set_model_provider(_boom)
    with tempfile.TemporaryDirectory() as d:
        rows = [['Particulars (INR Cr)', 'Apr-25', 'May-25', 'Jun-25'],
                ['Revenue', 10, 11, 12], ['COGS', 4, 4, 5],
                ['Gross Profit', 6, 7, 7], ['EBITDA', 2, 2, 3],
                ['Cash', 5, 6, 7], ['Headcount', 20, 21, 22]]
        p = _write(os.path.join(d, 'clean.xlsx'), rows)
        det = extract.extract_company('clean', p, rate_card=_RC, entity='CleanCo')
        assert all(det.fields[c].value_cr is not None for c in extract.MIS_CONCEPTS), \
            'precondition: deterministic path must fully resolve this file'
        llm.new_metrics()
        rec = extract.extract_company('clean', p, rate_card=_RC, entity='CleanCo', use_model=True)
    assert all(rec.fields[c].value_cr is not None for c in extract.MIS_CONCEPTS)
    assert llm.current_metrics().calls == 0


def test_budget_exhaustion_is_a_disclosed_hold(monkeypatch):
    monkeypatch.setattr(extract, '_CALL_BUDGET', 0)
    llm.current_metrics().calls = 1
    fields = _gap_fields()
    _fill(fields, _ident('cfp-budget'), _located(_ROW))
    for c in extract.MIS_CONCEPTS:
        assert fields[c].held and 'call-budget' in (fields[c].hold_reason or '')


def test_locator_is_order_invariant():
    # RECALL must not depend on request order (the cash-order-sensitivity bug).
    # request_concepts canonicalises the target set, so the same set yields the same
    # prompt AND the same cache key no matter how the caller orders the concepts.
    from backend.dataimport.preingest3 import locator
    from backend.dataimport.preingest3.statements import Statement
    assert (locator.request_concepts(['cash', 'revenue', 'ebitda'])
            == locator.request_concepts(['ebitda', 'cash', 'revenue']))

    grid = {'P&L': [list(r) for r in ROWS]}
    st = Statement('P&L', 1, 7, 0, 0)
    seen = []

    def rec_provider(prompt, **kw):
        seen.append(prompt)
        return types.SimpleNamespace(text='{"located":[]}')
    llm.set_model_provider(rec_provider)

    # two permutations, DIFFERENT content_fp so both genuinely call the provider —
    # the model INPUT (prompt sequence: main + recall-retry) must be byte-identical.
    locator.locate_rows(st, grid, ['cash', 'revenue', 'ebitda'], content_fp='ord-A')
    run_a = list(seen); seen.clear()
    locator.locate_rows(st, grid, ['ebitda', 'revenue', 'cash'], content_fp='ord-B')
    run_b = list(seen)
    assert run_a and run_a == run_b, 'permuted request produced a different model prompt'

    # same content_fp + permuted order → cache HIT (order-invariant cache key).
    llm.new_metrics()
    locator.locate_rows(st, grid, ['cash', 'revenue', 'ebitda'], content_fp='ord-same')
    assert llm.current_metrics().calls >= 1
    llm.new_metrics()
    locator.locate_rows(st, grid, ['revenue', 'ebitda', 'cash'], content_fp='ord-same')
    assert llm.current_metrics().calls == 0, 'permuted re-run missed cache → key is order-sensitive'


def test_recall_missing_concept_is_disclosed_not_silently_dropped():
    # completeness: a concept the model SILENTLY DROPS (never mentions) is re-requested
    # once, and if still undetermined it is HELD with a disclosed recall reason and a
    # diagnostic — never silently lost.
    def drop_cash(prompt, **kw):
        items = ', '.join(
            f'{{"concept":"{c}","form":"direct","row":{r},"row_label":"{_LABEL.get(c, c)}","rows":[]}}'
            for c, r in _ROW.items() if c != 'cash')          # cash omitted on BOTH calls
        return types.SimpleNamespace(text='{"located":[' + items + ']}')

    fields = _gap_fields()
    diags = _fill(fields, _ident('cfp-recall'), drop_cash)
    cash = _diag(diags, 'cash')
    assert cash.get('recall') == 'undetermined' and cash['row'] is None
    assert fields['cash'].held and 'recall' in (fields['cash'].hold_reason or '')


def test_stock_bound_to_flow_label_is_held(monkeypatch):
    # STOCK-vs-FLOW guard (U5). Cash is a STOCK; a locate onto a period FLOW line
    # ("Cash Collected") ties within-column and would slip past triangulation. Even
    # with emit ON, a flow-labelled cash is HELD with a disclosed reason.
    monkeypatch.setenv('PREINGEST3_EMIT_UNPROVEN', '1')

    def flow_cash(prompt, **kw):
        items = ', '.join(
            f'{{"concept":"{c}","form":"direct","row":{r},'
            f'"row_label":"{("Cash Collected" if c == "cash" else _LABEL.get(c, c))}","rows":[]}}'
            for c, r in _ROW.items())
        return types.SimpleNamespace(text='{"located":[' + items + ']}')

    fields = _gap_fields()
    _fill(fields, _ident('cfp-flow'), flow_cash)
    assert fields['cash'].held and fields['cash'].value_cr is None
    assert 'flow-labelled' in (fields['cash'].hold_reason or '')


def test_stock_on_a_stock_label_is_not_flow_held(monkeypatch):
    # the guard must NOT false-fire: cash on its real stock label ('Cash') is never
    # held FOR THE FLOW reason (it may still hold for want of an identity — separate).
    monkeypatch.setenv('PREINGEST3_EMIT_UNPROVEN', '1')
    fields = _gap_fields()
    _fill(fields, _ident('cfp-stocklbl'), _located(_ROW))
    assert 'flow-labelled' not in (fields['cash'].hold_reason or '')


def test_cash_inherits_closing_cash_when_target_absent(monkeypatch):
    # CONCEPT EQUIVALENCE (U5). The Hubler-live reality: the model routes the closing
    # balance to `closing_cash` and marks generic `cash` ABSENT (correctly — the bare
    # label isn't 'cash'). The target must inherit the anchor's located row so the BS
    # cash stock is captured, verified, never silently 0.
    monkeypatch.setenv('PREINGEST3_EMIT_UNPROVEN', '1')

    def route_to_closing(prompt, **kw):
        loc = [f'{{"concept":"{c}","form":"direct","row":{r},"row_label":"{_LABEL.get(c, c)}","rows":[]}}'
               for c, r in _ROW.items() if c != 'cash']
        loc.append('{"concept":"cash","form":"absent","row":null,"row_label":"","rows":[]}')
        loc.append(f'{{"concept":"closing_cash","form":"direct","row":{_ROW["cash"]},'
                   f'"row_label":"Closing balance","rows":[]}}')
        return types.SimpleNamespace(text='{"located":[' + ', '.join(loc) + ']}')

    fields = _gap_fields()
    diags = _fill(fields, _ident('cfp-equiv'), route_to_closing)
    cash = _diag(diags, 'cash')
    assert cash['row'] == _ROW['cash'], 'cash did not inherit closing_cash row'
    assert cash['native'] is not None and cash.get('recall') is None    # bound & collapsed, not a recall hold


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-q']))
