"""Step 4 EMIT-WIRING — the whole-file finder emit path (extract._model_find_across_file), proven
OFFLINE with a dual-prompt fake provider (finder + per-statement locate_rows). This is the
never-a-wrong-number FRONTIER: the finder surfaces the same concept at many scopes plus real noise,
and the emit path must reject the noise and resolve the scopes to ONE consolidated figure or HOLD.

Gates locked here (reddening where new):
  • G1  anchors relocate — per-statement anchors re-enter via locate_rows, bounded to the statement,
        and feed triangulation; neutralise locate_rows → the identity chain breaks → HELD.
  • G2  noise rejection — a 'Total Other Income' row (non-operating) and a non-statement 'Main' check
        cell ('Chk Total Sales' = a value + the word 'sales') are rejected, never emitted as revenue;
        neutralising each guard lets the noise in and the real figure no longer emits (RED).
  • G3  multiscope — Summary(33) vs Div-A(20) vs Div-B(13): the Σ-identity consolidated (33) emits;
        a division figure NEVER emits; when the divisions don't roll up, or only divisions exist,
        → HOLD; exactly ONE Figure per concept (never N).
  • G4  cash period alignment — BS 'current' col equals the cash-flow PRIOR col; aligned by period-key
        they land in different buckets (no false conflict), not conflated by column position.
  • G5  locator-only + triangulate + hold — the emitted value is the CODE-READ cell (never a model
        number); an untriangulated scope never wins the emit.

Fake-provider, no live Gemini, deterministic and hermetic (mirrors test_model_fallback)."""
import re
import types

import pytest

from backend.dataimport.preingest3 import extract, gate, llm, locator
from backend.dataimport.preingest3.cir import Figure, Provenance
from backend.dataimport.preingest3.ratecard import default_inr_card

_RC = default_inr_card('2026-06-30')
_AX = ['Particulars (INR Cr)', 'Apr-25', 'May-25', 'Jun-25']


def _sheet(title, datarows, axis=_AX):
    return [[title, '', '', ''], list(axis)] + [list(r) for r in datarows]


# Consolidated P&L: Σrev 33 = Σcogs 13 + Σgp 20; Σgp 20 − Σopex 11 = Σebitda 9. 'Total Other Income'
# (Σ=24) is the non-operating noise row the finder ALSO tags revenue-ish.
_SUMMARY = _sheet('Statement of Profit and Loss', [
    ['Revenue', 11, 11, 11],            # 1-based row 3   Σ=33
    ['COGS', 4, 4, 5],                  # row 4           Σ=13
    ['Gross Profit', 7, 7, 6],          # row 5           Σ=20
    ['OPEX', 3, 4, 4],                  # row 6           Σ=11
    ['EBITDA', 4, 3, 2],                # row 7           Σ=9
    ['Cash', 5, 6, 7],                  # row 8           latest 7
    ['Headcount', 20, 21, 22],          # row 9           latest 22
    ['Total Other Income', 8, 8, 8],    # row 10          Σ=24  (G2)
])
# divisions need >2 data rows to form a statement region (degenerate-region guard); revenue at row 3.
_DIV_A = _sheet('Income Statement', [['Revenue', 6, 7, 7], ['COGS', 2, 2, 3], ['EBITDA', 2, 2, 1]])  # Σrev=20
_DIV_B = _sheet('Income Statement', [['Revenue', 4, 4, 5], ['COGS', 1, 1, 2], ['EBITDA', 1, 1, 1]])  # Σrev=13
# Non-statement working tab: a period axis + the word 'sales' in a CHECK cell that carries a value.
_MAIN = _sheet('Working file', [['Chk Total Sales', 461, 300, 346], ['Note A', 1, 1, 1], ['Note B', 2, 2, 2]])

_SUM_CHAIN = {'revenue': (3, 'Revenue'), 'cogs': (4, 'COGS'), 'gross_profit': (5, 'Gross Profit'),
              'opex': (6, 'OPEX'), 'ebitda': (7, 'EBITDA'), 'cash': (8, 'Cash'),
              'headcount': (9, 'Headcount')}


def _prof(sheets: dict):
    return {'sheets': [types.SimpleNamespace(sheet=n, currency_hints=['INR']) for n in sheets],
            'grid': {n: [list(r) for r in rows] for n, rows in sheets.items()}}


def _ident(fp='cfp-emit'):
    return types.SimpleNamespace(content_fp=fp, layout_fp='lfp-' + fp)


