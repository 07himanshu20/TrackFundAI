"""ONE-PASS whole-file finder (locator.locate_across_file) — Step 4 core, proven DETERMINISTICALLY
with a stubbed provider (no real tokens, no customer data). Locks the AI-contract invariants:
  • EXHAUSTIVE: every location of a concept across ALL statements is returned (never first-match-stop)
    — the same concept on two sheets comes back twice, so reconcile_locations can cross-check them.
  • LOCATOR-ONLY: a volunteered number is impossible to emit (RowRecord has no value slot; the parser
    whitelists concept/sheet/form/row/rows/row_label).
  • COMPLETENESS/RECALL: a silently dropped concept is re-requested once; still-undetermined → 'missing'.
  • NEVER-GUESS-A-SHEET: a record naming a sheet outside the supplied inventory is discarded.
The real one-file run (tokens, Vertex/ADC) is separate and gated on GOOGLE_CLOUD_PROJECT."""
import types

import pytest

from backend.dataimport.preingest3 import llm, locator
from backend.dataimport.preingest3.statements import Statement

GRID = {
    'P&L': [['Particulars', 'Mar-26'], ['Total revenue', 100], ['EBITDA', 20]],
    'Balance Sheet': [['Particulars', 'Mar-26'], ['Cash in Bank', 50]],
    'Cashflow': [['Particulars', 'Mar-26'], ['Closing cash', 50]],
}
INVENTORY = [
    Statement('P&L', 0, 2, header_row=0, label_col=0),
    Statement('Balance Sheet', 0, 1, header_row=0, label_col=0),
    Statement('Cashflow', 0, 1, header_row=0, label_col=0),
]


def _requested_concepts(prompt: str):
    """The concepts the finder actually asked for — parsed ONLY from the CONCEPTS block (the STRICT
    RULES section above also uses '- ' bullets, so scope the parse between the two markers)."""
    out, in_block = [], False
    for line in prompt.splitlines():
        if line.startswith('CONCEPTS TO LOCATE'):
            in_block = True
            continue
        if line.startswith('STATEMENTS'):
            break
        if in_block and line.strip().startswith('- '):
            out.append(line.strip()[2:].split(' (')[0].strip())
    return out


def _provider(locmap, *, extra_fields='', absent_the_rest=True):
    """Fake provider: returns `locmap` = {concept: [(sheet, row1based, label)]} as located records,
    and (optionally) an 'absent' record for every other requested concept so 'missing' is empty."""
    def provider(prompt, **kw):
        items = []
        for c, locs in locmap.items():
            for sheet, row, label in locs:
                items.append(f'{{"concept":"{c}","sheet":"{sheet}","form":"direct","row":{row},'
                             f'"row_label":"{label}","rows":[]{extra_fields}}}')
        if absent_the_rest:
            have = set(locmap)
            for c in _requested_concepts(prompt):
                if c not in have:
                    items.append(f'{{"concept":"{c}","form":"absent","sheet":"","row":null,"rows":[]}}')
        return types.SimpleNamespace(text='{"located":[' + ', '.join(items) + ']}')
    return provider


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    saved = llm._MODEL_PROVIDER
    monkeypatch.setattr(llm, '_rl_sleep', lambda *a, **k: None)
    monkeypatch.setattr(llm, '_CACHE_DIR', str(tmp_path / 'calls'))
    llm.new_metrics()
    yield
    llm._MODEL_PROVIDER = saved


def _cash(records):
    return [(s, r) for s, r in records if r.concept == 'cash']


def test_exhaustive_returns_every_location_never_first_match_stop():
    llm.set_model_provider(_provider({
        'revenue': [('P&L', 2, 'Total revenue')],
        'ebitda': [('P&L', 3, 'EBITDA')],
        'cash': [('Balance Sheet', 2, 'Cash in Bank'), ('Cashflow', 2, 'Closing cash')],
    }))
    out = locator.locate_across_file(INVENTORY, GRID, ['revenue', 'ebitda', 'cash', 'headcount'],
                                     content_fp='cfp-finder')
    assert not out.get('error')
    cash = _cash(out['records'])
    assert len(cash) == 2, 'cash on BS AND cash-flow must BOTH return (no first-match-stop)'
    assert {s for s, _ in cash} == {'Balance Sheet', 'Cashflow'}
    concepts = {r.concept for _s, r in out['records']}
    assert {'revenue', 'ebitda', 'cash'} <= concepts
    assert 'headcount' in out['absent']              # genuinely in no statement → absent, not guessed
    assert out['missing'] == []


