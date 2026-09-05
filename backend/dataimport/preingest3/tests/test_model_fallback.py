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

from backend.dataimport.preingest3 import extract, gate, llm, templates
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


def _fill(fields, ident, provider=None, prof=None):
    if provider is not None:
        llm.set_model_provider(provider)
    return extract._model_fill(prof or _prof(), ident, list(extract.MIS_CONCEPTS),
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


def test_locator_only_model_volunteered_number_is_never_emitted(monkeypatch):
    # PART-1 CONTRACT (locator-only, reddening). The model returns LOCATIONS; if it ALSO volunteers
    # a number at the CORRECT location ("row 2 ... value=9999999"), that number must be IGNORED and
    # the CODE-READ cell value emitted instead. locator._parse_located_rows whitelists only
    # concept/form/row/rows/row_label — there is no field a volunteered number can travel in, and
    # RowRecord has no value slot — so this locks the invariant end-to-end through the emit path.
    monkeypatch.setenv('PREINGEST3_EMIT_UNPROVEN', '1')
    fields = _gap_fields()

    def prov(prompt, **kw):
        # correct rows, but each also carries a BOGUS volunteered number the code must discard
        items = ', '.join(
            f'{{"concept":"{c}","form":"direct","row":{r},"row_label":"{_LABEL.get(c, c)}",'
            f'"rows":[],"value":9999999,"amount":9999999}}'
            for c, r in _ROW.items())
        return types.SimpleNamespace(text='{"located":[' + items + ']}')

    _fill(fields, _ident('cfp-locator-only'), prov)
    # emitted values are the CODE-READ collapse (revenue Σ=33, ebitda Σ=9), NEVER the model's 9999999
    assert fields['revenue'].value_cr == 33 and not fields['revenue'].held
    assert fields['ebitda'].value_cr == 9
    for c in extract.MIS_CONCEPTS:
        f = fields.get(c)
        if isinstance(f, Figure) and f.value_cr is not None:
            assert f.value_cr != 9999999, f'{c} emitted the model-volunteered number — locator-only broken'


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


# ══════════════════════════════════════════════════════════════════════════════════════════════
# U3 — LAYOUT TEMPLATE REGISTRY (cache A, 3b): a known layout reuses the model's proven row LOCATIONS
# and skips the locate call; every hit re-reads THIS file's cells + re-runs the 3 signals, so a
# stale/wrong/cross-tenant location can never emit a wrong number. Model-off never touches it.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _scaled_prof():
    """SAME layout as _prof() (revenue row 2, cogs row 3, …) but DIFFERENT numbers; the identities still
    hold on the sums (63-26=37, 37-22=15) so a hit re-verifies. Proves the registry serves LOCATIONS, not
    values — run 2 reads ITS OWN numbers off the same rows."""
    sheet = types.SimpleNamespace(sheet='P&L', currency_hints=['INR'])
    # PER-COLUMN consistent (the identity signal checks the ref column, not just the sums):
    #   each month: revenue-cogs=gross_profit AND gross_profit-opex=ebitda.
    rows = [
        ['Particulars (INR Cr)', 'Apr-25', 'May-25', 'Jun-25'],
        ['Revenue', 20, 21, 22],           # Σ=63
        ['COGS', 8, 9, 10],                # Σ=27
        ['Gross Profit', 12, 12, 12],      # Σ=36   (20-8, 21-9, 22-10)
        ['OPEX', 6, 7, 8],                 # Σ=21
        ['EBITDA', 6, 5, 4],               # Σ=15   (12-6, 12-7, 12-8)
        ['Cash', 5, 6, 7],
        ['Headcount', 20, 21, 22],
    ]
    return {'sheets': [sheet], 'grid': {'P&L': [list(r) for r in rows]}}


def _imperfect_prof():
    """SAME layout + numbers as _prof() but the cogs row is SOFT-labelled ('Total COGS' → lexicon
    'contains', not 'exact') — a located-but-IMPERFECT INTERMEDIATE: arithmetically consistent every
    column (revenue−cogs=gross_profit, gross_profit−opex=ebitda) yet not all-PASS on its own 3 signals.
    On a MISS it is located, so revenue−cogs=gross_profit FIRES and revenue's identity PASSES → revenue
    emits. Under put-on-PASS it would NOT be cached (label SOFT), so a HIT that dropped it would silently
    hold revenue though the miss emitted it — the hit≠miss coverage asymmetry the capstone proves gone."""
    sheet = types.SimpleNamespace(sheet='P&L', currency_hints=['INR'])
    rows = [
        ['Particulars (INR Cr)', 'Apr-25', 'May-25', 'Jun-25'],
        ['Revenue', 10, 11, 12],           # Σ=33
        ['Total COGS', 4, 4, 5],           # Σ=13  ← SOFT label (contains) = the imperfect intermediate
        ['Gross Profit', 6, 7, 7],         # Σ=20  (10-4, 11-4, 12-5)
        ['OPEX', 3, 4, 4],                 # Σ=11
        ['EBITDA', 3, 3, 3],               # Σ=9   (6-3, 7-4, 7-4)
        ['Cash', 5, 6, 7],
        ['Headcount', 20, 21, 22],
    ]
    return {'sheets': [sheet], 'grid': {'P&L': [list(r) for r in rows]}}


def _emitted_cir(fields):
    """The observable emit outcome per MIS target: (value_cr, held). hit==miss means this is identical."""
    return {c: (fields[c].value_cr, bool(fields[c].held)) for c in extract.MIS_CONCEPTS}


def _cached_concepts(layout_fp):
    import json
    with open(templates._STORE) as fh:
        data = json.load(fh)
    tpl = data.get('layouts', {}).get(layout_fp, {})
    return {r['concept'] for recs in tpl.get('statements', {}).values() for r in recs}


def _corrupt_cached_row(layout_fp, concept, to_row0):
    import json
    with open(templates._STORE) as fh:
        data = json.load(fh)
    for recs in data['layouts'][layout_fp]['statements'].values():
        for r in recs:
            if r['concept'] == concept:
                r['row'] = to_row0                          # point it at a DIFFERENT concept's row
    with open(templates._STORE, 'w') as fh:
        json.dump(data, fh)


# ── ACCEPTANCE TEST 3 + happy 2: a known layout SKIPS the locate call, and re-reads THIS file's numbers ──
def test_templates_hit_skips_locate_and_reads_this_files_numbers(monkeypatch):
    monkeypatch.setenv('PREINGEST3_EMIT_UNPROVEN', '1')       # emit ON, so a re-verified hit emits
    ident = _ident('cfp-tmpl')
    _fill(_gap_fields(), ident, _located(_ROW))              # run 1 (MISS) → put-on-PASS freezes the rows
    assert templates.has_layout(ident.layout_fp)             # layout learned

    def _boom(prompt, **kw):
        raise AssertionError('registry HIT expected — the model must NOT be called')

    llm.new_metrics()
    f2 = _gap_fields()
    _fill(f2, ident, _boom, prof=_scaled_prof())            # run 2 (HIT), different numbers, same layout
    assert llm.current_metrics().calls == 0                  # SKIPS locate — the SLA lever (cost per layout, not file)
    assert f2['revenue'].value_cr == 63 and not f2['revenue'].held   # THIS file's numbers, re-read off the cached rows
    assert f2['ebitda'].value_cr == 15


# ── ACCEPTANCE TEST 2 (REDDENING — the load-bearing safety proof): a WRONG cached location is caught by
#    the re-read + re-verify and HELD, never emitted. This is what makes the non-org-scoped, cross-tenant-
#    shared registry safe: it serves a location, and the location is re-proven against THIS file every hit. ──
def test_templates_wrong_cached_location_is_caught_and_held(monkeypatch):
    monkeypatch.setenv('PREINGEST3_EMIT_UNPROVEN', '1')       # emit ON, so a CORRECT hit WOULD emit
    ident = _ident('cfp-wrongloc')
    _fill(_gap_fields(), ident, _located(_ROW))              # run 1 → correct rows cached
    _corrupt_cached_row(ident.layout_fp, 'revenue', _ROW['ebitda'] - 1)   # revenue → the EBITDA row (0-based)

    def _boom(prompt, **kw):
        raise AssertionError('registry HIT expected — the model must NOT be called')

    llm.new_metrics()
    f2 = _gap_fields()
    _fill(f2, ident, _boom)                                  # run 2 → hit the CORRUPTED location
    assert llm.current_metrics().calls == 0                  # served from the (corrupted) registry, no model call
    # THE SAFETY PROOF: the re-read label ('EBITDA' ≠ revenue) + broken identity dissent → revenue is NOT
    # emitted — value_cr stays None, NEVER the EBITDA number the corrupted location points at. A shared /
    # stale / cross-tenant location can never ship a wrong figure because the re-verify runs every hit.
    assert f2['revenue'].value_cr is None                    # wrong location caught → NOT emitted (no wrong number)
    assert f2['ebitda'].value_cr == 9                        # a CORRECT cached row still emits — only the bad one is caught


# ── CAPSTONE (before 3d): a cache HIT must emit the IDENTICAL CIR to a MISS — same figures emitted, same
#    held — NOT merely "reads correct values". The load-bearing case is a located-but-imperfect INTERMEDIATE
#    (SOFT-labelled cogs): on a miss it is present so revenue−cogs=gross_profit fires and revenue emits; a hit
#    that dropped it (put-on-PASS caches only all-PASS rows) would silently HOLD revenue — fail-closed, but
#    hit≠miss. Equality here proves the registry is a TRANSPARENT SUBSTITUTE for locate_rows, not just safe. ──
def test_templates_hit_equals_miss_including_imperfect_intermediate(monkeypatch):
    monkeypatch.setenv('PREINGEST3_EMIT_UNPROVEN', '1')      # emit ON so equality is observable on the emits
    prof = _imperfect_prof()

    f_miss = _gap_fields()
    _fill(f_miss, _ident('cfp-hiteqmiss'), _located(_ROW), prof=prof)   # MISS → locates the FULL chain, populates
    miss_cir = _emitted_cir(f_miss)
    assert miss_cir['revenue'] == (33, False)               # precondition: the miss DID emit revenue VIA the intermediate
    assert miss_cir['ebitda'] == (9, False)

    def _boom(prompt, **kw):
        raise AssertionError('registry HIT expected — the model must NOT be called')

    llm.new_metrics()
    f_hit = _gap_fields()
    _fill(f_hit, _ident('cfp-hiteqmiss'), _boom, prof=prof)  # HIT → SAME file, served from the registry
    assert llm.current_metrics().calls == 0                  # genuinely a hit (locate skipped)
    assert _emitted_cir(f_hit) == miss_cir                   # HIT == MISS, EXACTLY — revenue included, not just held-safe


# ── put-on-PASS is REPLACED by cache-the-full-located-set (§ hit==miss): the registry mirrors what
#    locate_rows returned, and re-verify-on-hit is the SOLE safety layer. A wrong-row locate is therefore
#    cached but can never EMIT — it is re-read + re-triangulated on the hit and HELD (proven by
#    test_cached_wrong_row_still_fails_closed / D2). This test pins the new put contract: the full located
#    chain is frozen so a hit re-verifies it whole; safety is the re-verify, not a put-time filter. ──
def test_templates_put_caches_full_located_chain_for_reverify(monkeypatch):
    monkeypatch.setenv('PREINGEST3_EMIT_UNPROVEN', '1')
    ident = _ident('cfp-putfull')
    _fill(_gap_fields(), ident, _located(_ROW), prof=_imperfect_prof())   # cogs is SOFT-labelled (not all-PASS)
    cached = _cached_concepts(ident.layout_fp)
    assert 'cogs' in cached                                  # the imperfect intermediate IS cached (so a hit re-verifies the whole chain)
    assert {'revenue', 'gross_profit', 'opex', 'ebitda'} <= cached   # the full located chain is frozen, not just all-PASS rows


# ── self-erasing: a logic-version change invalidates the whole store (never freezes yesterday's locate) ──
def test_templates_store_self_erases_on_logic_version(monkeypatch):
    from backend.dataimport.preingest3.statements import Statement
    st = Statement('P&L', 1, 7, header_row=0, label_col=0)
    rr = types.SimpleNamespace(concept='revenue', form='direct', row=1, operand_rows=[], row_label='Revenue')
    templates.put_statement_rows('lfp-x', st, [rr])
    assert templates.get_statement_rows('lfp-x', st) is not None       # present under the current logic version
    monkeypatch.setattr(templates, 'net_logic_version', lambda: 'lv_DIFFERENT')   # the name templates binds
    assert templates.get_statement_rows('lfp-x', st) is None           # version skew → whole store treated empty
    assert templates.has_layout('lfp-x') is False


# ══════════════════════════════════════════════════════════════════════════════════════════════
# PROBE ORDER (3d efficiency): the model path visits the deterministic BEST MIS statement sheet FIRST,
# then every other sheet in profile order. REORDER, never RESTRICT — the fallback stays intact so a gap
# off the best sheet is still found. Cuts the cold model-call WASTE of probing decoy tabs before the real
# statement (a monthly report can carry 50+ statement-shaped tabs); zero coverage change.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _sh(n):
    return types.SimpleNamespace(sheet=n, currency_hints=['INR'])


_DECOY = [['Item', 'Apr-25', 'May-25', 'Jun-25'], ['Widgets', 1, 2, 3], ['Gadgets', 4, 5, 6], ['Gizmos', 7, 8, 9]]


def _multi_sheet_prof():
    """3 sheets in profile order [D1, D2, PL] — PL (the real P&L) is LAST; the decoys carry a time axis but
    NO MIS concepts, so _best_sheet ranks PL the best. Profile-order probing would hit D1, D2 before PL."""
    return {'sheets': [_sh('D1'), _sh('D2'), _sh('PL')],
            'grid': {'D1': [list(r) for r in _DECOY], 'D2': [list(r) for r in _DECOY],
                     'PL': [list(r) for r in ROWS]}}


def test_model_probe_order_is_best_sheet_first_reorder_not_restrict():
    order = [s.sheet for s in extract._model_probe_order(_multi_sheet_prof())]
    assert order[0] == 'PL'                     # deterministic best sheet FIRST (profile order puts PL last)
    assert order == ['PL', 'D1', 'D2']          # REORDER not RESTRICT: every sheet present, the rest keep profile order


def test_model_probe_order_no_best_sheet_keeps_profile_order():
    # a workbook with NO MIS statement (all decoys) → no deterministic best → probe in profile order (unchanged).
    prof = {'sheets': [_sh('D1'), _sh('D2')], 'grid': {'D1': [list(r) for r in _DECOY], 'D2': [list(r) for r in _DECOY]}}
    assert [s.sheet for s in extract._model_probe_order(prof)] == ['D1', 'D2']


def test_model_fill_probes_best_sheet_first_and_early_breaks(monkeypatch):
    # The payoff: with the best sheet FIRST, gaps that resolve there resolve on probe #1 → early break →
    # the decoy tabs are NEVER probed (profile order would have spent a model call on D1 and D2 first).
    monkeypatch.setenv('PREINGEST3_EMIT_UNPROVEN', '1')
    from backend.dataimport.preingest3 import locator
    seen = []
    real = locator.locate_rows

    def spy(stmt, grid, concepts, **kw):
        seen.append(stmt.sheet)
        if stmt.sheet == 'PL':
            return real(stmt, grid, concepts, **kw)          # the real locate on the good sheet (via the provider)
        return {'records': [], 'missing': list(concepts), 'error': None}   # decoys locate nothing
    monkeypatch.setattr(locator, 'locate_rows', spy)
    llm.set_model_provider(_located(_ROW)); llm.new_metrics()

    fields = _gap_fields()
    extract._model_fill(_multi_sheet_prof(), _ident('cfp-probe'), ['revenue', 'ebitda'],
                        entity='T', domicile=None, anchor_cr=None, rate_card=_RC,
                        fields=fields, source_label='t.xlsx')
    assert seen == ['PL']                        # best sheet first → both gaps resolve → early break; decoys NEVER probed
    assert fields['revenue'].value_cr == 33 and not fields['revenue'].held    # coverage preserved (resolved on probe #1)
    assert fields['ebitda'].value_cr == 9 and not fields['ebitda'].held


def test_model_fill_falls_back_to_other_sheets_for_gaps_off_the_best_sheet(monkeypatch):
    # REORDER not RESTRICT (the load-bearing coverage invariant): a gap that lives OFF the best sheet is STILL
    # reached and LOCATED. Best sheet 'PL' resolves revenue; 'cash' lives only on a later 'BS' tab → the
    # fallback must reach 'BS' and locate cash there. Starting at the best sheet must never orphan an off-sheet
    # gap. (cash has no identity here so it won't EMIT — the invariant under test is that the fallback REACHES
    # and LOCATES it, i.e. coverage is not narrowed by the reorder.)
    monkeypatch.setenv('PREINGEST3_EMIT_UNPROVEN', '1')
    from backend.dataimport.preingest3 import locator
    bs = [['Particulars (INR Cr)', 'Apr-25', 'May-25', 'Jun-25'],
          ['Trade Receivables', 30, 33, 36], ['Inventory', 20, 22, 24], ['Cash', 5, 6, 7]]
    prof = {'sheets': [_sh('PL'), _sh('BS')],
            'grid': {'PL': [list(r) for r in ROWS], 'BS': [list(r) for r in bs]}}
    seen = []

    def spy(stmt, grid, concepts, **kw):
        seen.append(stmt.sheet)
        if stmt.sheet == 'PL':
            return {'records': [locator.RowRecord(concept='revenue', form='direct', row=1, operand_rows=[], row_label='Revenue')],
                    'missing': [c for c in concepts if c != 'revenue'], 'error': None}
        if stmt.sheet == 'BS':
            return {'records': [locator.RowRecord(concept='cash', form='direct', row=3, operand_rows=[], row_label='Cash')],
                    'missing': [c for c in concepts if c != 'cash'], 'error': None}
        return {'records': [], 'missing': list(concepts), 'error': None}
    monkeypatch.setattr(locator, 'locate_rows', spy)
    llm.set_model_provider(lambda *a, **k: types.SimpleNamespace(text='{"located":[]}')); llm.new_metrics()

    fields = _gap_fields()
    diags = extract._model_fill(prof, _ident('cfp-fallback'), ['revenue', 'cash'],
                                entity='T', domicile=None, anchor_cr=None, rate_card=_RC,
                                fields=fields, source_label='t.xlsx')
    assert seen == ['PL', 'BS']                   # best sheet first, THEN the fallback reached the off-best sheet
    assert next(d for d in diags if d['concept'] == 'revenue')['sheet'] == 'PL'   # best-sheet gap located on probe #1
    assert next(d for d in diags if d['concept'] == 'cash')['sheet'] == 'BS'      # off-best gap LOCATED by the fallback — coverage preserved


# ── validation: a stored row outside the statement's row range is rejected (never trusted blindly) ──
def test_templates_get_rejects_out_of_range_row():
    from backend.dataimport.preingest3.statements import Statement
    st = Statement('P&L', 1, 7, header_row=0, label_col=0)
    good = types.SimpleNamespace(concept='revenue', form='direct', row=2, operand_rows=[], row_label='Revenue')
    bad = types.SimpleNamespace(concept='ebitda', form='direct', row=99, operand_rows=[], row_label='EBITDA')
    templates.put_statement_rows('lfp-oor', st, [good, bad])
    got = templates.get_statement_rows('lfp-oor', st)
    assert {r['concept'] for r in got} == {'revenue'}                  # the out-of-range ebitda row is dropped


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-q']))
