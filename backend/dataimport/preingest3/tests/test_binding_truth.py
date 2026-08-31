"""LOCKED binding-truth table for all 10 real trivesta company files.

The 4-part binding contract every row-bind must satisfy:
  (1) data-row      — has values on the period axis (not a section header);
  (2) argmax-unique — `concept` is the confident unique winner over all concepts
                      for the label (shared gate.rank floor+margin);
  (3) aggregate     — the TOTAL / period-END line, not a component or opening;
  (4) verified      — the figure fits its measure/scale (identity/anchor).

This fixture pins the EXPECTED bound row label (or GAP) per concept per file, so
any matcher change that regresses a previously-correct binding is caught
immediately (fix-one-break-another → visible). It is the guard the Agnikul-cash
regression proved we needed.

VERIFIED — ground-truthed against the raw cells; these must never regress.
KNOWN_WRONG — bindings that still fail clause (2)/(4) via SEMANTIC cross-concept
contamination (whole-word match into a wrong-meaning line, e.g. 'employees' in a
uniform-expense GL line). Recorded so (a) they're documented, (b) the day a fix
lands, the test flags it to promote them to VERIFIED. They are HELD downstream,
so none currently emits a wrong number except where noted in the bug report.
"""
import os

import pytest

from backend.dataimport.preingest3.profiler import profile_file
from backend.dataimport.preingest3.extract import _best_sheet, MIS_CONCEPTS

IN = 'backend/media/preingest/trivesta/100e86d5/in'
FILES = {
    'LDC': 'AVF_2026_03_20_P_LDC_Detailed_MIS_Feb26.xlsx',
    'Hubler': 'AVF_2026_03_11_P_Hubler_MIS_Feb26.xlsx',
    'InstaAstro': 'AVF_2026_03_18_P_InstaAstro_MIS_Feb_2026.xlsx',
    'Agnikul': 'AVF_2026_03_12_P_Agnikul_MIS_Feb26.xlsx',
    'Aliste': 'AVF_2026_03_26_P_Aliste_MIS_Feb26.xlsx',
    'Clientell': 'AVF_2026_03_16_P_Clientell_MIS_Feb26.xlsx',
    'CPC': 'CPC_Monthly_MIS-_May_25_to_be_sent_to_EL.xlsx',
    'CSS': '0525_CSS_Monthy_Report_-_Consol_Updated.xlsx',
    'CPM': '0625_CPM_Monthly_Report-R1.xlsx',
    'Analisa': '01_Monthly_Financial_Presentation_2025_May_Analisa.xlsx',
}

# (file, concept) -> expected bound label (None = GAP). Only the ground-truthed,
# must-not-regress bindings are asserted here.
VERIFIED = {
    ('LDC', 'revenue'): 'Gross Revenue',
    ('LDC', 'cash'): 'Cash & Bank Balance',
    ('Hubler', 'revenue'): 'Total revenue',
    ('Hubler', 'ebitda'): 'EBITDA',
    ('InstaAstro', 'revenue'): None,                 # only '% of revenue' ratios exist → GAP, not 0
    ('InstaAstro', 'ebitda'): 'EBITDA',
    ('InstaAstro', 'cash'): 'Ending Cash Balance',
    ('Agnikul', 'revenue'): 'Total Income',
    ('Agnikul', 'cash'): 'Cash in Bank',             # the regression this fixture guards
    ('Aliste', 'revenue'): 'Total Revenue',          # on the LIVE 'P&L' tab — CORRECTED 2026-08-20:
    #   the file owner confirmed by inspection that the previously-anchored 'Profit & Loss' is a
    #   HIDDEN, #REF!-broken, stale (Dec-2025) DEFUNCT tab, while 'P&L' is the maintained monthly
    #   statement (clean Apr-2025→Feb-2026). Human-AUTHORISED ground-truth correction; the machine
    #   fix is the error-dominated-row disqualifier (_concept_row_all_error), NOT visibility (a
    #   correct sheet can also be hidden — Analisa's 'ProfitLoss (23)').
    ('CPC', 'cash'): 'Closing Cash Balance including Fixed deposits',
    ('Analisa', 'revenue'): 'TOTAL REVENUE',
    ('Clientell', 'revenue'): 'Total Revenues from Operations',   # R1c (2026-08-20): the Schedule-III
    #   mandated top line was INVISIBLE to the matcher — the plural 'Revenues from Operations' is not a
    #   synonym of singular 'revenue', so on the ops sheet revenue mis-matched the funnel COUNT 'Sales
    #   Qualified Leads' (via the 'sales' synonym). Adding the standard 'revenue(s) from operations'
    #   synonym makes the real financial sheet 'Analysis' win selection (n_found 3>2) and revenue bind to
    #   the money row. It still HOLDS at emit (2 discrete months + a cumulative YTD sentinel cannot form a
    #   confident full-period flow), so no wrong number ships — cash=9.3253/headcount=19 are unaffected.
}