def _gap_fields():
    pv = Provenance(source_file='t.xlsx', content_fingerprint='cfp-emit', sheet='', cell='', row_label='')
    f = {'company': 'TestCo'}
    for c in extract.MIS_CONCEPTS:
        f[c] = Figure(c, None, None, pv, gap=True)
    return f


def _reqs(prompt):
    out, inb = [], False
    for line in prompt.splitlines():
        if line.startswith('CONCEPTS TO LOCATE'):
            inb = True
            continue
        if line.startswith('STATEMENTS') or line.startswith('ROW LABELS'):
            break
        if inb and line.strip().startswith('- '):
            out.append(line.strip()[2:].split(' (')[0].strip())
    return out


def _dual_provider(finder_map, rows_map):
    """finder_map = {concept: [(sheet,row1based,label)]} for the whole-file finder; rows_map =
    {sheet: {concept:(row1based,label)}} for per-statement locate_rows (the relocated anchors). Every
    requested-but-unreturned concept is marked absent so no recall retry fires."""
    def provider(prompt, **kw):
        if 'STATEMENTS (' in prompt:                       # whole-file finder
            items, have = [], set()
            for c, locs in finder_map.items():
                for sheet, row, label in locs:
                    items.append(f'{{"concept":"{c}","sheet":"{sheet}","form":"direct","row":{row},'
                                 f'"row_label":"{label}","rows":[]}}')
                    have.add(c)
            for c in _reqs(prompt):
                if c not in have:
                    items.append(f'{{"concept":"{c}","form":"absent","sheet":"","row":null,"rows":[]}}')
            return types.SimpleNamespace(text='{"located":[' + ', '.join(items) + ']}')
        m = re.search(r'STATEMENT: sheet (.*?), rows', prompt)   # per-statement locate_rows
        rmap = rows_map.get(m.group(1) if m else '', {})
        items, have = [], set()
        for c, (row, label) in rmap.items():
            items.append(f'{{"concept":"{c}","form":"direct","row":{row},"row_label":"{label}","rows":[]}}')
            have.add(c)
        for c in _reqs(prompt):
            if c not in have:
                items.append(f'{{"concept":"{c}","form":"absent","row":null,"rows":[]}}')
        return types.SimpleNamespace(text='{"located":[' + ', '.join(items) + ']}')
    return provider


def _find(fields, ident, provider, prof, require_bound=False, boundary_verified=True):
    llm.set_model_provider(provider)
    return extract._model_find_across_file(prof, ident, list(extract.MIS_CONCEPTS), entity='TestCo',
                                           domicile=None, anchor_cr=None, rate_card=_RC, fields=fields,
                                           source_label='t.xlsx', require_bound=require_bound,
                                           boundary_verified=boundary_verified)


def _diag(diags, concept):
    return next((d for d in diags if d.get('concept') == concept and 'disposition' in d), None)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    saved = llm._MODEL_PROVIDER
    monkeypatch.setattr(llm, '_rl_sleep', lambda *a, **k: None)
    monkeypatch.setattr(llm, '_CACHE_DIR', str(tmp_path / 'calls'))
    monkeypatch.setattr(gate, '_RELI_PATH', str(tmp_path / 'reli.json'))
    monkeypatch.setattr(gate, '_AUDIT_PATH', str(tmp_path / 'audit.json'))
    monkeypatch.setenv('PREINGEST3_EMIT_UNPROVEN', '1')     # let an all-PASS verdict emit (else first-seen holds)
    llm.new_metrics()
    yield
    llm._MODEL_PROVIDER = saved


# ── G3 + G1 + G5: multiscope resolves to the consolidated; the emitted value is the code-read cell ──
def test_g3_multiscope_resolves_to_consolidated_and_emits_code_read_value():
    prof = _prof({'Summary': _SUMMARY, 'Div A': _DIV_A, 'Div B': _DIV_B})
    finder_map = {'revenue': [('Summary', 3, 'Revenue'), ('Div A', 3, 'Revenue'), ('Div B', 3, 'Revenue')],
                  'ebitda': [('Summary', 7, 'EBITDA')], 'cash': [('Summary', 8, 'Cash')],
                  'headcount': [('Summary', 9, 'Headcount')]}
    fields = _gap_fields()
    diags = _find(fields, _ident(), _dual_provider(finder_map, {'Summary': _SUM_CHAIN}), prof)
    rev = fields['revenue']
    assert rev.value_cr == 33 and not rev.held, 'the Σ-consolidated (33) must emit'
    assert rev.provenance.sheet == 'Summary', 'the emit must come from the consolidated scope, never a division'
    d = _diag(diags, 'revenue')
    from decimal import Decimal
    assert d['disposition'] == 'multiscope' and Decimal(d['value_cr']) == 33
    # G5 locator-only: value is the CODE-READ Σ of the three month cells, not any model-volunteered number
    assert rev.provenance.derived_from == ['B3', 'C3', 'D3']


