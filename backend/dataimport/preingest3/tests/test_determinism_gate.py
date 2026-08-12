"""DETERMINISM GATE (advisor Step 2) — 100% determinism is an ENGINEERED property,
proven, not hoped for.

WHY CROSS-PROCESS: dict/set iteration order is stable *within* one process but varies
with PYTHONHASHSEED *across* processes. An in-process "run it twice" check shares the
seed and would stay green while real production runs (fresh process each time) diverge.
So the only honest test spawns TWO separate processes under DIFFERENT seeds and diffs
their output byte-for-byte. A set-ordering bug in a selection path (e.g. `_best_sheet`
over CPM's 68 sheets, CSS's 80) shows up here and nowhere else.

WHY IT PAIRS WITH A CODE INVARIANT: this test catches *regressions*; it does not prove
absence. The absence guarantee is the code-level rule (audited, and documented at each
selection set): every set/dict iteration that feeds a SELECTION decision is consumed via
`sorted(...)` (see extract._model_fill `remaining`, locator.locate_rows `sorted(absent)`,
locator.request_concepts `sorted(targets)`). The seed test is the tripwire that fires if
that discipline ever lapses.

UNIVERSAL, not per-file: parametrised over EVERY MIS file — determinism is a property of
the code for all sheets, never tuned for one. Two seeds (0 and a large prime) are the
regression tripwire; the structural rule is what makes the class impossible.

SLOW: each case spawns two subprocesses that each parse the whole workbook (CSS = 80
sheets ≈ 24s/proc). Marked `slow` so `pytest -m "not slow"` skips it in fast loops; run
it before any change to a selection path. Skipped entirely if the fixtures are absent.
"""
import json
import os
import subprocess
import sys

import pytest

from backend.dataimport.preingest3.profiler import profile_file
from backend.dataimport.preingest3.extract import _best_sheet, MIS_CONCEPTS

_HERE = os.path.dirname(__file__)
_BACKEND = os.path.abspath(os.path.join(_HERE, '..', '..', '..'))   # …/backend
_IN_REL = 'media/preingest/trivesta/100e86d5/in'
_SEEDS = ('0', '99991')

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not os.path.isdir(os.path.join(_BACKEND, _IN_REL)),
                       reason='real fixture files not present'),
]

# (label, filename, fund-anchor token) — the full MIS set, biggest-sheet files included.
FILES = [
    ('Hubler', 'AVF_2026_03_11_P_Hubler_MIS_Feb26.xlsx', 'hubbler'),
    ('Agnikul', 'AVF_2026_03_12_P_Agnikul_MIS_Feb26.xlsx', 'agnikul'),
    ('Clientell', 'AVF_2026_03_16_P_Clientell_MIS_Feb26.xlsx', 'clientell'),
    ('InstaAstro', 'AVF_2026_03_18_P_InstaAstro_MIS_Feb_2026.xlsx', 'instaastro'),
    ('LDC', 'AVF_2026_03_20_P_LDC_Detailed_MIS_Feb26.xlsx', 'ldc'),
    ('Aliste', 'AVF_2026_03_26_P_Aliste_MIS_Feb26.xlsx', 'aliste'),
    ('CPC', 'CPC_Monthly_MIS-_May_25_to_be_sent_to_EL.xlsx', 'cpc'),
    ('CPM', '0625_CPM_Monthly_Report-R1.xlsx', 'chemopharm'),          # 68 sheets
    ('CSS', '0525_CSS_Monthy_Report_-_Consol_Updated.xlsx', 'css'),    # 80 sheets
    ('Analisa', '01_Monthly_Financial_Presentation_2025_May_Analisa.xlsx', 'analisa'),
]


def _run(fname: str, token: str, seed: str) -> str:
    env = {**os.environ, 'PYTHONHASHSEED': seed}
    proc = subprocess.run(
        [sys.executable, '-m', 'dataimport.preingest3.tests._det_worker', _IN_REL, fname, token],
        cwd=_BACKEND, env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f'worker failed (seed={seed}): {proc.stderr[-500:]}'
    assert proc.stdout.strip(), f'worker produced no output (seed={seed})'
    return proc.stdout


@pytest.mark.parametrize('label,fname,token', FILES, ids=[f[0] for f in FILES])
def test_extract_is_deterministic_across_hash_seeds(label, fname, token):
    a = _run(fname, token, _SEEDS[0])
    b = _run(fname, token, _SEEDS[1])
    # Byte-identical is the strong claim; if it ever breaks, show which concept moved.
    if a != b:
        da, db = json.loads(a), json.loads(b)
        moved = {k: (da.get(k), db.get(k)) for k in set(da) | set(db) if da.get(k) != db.get(k)}
        raise AssertionError(
            f'{label}: extraction is NONDETERMINISTIC across PYTHONHASHSEED {_SEEDS} '
            f'(a set/dict iteration feeding selection is unsorted). Moved: {moved}')


# Files with the MOST sheets are the most tie-prone for _best_sheet; CSS is the concrete
# regression fixture (it flipped 'ANA & LS' ↔ 'SourcePL-SAP' before the name tie-break).
_SHUFFLE_FILES = [('Hubler', FILES[0][1]), ('CPM', FILES[7][1]), ('CSS', FILES[8][1])]


@pytest.mark.parametrize('label,fname', _SHUFFLE_FILES, ids=[f[0] for f in _SHUFFLE_FILES])
def test_best_sheet_pick_is_input_order_invariant(label, fname):
    """STRUCTURAL determinism: _best_sheet's pick must not depend on the ORDER sheets are
    presented in. Profile once, then run selection over the original, reversed, and rotated
    sheet lists — all must pick the SAME sheet. This is what the stable (name) tie-break in
    _best_sheet guarantees; it makes the order-dependence class impossible, complementing the
    cross-process seed gate (which fixes input order and so cannot see this class)."""
    prof = profile_file(label, os.path.join(_BACKEND, _IN_REL, fname))
    sheets = list(prof['sheets'])
    orders = {
        'orig': sheets,
        'rev': list(reversed(sheets)),
        'rot': sheets[len(sheets) // 2:] + sheets[:len(sheets) // 2],
    }
    picks = {}
    for tag, order in orders.items():
        p2 = dict(prof)
        p2['sheets'] = order
        best = _best_sheet(p2, MIS_CONCEPTS)
        picks[tag] = best[2] if best else None
    assert len(set(picks.values())) == 1, (
        f'{label}: _best_sheet pick depends on sheet input order (unstable tie-break): {picks}')