# The negation gate moved CSS off the raw SAP trial balance onto a real-statement
# sheet, so ebitda now binds 'EBITDA' and headcount 'Headcount (Number)' — the
# ROWS look right, but NOT promoted to VERIFIED: the emitted revenue/EBITDA give a
# 39% margin (distributor norm ~10-15%), a period-basis mismatch tell. A binding
# is only VERIFIED when its figure also passes the ratio/identity check.
PENDING = {
    ('CSS', 'ebitda'): 'EBITDA',
    ('CSS', 'headcount'): 'Headcount (Number)',
}

# Documented open bugs (semantic cross-concept contamination). Expected CORRECT
# answer is GAP — the label is not that concept. The test asserts they are STILL
# wrong so the ledger stays honest and a future fix is noticed.
KNOWN_WRONG = {
    # Pure-semantic residue: right value-type/scale/context, differs only in
    # MEANING → the model route resolves these (they HOLD now, quota-blocked, so
    # none emits a wrong number: Analisa cash is held by the >3-orders
    # figure-anchor sanity).
    #   ('Clientell', 'revenue') — FIXED (R1c, 2026-08-20; now VERIFIED above). Was mis-bound to the
    #   funnel count 'Sales Qualified Leads'; the Schedule-III 'revenue(s) from operations' synonym flips
    #   selection onto Analysis's real money line. Removed here (no wrong binding remains to document).
    # ('CPM', 'cash') — STRUCTURALLY FIXED (net axis-1, 2026-07-27). CPM's cash used to bind the
    # SAP-dump GL line '83231010 - Sponsorship-cash-OP' because the dump `SourcePL SAP` WON sheet
    # selection. The tier selector now DEMOTES that dump (GL-code 78%, 647 labels), so CPM selects a
    # real division P&L on which no cash line exists → cash cleanly GAPs (value-audited: None, no
    # wrong number). Removed from KNOWN_WRONG (no wrong binding remains to document). The RIGHT source
    # — consolidated cash from the roll-up statement — awaits the Σ-divisions consolidation axis.
    ('Analisa', 'cash'): '9000/000   Selling Exp / Cash Disc Voucher',
    # STOCK-vs-FLOW (advisor 2026-07-23): the deterministic binder lands Hubler cash on
    # the period FLOW "Cash Collected", NOT the cash stock. The model locator corrected
    # this recorded truth — it finds the balance-sheet "Closing balance" (the real
    # stock). The bind still lands here, but the U5 stock-vs-flow guard HOLDS it at emit
    # (test_real_files_values asserts held), so no wrong number leaves. If a matcher
    # fix ever moves this bind to the closing-balance row, promote it to VERIFIED.
    ('Hubler', 'cash'): 'Cash Collected',
}

# the sheet _best_sheet must win per file — a matcher change now moves sheet
# selection (the CSS negation cascade proved it), so a sheet regression must be
# diagnosable directly, not surface as a mystery value change elsewhere.
VERIFIED_SHEET = {
    'LDC': 'Consolidated MIS', 'Hubler': 'P&L', 'InstaAstro': 'P&L',
    'Agnikul': 'Analysis', 'Aliste': 'P&L', 'CPC': 'CFS EL',   # Aliste: P&L is the LIVE tab
    #   ('Profit & Loss' is hidden+#REF!-broken defunct — human-authorised 2026-08-20, see VERIFIED)
    'Analisa': 'ProfitLoss (23)',
    'Clientell': 'Analysis',   # R1c flipped selection off 'Operational Metrics' (ops KPIs) onto the real
    #   financial summary 'Analysis' once revenue's real money line became matchable (n_found 3>2)
}