def test_g3_division_figure_is_never_emitted_as_company_value():
    # divisions that DON'T roll up (20 + 20 ≠ 33) → no unique Σ satisfier → HOLD; never a division.
    prof = _prof({'Summary': _SUMMARY, 'Div A': _DIV_A,
                  'Div B': _sheet('Income Statement', [['Revenue', 7, 7, 6]])})   # Σ=20, breaks the roll-up
    finder_map = {'revenue': [('Summary', 3, 'Revenue'), ('Div A', 3, 'Revenue'), ('Div B', 3, 'Revenue')]}
    fields = _gap_fields()
    _find(fields, _ident('cfp-noroll'), _dual_provider(finder_map, {'Summary': _SUM_CHAIN}), prof)
    assert fields['revenue'].held and fields['revenue'].value_cr is None
    assert fields['revenue'].value_cr != 20


def test_g3_divisions_only_no_consolidated_holds():
    prof = _prof({'Div A': _DIV_A, 'Div B': _DIV_B})            # 20 and 13, no consolidated present
    finder_map = {'revenue': [('Div A', 3, 'Revenue'), ('Div B', 3, 'Revenue')]}
    fields = _gap_fields()
    _find(fields, _ident('cfp-divonly'), _dual_provider(finder_map, {}), prof)
    assert fields['revenue'].held and fields['revenue'].value_cr is None


def test_g3_exactly_one_figure_per_concept_never_n_values():
    prof = _prof({'Summary': _SUMMARY, 'Div A': _DIV_A, 'Div B': _DIV_B})
    finder_map = {'revenue': [('Summary', 3, 'Revenue'), ('Div A', 3, 'Revenue'), ('Div B', 3, 'Revenue')]}
    fields = _gap_fields()
    _find(fields, _ident('cfp-one'), _dual_provider(finder_map, {'Summary': _SUM_CHAIN}), prof)
    assert isinstance(fields['revenue'], Figure)               # one field, one Figure — never a list of 79


# ── G1 / unified emit: three legitimate corroboration bases, and no-basis → HOLD (fail-closed) ─────
# The emit is unified onto the shared _emit_from_collapse discipline (not a triangulation-ONLY gate):
# it needs ONE of (i) identity-chain, (ii) cross-sheet reconcile, (iii) deterministic-locator-match.
def test_g1_locate_rows_relocates_per_statement_anchors(monkeypatch):
    # Guard 2 stays wired: the finder emit path calls locate_rows per single-region statement, which
    # re-introduces the identity anchors BOUNDED to that statement (their bounding is proven in test_finder).
    seen = {'stmts': [], 'reqs': []}
    real = locator.locate_rows

    def spy(st, grid, targets, **kw):
        seen['stmts'].append(st.sheet)
        seen['reqs'].append(tuple(locator.request_concepts(targets)))
        return real(st, grid, targets, **kw)
    monkeypatch.setattr(locator, 'locate_rows', spy)
    _find(_gap_fields(), _ident('cfp-g1-wired'),
          _dual_provider({'revenue': [('Summary', 3, 'Revenue')]}, {'Summary': _SUM_CHAIN}), _prof({'Summary': _SUMMARY}))
    assert 'Summary' in seen['stmts']                                   # locate_rows invoked on the statement
    assert any('cogs' in r or 'gross_profit' in r for r in seen['reqs'])   # anchors re-enter (relocation)


def test_basis_i_identity_chain_alone_can_emit(monkeypatch):
    # code locator disabled → basis (iii) off; single-source → basis (ii) off; only the identity chain
    # remains. With anchors → chain PASSES → emit. REDDENING: remove anchors → no basis → HELD.
    monkeypatch.setattr(extract, '_find_concept_row', lambda *a, **k: None)
    prof = _prof({'Summary': _SUMMARY})
    fmap = {'revenue': [('Summary', 3, 'Revenue')], 'ebitda': [('Summary', 7, 'EBITDA')],
            'cash': [('Summary', 8, 'Cash')], 'headcount': [('Summary', 9, 'Headcount')]}
    f = _gap_fields()
    _find(f, _ident('cfp-basis-i'), _dual_provider(fmap, {'Summary': _SUM_CHAIN}), prof)
    assert f['revenue'].value_cr == 33 and not f['revenue'].held        # identity-chain ALONE emits
    monkeypatch.setattr(locator, 'locate_rows', lambda *a, **k: {'records': [], 'absent': [], 'missing': []})
    f2 = _gap_fields()
    _find(f2, _ident('cfp-basis-i-red'), _dual_provider(fmap, {'Summary': _SUM_CHAIN}), prof)
    assert f2['revenue'].held and f2['revenue'].value_cr is None        # no chain, no other basis → HELD


