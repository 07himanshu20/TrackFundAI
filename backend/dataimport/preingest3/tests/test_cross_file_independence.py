"""CROSS-FILE INDEPENDENCE (universal-guard): changing the file SET must not change any OTHER file's
result. This is the concrete enforcement of "universal, not file-specific" — a contamination bug
(shared mutable state, a global cache keyed too broadly, a currency/alias/anchor ledger leaking across
files) would make an unchanged file's output differ when a DIFFERENT file is added/removed/reordered.

Suite level: assert the deterministic per-file REPORT (status/reason_code/entity/content_fp) of the
UNCHANGED files is byte-identical across add / remove / reorder. This exercises the shared cross-file
machinery (routing, conservation, currency ledger, alias/fund-anchor) token-free. The emit/VALUE-level
independence is proven on the real 15-file corpus in scratchpad/cross_file_independence.py (151 shared
identities byte-identical when the heavy file is dropped; 0 order-sensitive diffs)."""
import os
import tempfile

import openpyxl

from backend.dataimport.preingest3 import pipeline
from backend.dataimport.preingest3.ratecard import default_inr_card

RC = default_inr_card('2026-06-30')


def _xlsx(path, rows):
    wb = openpyxl.Workbook(); ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(path); return path


def _report_map(files):
    """label -> (status, reason_code, entity_id, content_fp) — deterministic, model-off."""
    res = pipeline.run(files, as_of='2026-06-30', org='indep', rate_card=RC, max_workers=1)
    return {r.label: (r.status, r.reason_code, r.entity_id, r.content_fp) for r in res.files}


def _sub(m, labels):
    return {k: v for k, v in m.items() if k in labels}


def test_add_remove_reorder_leaves_other_files_identical():
    with tempfile.TemporaryDirectory() as d:
        A = ('CoA', _xlsx(os.path.join(d, 'A.xlsx'),
                          [['Particulars', 'Apr-25', 'May-25'], ['Revenue', 10, 11], ['EBITDA', 2, 3]]))
        B = ('CoB', _xlsx(os.path.join(d, 'B.xlsx'),
                          [['Particulars', 'Apr-25', 'May-25'], ['Revenue', 20, 21], ['EBITDA', 4, 5]]))
        C = ('CoC', _xlsx(os.path.join(d, 'C.xlsx'),
                          [['Particulars', 'Apr-25', 'May-25'], ['Revenue', 30, 31], ['EBITDA', 6, 7]]))
        D = ('CoD', _xlsx(os.path.join(d, 'D.xlsx'),
                          [['Particulars', 'Apr-25', 'May-25'], ['Revenue', 40, 41], ['EBITDA', 8, 9]]))

        base = _report_map([A, B, C])
        assert set(base) == {'CoA', 'CoB', 'CoC'}

        added = _report_map([A, B, C, D])                     # ADD D
        assert _sub(added, base) == base, 'adding D changed an existing file report (contamination)'

        removed = _report_map([A, C])                         # REMOVE B
        assert _sub(removed, {'CoA', 'CoC'}) == _sub(base, {'CoA', 'CoC'}), 'removing B perturbed A/C'

        reordered = _report_map([C, B, A])                    # REORDER
        assert reordered == base, 'upload order changed the reports (order-dependence = contamination)'