def test_locator_only_volunteered_number_has_no_slot():
    # each record ALSO carries a bogus number; RowRecord has no value slot + the parser whitelists,
    # so the volunteered number cannot travel — the located ROW is all that survives.
    llm.set_model_provider(_provider(
        {'cash': [('Balance Sheet', 2, 'Cash in Bank')]},
        extra_fields=',"value":9999999,"amount":9999999'))
    out = locator.locate_across_file(INVENTORY, GRID, ['cash'], content_fp='cfp-loc-only')
    cash = _cash(out['records'])
    assert len(cash) == 1
    rr = cash[0][1]
    assert not hasattr(rr, 'value') and not hasattr(rr, 'amount')
    assert rr.row == 1 and rr.row_label == 'Cash in Bank'     # 1-based 2 → 0-based 1


def test_unknown_sheet_is_discarded_never_guessed():
    llm.set_model_provider(_provider(
        {'cash': [('Ghost Sheet', 2, 'Cash'), ('Balance Sheet', 2, 'Cash in Bank')]},
        absent_the_rest=False))
    out = locator.locate_across_file(INVENTORY, GRID, ['cash'], content_fp='cfp-ghost')
    cash = _cash(out['records'])
    assert {s for s, _ in cash} == {'Balance Sheet'}, 'a location on an unlisted sheet must be discarded'


def test_out_of_range_row_is_discarded():
    llm.set_model_provider(_provider({'cash': [('Balance Sheet', 99, 'Cash in Bank')]},
                                     absent_the_rest=False))
    out = locator.locate_across_file(INVENTORY, GRID, ['cash'], content_fp='cfp-oor')
    assert _cash(out['records']) == []               # row 99 not in the statement → discarded


def test_completeness_recall_refetches_silent_drop():
    # first call omits cash entirely (silent drop); the recall net re-requests and the retry supplies it.
    calls = {'n': 0}

    def provider(prompt, **kw):
        calls['n'] += 1
        if calls['n'] == 1:
            return types.SimpleNamespace(text='{"located":['
                '{"concept":"revenue","sheet":"P&L","form":"direct","row":2,"row_label":"Total revenue","rows":[]}]}')
        return types.SimpleNamespace(text='{"located":['
            '{"concept":"cash","sheet":"Balance Sheet","form":"direct","row":2,"row_label":"Cash in Bank","rows":[]}]}')
    llm.set_model_provider(provider)
    out = locator.locate_across_file(INVENTORY, GRID, ['revenue', 'cash'], content_fp='cfp-recall')
    assert calls['n'] == 2, 'a silent drop must trigger exactly one focused recall retry'
    assert _cash(out['records']), 'the recall retry must recover the dropped concept'


def test_empty_inventory_holds_all_as_missing_no_call():
    calls = {'n': 0}

    def provider(prompt, **kw):
        calls['n'] += 1
        return types.SimpleNamespace(text='{"located":[]}')
    llm.set_model_provider(provider)
    out = locator.locate_across_file([], GRID, ['cash'], content_fp='cfp-empty')
    assert calls['n'] == 0 and out['records'] == [] and 'cash' in out['missing']


# ── TOKEN-BUDGETED CHUNKING (split, never filter-to-fit) ──────────────────────────────────────────
import re  # noqa: E402

GRID2 = {**GRID,
         'Segment A': [['Particulars', 'Mar-26'], ['Revenue seg A', 30]],
         'Segment B': [['Particulars', 'Mar-26'], ['Revenue seg B', 40]]}
INV2 = INVENTORY + [Statement('Segment A', 0, 1, header_row=0, label_col=0),
                    Statement('Segment B', 0, 1, header_row=0, label_col=0)]


def _sheets_in_prompt(prompt):
    return set(re.findall(r'=== SHEET "(.*?)" \(rows', prompt))


def _chunk_provider(locmap, *, error_sheets=()):
    """Per-chunk fake: returns only the locmap records whose sheet is IN this chunk's prompt, marks the
    rest absent. If a chunk contains any `error_sheets`, it returns an unparseable reply (chunk error)."""
    def provider(prompt, **kw):
        sheets = _sheets_in_prompt(prompt)
        if sheets & set(error_sheets):
            return types.SimpleNamespace(text='<<garbage not json>>')
        items, seen = [], set()
        for c, locs in locmap.items():
            for sheet, row, label in locs:
                if sheet in sheets:
                    items.append(f'{{"concept":"{c}","sheet":"{sheet}","form":"direct","row":{row},'
                                 f'"row_label":"{label}","rows":[]}}')
                    seen.add(c)
        for c in _requested_concepts(prompt):
            if c not in seen:
                items.append(f'{{"concept":"{c}","form":"absent","sheet":"","row":null,"rows":[]}}')
        return types.SimpleNamespace(text='{"located":[' + ', '.join(items) + ']}')
    return provider