def test_basis_iii_locator_match_alone_can_emit(monkeypatch):
    # no anchors (no chain → basis i off), single-source (basis ii off): the finder row MATCHES the code
    # locator → basis (iii) emits. REDDENING: finder points at a row code does NOT pick → uncorroborated → HELD.
    monkeypatch.setattr(locator, 'locate_rows', lambda *a, **k: {'records': [], 'absent': [], 'missing': []})
    prof = _prof({'Summary': _SUMMARY})
    f = _gap_fields()
    _find(f, _ident('cfp-basis-iii'), _dual_provider({'revenue': [('Summary', 3, 'Revenue')]}, {}), prof)
    assert f['revenue'].value_cr == 33 and not f['revenue'].held        # locator-match ALONE emits
    f2 = _gap_fields()
    _find(f2, _ident('cfp-basis-iii-red'), _dual_provider({'revenue': [('Summary', 5, 'Gross Profit')]}, {}), prof)
    assert f2['revenue'].held and f2['revenue'].value_cr is None        # code finds revenue at row 3, not 5 → HELD


# ── G2: reject 'Total Other Income'; neutralising the guard poisons reconcile → HOLD ──────────────
def test_g2_total_other_income_is_rejected_not_emitted_as_revenue():
    prof = _prof({'Summary': _SUMMARY, 'Div A': _DIV_A, 'Div B': _DIV_B})
    finder_map = {'revenue': [('Summary', 3, 'Revenue'), ('Summary', 10, 'Total Other Income'),
                              ('Div A', 3, 'Revenue'), ('Div B', 3, 'Revenue')]}
    fields = _gap_fields()
    diags = _find(fields, _ident('cfp-oi'), _dual_provider(finder_map, {'Summary': _SUM_CHAIN}), prof)
    assert any(d.get('rejected') == 'other-income-not-revenue' for d in diags)
    assert fields['revenue'].value_cr == 33 and not fields['revenue'].held      # real revenue unpolluted


def test_g2_other_income_reddening_neutralised_guard_poisons_the_reconcile(monkeypatch):
    # with the guard OFF, 'Total Other Income' (Σ=24) enters as a SAME-scope revenue reading on Summary
    # (33 vs 24) → same-scope conflict → HOLD. Proves the guard is load-bearing.
    monkeypatch.setattr(extract, '_OTHER_INCOME_RE', re.compile(r'^\Z'))   # matches nothing → guard off
    prof = _prof({'Summary': _SUMMARY, 'Div A': _DIV_A, 'Div B': _DIV_B})
    finder_map = {'revenue': [('Summary', 3, 'Revenue'), ('Summary', 10, 'Total Other Income'),
                              ('Div A', 3, 'Revenue'), ('Div B', 3, 'Revenue')]}
    fields = _gap_fields()
    _find(fields, _ident('cfp-oi-red'), _dual_provider(finder_map, {'Summary': _SUM_CHAIN}), prof)
    assert fields['revenue'].held and fields['revenue'].value_cr is None


# ── G2: the sneaky 'Chk Total Sales' — a value + the word 'sales' on a NON-statement tab ───────────
def test_g2_chk_total_sales_on_non_statement_sheet_is_rejected():
    prof = _prof({'Summary': _SUMMARY, 'Div A': _DIV_A, 'Div B': _DIV_B, 'Main': _MAIN})
    finder_map = {'revenue': [('Summary', 3, 'Revenue'), ('Div A', 3, 'Revenue'),
                              ('Div B', 3, 'Revenue'), ('Main', 3, 'Chk Total Sales')]}
    fields = _gap_fields()
    diags = _find(fields, _ident('cfp-chk'), _dual_provider(finder_map, {'Summary': _SUM_CHAIN}), prof)
    assert any(d.get('rejected') == 'non-statement-sheet' and d.get('sheet') == 'Main' for d in diags)
    assert fields['revenue'].value_cr == 33 and not fields['revenue'].held      # check cell never entered reconcile