_CACHE = {}


def _extract(name):
    if name not in _CACHE:
        prof = profile_file(name, os.path.join(IN, FILES[name]))
        best = _best_sheet(prof, MIS_CONCEPTS)
        sheet, d = None, {}
        if best:
            _n, _c, sheet, rows, ax, lc, found = best
            for c in MIS_CONCEPTS:
                r = found.get(c)
                d[c] = None if r is None else str(rows[r][lc]).strip()
        _CACHE[name] = (sheet, d)
    return _CACHE[name]


def _bound(name):
    return _extract(name)[1]


@pytest.mark.parametrize('name,sheet', sorted(VERIFIED_SHEET.items()))
def test_winning_sheet_never_regresses(name, sheet):
    assert _extract(name)[0] == sheet


def test_error_dominated_row_disqualifier_primitive():
    """R1a primitive: an error-dominated concept row (≥1 spreadsheet error, no real non-zero
    number) is disqualified; a row with any real value, or a clean all-zero row, is not."""
    from backend.dataimport.preingest3.extract import _concept_row_all_error
    from collections import namedtuple
    C = namedtuple('C', 'col')
    cols = [C(1), C(2), C(3)]
    assert _concept_row_all_error([['lbl', '#REF!', '#REF!', 0]], 0, cols) is True   # all #REF! + stray 0
    assert _concept_row_all_error([['lbl', '#REF!', 5, 0]], 0, cols) is False         # a real number present
    assert _concept_row_all_error([['lbl', 0, 0, 0]], 0, cols) is False               # clean all-zero (no error)


def test_aliste_flip_depends_on_the_disqualifier(monkeypatch):
    """REDDENING control for the Aliste ground-truth correction: the flip to the live 'P&L' is
    caused by the error-dominated-row disqualifier, nothing else. Neutralise it and Aliste
    REVERTS to the hidden, #REF!-broken defunct 'Profit & Loss' — proving the fix is load-bearing
    and the corrected VERIFIED anchor is not a bare test-edit."""
    import backend.dataimport.preingest3.extract as ex
    monkeypatch.setattr(ex, '_concept_row_all_error', lambda *a, **k: False)
    best = ex._best_sheet(profile_file('Aliste', os.path.join(IN, FILES['Aliste'])), ex.MIS_CONCEPTS)
    assert best[2] == 'Profit & Loss', 'without the disqualifier the defunct hidden sheet must win (RED)'


@pytest.mark.parametrize('key,expected', sorted(VERIFIED.items()))
def test_verified_binding_never_regresses(key, expected):
    name, concept = key
    assert _bound(name).get(concept) == expected


@pytest.mark.parametrize('key,wrong', sorted(KNOWN_WRONG.items()))
def test_known_wrong_binding_still_documented(key, wrong):
    name, concept = key
    got = _bound(name).get(concept)
    # If this assertion fails, the semantic-contamination bug was fixed — promote
    # this entry from KNOWN_WRONG to VERIFIED (expected GAP or the real row).
    assert got == wrong, f'{key} changed from known-wrong {wrong!r} to {got!r} — update the ledger'


if __name__ == '__main__':
    ok = 0
    for key, expected in sorted(VERIFIED.items()):
        got = _bound(key[0]).get(key[1])
        mark = 'ok ' if got == expected else 'FAIL'
        if got == expected:
            ok += 1
        else:
            print(f'{mark} {key}: expected {expected!r} got {got!r}')
    print(f'VERIFIED {ok}/{len(VERIFIED)} pass')
    for key, wrong in sorted(KNOWN_WRONG.items()):
        got = _bound(key[0]).get(key[1])
        print(f'{"still-wrong" if got == wrong else "CHANGED"} {key}: {got!r}')