def test_chunking_unions_locations_and_is_exhaustive_across_chunks():
    # revenue lives on 3 sheets that a small budget splits across ≥2 chunks; the union must recover all 3.
    llm.set_model_provider(_chunk_provider({
        'revenue': [('P&L', 2, 'Total revenue'), ('Segment A', 2, 'Revenue seg A'),
                    ('Segment B', 2, 'Revenue seg B')],
        'cash': [('Balance Sheet', 2, 'Cash in Bank')],
    }))
    out = locator.locate_across_file_chunked(INV2, GRID2, ['revenue', 'cash'],
                                             content_fp='cfp-chunk', token_budget=25)
    assert out['chunks'] >= 2, 'small budget must split into multiple chunks'
    rev_sheets = {s for s, r in out['records'] if r.concept == 'revenue'}
    assert rev_sheets == {'P&L', 'Segment A', 'Segment B'}, 'union across chunks must be exhaustive'
    assert out['missing'] == [] and out['chunk_error'] is False


def test_chunk_boundaries_are_deterministic():
    llm.set_model_provider(_chunk_provider({'revenue': [('P&L', 2, 'Total revenue')]}))
    a = locator.locate_across_file_chunked(INV2, GRID2, ['revenue'], content_fp='cfp-det', token_budget=25)
    b = locator.locate_across_file_chunked(INV2, GRID2, ['revenue'], content_fp='cfp-det', token_budget=25)
    assert a['chunks'] == b['chunks']
    assert {(s, r.concept, r.row) for s, r in a['records']} == {(s, r.concept, r.row) for s, r in b['records']}


def test_absent_only_when_absent_in_every_chunk():
    # headcount is on NO sheet → every chunk says absent → final 'absent' (determinate), not 'missing'.
    llm.set_model_provider(_chunk_provider({'revenue': [('P&L', 2, 'Total revenue')]}))
    out = locator.locate_across_file_chunked(INV2, GRID2, ['revenue', 'headcount'],
                                             content_fp='cfp-abs', token_budget=25)
    assert 'headcount' in out['absent'] and 'headcount' not in out['missing']


def test_chunk_error_makes_unlocated_concept_missing_never_silently_absent():
    # the chunk containing 'Segment B' errors; a concept found ONLY there is undetermined → MISSING,
    # never downgraded to a silent 'absent' (the never-miss guarantee under partial failure).
    llm.set_model_provider(_chunk_provider(
        {'revenue': [('Segment B', 2, 'Revenue seg B')]}, error_sheets=('Segment B',)))
    out = locator.locate_across_file_chunked(INV2, GRID2, ['revenue'],
                                             content_fp='cfp-err', token_budget=25)
    assert out['chunk_error'] is True
    assert 'revenue' in out['missing'] and 'revenue' not in out['absent']


# ── TARGETS-ONLY (Option 1 / Guard 1) + PER-STATEMENT ANCHOR RELOCATION (Guard 2) ─────────────────
def test_finder_requests_targets_only_no_over_location_anchors():
    """Option 1 / Guard 1: the whole-file finder requests the TARGET concepts ONLY. The over-location
    anchors (period_total, assets, liabilities, cogs, …) — each of which matches dozens of 'Total …'
    rows across a whole workbook and drove the output-explosion timeout — must NOT be requested here.
    They are relocated to the per-statement step downstream (see the Guard-2 test below). Reddens the
    instant the finder re-expands to request_concepts()."""
    seen = {}

    def provider(prompt, **kw):
        seen['p'] = prompt
        items = [f'{{"concept":"{c}","form":"absent","sheet":"","row":null,"rows":[]}}'
                 for c in _requested_concepts(prompt)]
        return types.SimpleNamespace(text='{"located":[' + ', '.join(items) + ']}')
    llm.set_model_provider(provider)
    locator.locate_across_file(INVENTORY, GRID, ['revenue', 'ebitda', 'cash', 'headcount'],
                               content_fp='cfp-targets-only')
    reqs = set(_requested_concepts(seen['p']))
    assert reqs == {'revenue', 'ebitda', 'cash', 'headcount'}, f'finder must request targets only, got {reqs}'
    pure_anchors = set(locator.OVER_LOCATION_ANCHORS) - reqs      # anchors that are NOT also targets
    assert 'period_total' in pure_anchors and 'assets' in pure_anchors   # sanity: these are pure anchors
    assert not (pure_anchors & reqs), 'no over-location anchor may leak into the whole-file request'