def test_g2_chk_total_sales_reddening_admitting_the_tab_breaks_the_emit(monkeypatch):
    # force 'Main' to look like an income statement → the check cell (Σ=1107) enters reconcile as a new
    # scope. Σ over {33,20,13,1107}: no unique satisfier → HOLD. Proves the non-statement gate is load-bearing.
    real = extract._statement_kind
    monkeypatch.setattr(extract, '_statement_kind',
                        lambda rows, sheet='', scan_rows=25: 'income' if sheet == 'Main' else real(rows, sheet, scan_rows))
    prof = _prof({'Summary': _SUMMARY, 'Div A': _DIV_A, 'Div B': _DIV_B, 'Main': _MAIN})
    finder_map = {'revenue': [('Summary', 3, 'Revenue'), ('Div A', 3, 'Revenue'),
                              ('Div B', 3, 'Revenue'), ('Main', 3, 'Chk Total Sales')]}
    fields = _gap_fields()
    _find(fields, _ident('cfp-chk-red'), _dual_provider(finder_map, {'Summary': _SUM_CHAIN, 'Main': {}}), prof)
    assert fields['revenue'].held and fields['revenue'].value_cr is None


# ── G4: cash aligned across sheets by PERIOD, not column position ──────────────────────────────────
_CASHFLOW = [['Cash Flow Statement', '', ''], ['Particulars (INR Cr)', 'Apr-25', 'May-25'],
             ['Opening cash balance', 20, 25], ['Net cash generated', 5, 5],
             ['Closing cash balance', 25, 30]]                 # closing at row 5, latest May=30
_BS = [['Balance Sheet', '', ''], ['Particulars (INR Cr)', 'Mar-25', 'Apr-25'],
       ['Cash', 20, 25], ['Debtors', 10, 11], ['Net assets', 30, 36]]   # BS 'current' (Apr=25) == CF PRIOR col


def test_g4_cash_period_alignment_avoids_false_conflict():
    prof = _prof({'CashFlow': _CASHFLOW, 'BS': _BS})
    finder_map = {'cash': [('CashFlow', 5, 'Closing cash balance'), ('BS', 3, 'Cash')]}
    fields = _gap_fields()
    diags = _find(fields, _ident('cfp-g4'), _dual_provider(finder_map, {}), prof)
    d = _diag(diags, 'cash')
    # May-2025 (30) and Apr-2025 (25) are DIFFERENT periods → only the latest bucket reconciles →
    # 'single', NOT a false 'conflict'/'multiscope' hold from conflating the two by column position.
    assert d is not None and d['disposition'] == 'single'


def test_g4_money_stock_held_on_unverified_reporting_boundary():
    # Guard 4 (period-binding): a money STOCK is a point-in-time reading → trustworthy only at a VERIFIED
    # reporting date. When the boundary is not verified (no date, or merely FLOW-DERIVED — the CSS Dec-column
    # cash on a May file), the stock's latest column may be a projected balance → HELD, never emitted.
    # Control: a verified boundary → it CAN emit.
    prof = _prof({'BS': _BS})
    fmap = {'cash': [('BS', 3, 'Cash')]}
    f_un = _gap_fields()
    diags = _find(f_un, _ident('cfp-g4b-un'), _dual_provider(fmap, {}), prof, boundary_verified=False)
    assert any(d.get('rejected') == 'money-stock-on-unverified-reporting-boundary' for d in diags)
    assert not (isinstance(f_un['cash'], Figure) and f_un['cash'].confirmed)   # HELD/gap — never emitted
    f_ok = _gap_fields()
    _find(f_ok, _ident('cfp-g4b-ok'), _dual_provider(fmap, {}), prof, boundary_verified=True)   # control
    assert isinstance(f_ok['cash'], Figure) and f_ok['cash'].confirmed and f_ok['cash'].value_cr == 25


def test_g4_reddening_without_period_alignment_a_false_conflict_appears(monkeypatch):
    monkeypatch.setattr(extract, '_period_key', lambda col, acts: None)   # disable period bucketing
    prof = _prof({'CashFlow': _CASHFLOW, 'BS': _BS})
    finder_map = {'cash': [('CashFlow', 5, 'Closing cash balance'), ('BS', 3, 'Cash')]}
    fields = _gap_fields()
    diags = _find(fields, _ident('cfp-g4-red'), _dual_provider(finder_map, {}), prof)
    d = _diag(diags, 'cash')
    # both readings now share one bucket → 30 vs 25 across 2 scopes → multiscope with no Σ (2 sources) → HOLD
    assert d is not None and d['disposition'] == 'multiscope' and d['value_cr'] is None