def test_locate_rows_relocates_anchors_and_bounds_them_to_the_statement():
    """Guard 2 — relocation, not deletion: the anchors dropped from the whole-file finder RE-ENTER at
    the per-statement step (locate_rows requests them via request_concepts) so triangulation keeps its
    inputs — and are BOUNDED to that one statement: an anchor row the model places outside the
    statement window is discarded, so a triangulation input can only ever come from within the
    statement being reconciled. (If locate_rows stopped requesting anchors, 'period_total' would be
    absent from the prompt and this reddens.)"""
    seen = {}

    def provider(prompt, **kw):
        seen['p'] = prompt
        # cash INSIDE the window (row 2 → valid) is kept; period_total OUTSIDE (row 999) is bounded out.
        return types.SimpleNamespace(text='{"located":['
            '{"concept":"cash","form":"direct","row":2,"row_label":"Cash in Bank","rows":[]},'
            '{"concept":"period_total","form":"direct","row":999,"row_label":"Total","rows":[]}]}')
    llm.set_model_provider(provider)
    st = Statement('Balance Sheet', 0, 1, header_row=0, label_col=0)
    out = locator.locate_rows(st, GRID, ['cash'], content_fp='cfp-anchor-bound')
    reqs = set(_requested_concepts(seen['p']))
    assert 'period_total' in reqs, 'anchors must re-enter the per-statement request (relocation)'
    assert any(r.concept == 'cash' and r.row == 1 for r in out['records'])       # in-window kept (0-based)
    assert all(r.concept != 'period_total' for r in out['records']), 'anchor row outside the window is bounded out'


# ── ROW-RANGE SPLIT of a single oversized sheet (Guard 3d) — split, never filter-to-fit ────────────
BIG = {'Big P&L': [['Particulars', 'Mar-26']] + [[f'Line {i:02d}', i] for i in range(1, 40)]}
BIG_INV = [Statement('Big P&L', 0, 39, header_row=0, label_col=0)]


def _sheet_ranges_in_prompt(prompt):
    """{sheet: [(a,b), …]} the 1-based row ranges actually RENDERED in this chunk's prompt."""
    out = {}
    for m in re.finditer(r'=== SHEET "(.*?)" \(rows (\d+)-(\d+)\)', prompt):
        out.setdefault(m.group(1), []).append((int(m.group(2)), int(m.group(3))))
    return out


def _rowaware_provider(locmap):
    """Faithful chunk provider: returns a located record ONLY when its row is inside a range the chunk
    actually renders — the model cannot find a row it was never shown. Every requested concept with no
    visible row in this chunk is marked absent, so it is the cross-chunk UNION (not any single chunk)
    that recovers a concept whose row lands in a later window of a row-split sheet."""
    def provider(prompt, **kw):
        ranges = _sheet_ranges_in_prompt(prompt)
        items, seen = [], set()
        for c, locs in locmap.items():
            for sheet, row, label in locs:
                if any(a <= row <= b for a, b in ranges.get(sheet, [])):
                    items.append(f'{{"concept":"{c}","sheet":"{sheet}","form":"direct","row":{row},'
                                 f'"row_label":"{label}","rows":[]}}')
                    seen.add(c)
        for c in _requested_concepts(prompt):
            if c not in seen:
                items.append(f'{{"concept":"{c}","form":"absent","sheet":"","row":null,"rows":[]}}')
        return types.SimpleNamespace(text='{"located":[' + ', '.join(items) + ']}')
    return provider


def test_oversized_single_sheet_is_row_split_across_chunks_and_unioned():
    # ONE sheet whose labels alone exceed the budget must be SPLIT into row-range windows (never
    # filtered to fit): a concept on an EARLY row and one on a LATE row land in different windows/chunks,
    # and the union recovers both — the whole sheet is seen, just never all at once.
    llm.set_model_provider(_rowaware_provider({
        'revenue': [('Big P&L', 2, 'Line 01')],      # near the top → first window
        'ebitda': [('Big P&L', 40, 'Line 39')],      # the last row → a later window
    }))
    out = locator.locate_across_file_chunked(BIG_INV, BIG, ['revenue', 'ebitda'],
                                             content_fp='cfp-split', token_budget=30)
    assert out['chunks'] >= 2, 'one oversized sheet must be row-split into multiple chunks'
    got = {(s, r.concept, r.row + 1) for s, r in out['records']}
    assert ('Big P&L', 'revenue', 2) in got
    assert ('Big P&L', 'ebitda', 40) in got, 'a late row in a later window must still be found (union)'
    assert out['missing'] == [] and out['chunk_error'] is False


def test_row_split_windows_are_contiguous_and_cover_the_whole_sheet():
    # the split must partition the sheet with NO gaps and NO overlaps — every original row lands in
    # exactly one window, so no row (hence no concept) can be lost between windows.
    windows = locator._split_statement_by_budget(BIG_INV[0], BIG, token_budget=30)
    assert len(windows) >= 2
    assert windows[0].start_row == 0 and windows[-1].end_row == 39
    for a, b in zip(windows, windows[1:]):
        assert b.start_row == a.end_row + 1, 'windows must be contiguous (no gap, no overlap)'
    assert all(w.sheet == 'Big P&L' and w.label_col == 0 for w in windows)   # identity preserved
