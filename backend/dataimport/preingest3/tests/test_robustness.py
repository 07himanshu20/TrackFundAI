"""Adversarial / pathological input robustness — a production system meets garbage,
and a fail-closed design must PROVE it rejects/holds/discloses cleanly and never
crashes or emits a wrong number. Every real file so far is well-formed-ish; this
exercises the ugly edges: corrupt bytes, an empty file, a non-workbook, a file
with zero companies, a true duplicate, and a company whose every figure holds.
"""
import os
import tempfile
from decimal import Decimal

import openpyxl

from backend.dataimport.preingest3 import profiler, fund_anchor, assemble
from backend.dataimport.preingest3.ratecard import default_inr_card
from backend.dataimport.preingest3.extract import extract_company
from backend.dataimport.preingest3.cir import CIR, Record, Figure, Provenance

RC = default_inr_card('2026-02-28')
_PV = Provenance(source_file='x', content_fingerprint='', sheet='S', cell='A1')


def _write(path, data):
    with open(path, 'wb') as fh:
        fh.write(data)
    return path


def _valid_xlsx(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def test_corrupt_empty_and_nonworkbook_files_hold_never_crash():
    with tempfile.TemporaryDirectory() as d:
        corrupt = _write(os.path.join(d, 'corrupt.xlsx'), b'PK\x03\x04 not really a zip')
        empty = _write(os.path.join(d, 'empty.xlsx'), b'')
        txt = _write(os.path.join(d, 'notes.xlsx'), b'this is plain text, not a workbook\n' * 50)
        for bad in (corrupt, empty, txt):
            prof = profiler.profile_file(os.path.basename(bad), bad)
            assert prof['error'] and prof['sheets'] == []          # disclosed, sheet-less
            rec = extract_company(os.path.basename(bad), bad, rate_card=RC)  # must NOT raise
            assert rec.fields.get('_note')                          # held with a reason, no numbers
            assert not any(hasattr(v, 'value') and getattr(v, 'value', None) is not None
                           for v in rec.fields.values())
        # a whole fund-anchor pass over only-garbage returns empty, never crashes
        assert fund_anchor.build_fund_anchors([corrupt, empty, txt]) == {}


def test_zero_company_workbook_yields_no_anchors():
    with tempfile.TemporaryDirectory() as d:
        blank = _valid_xlsx(os.path.join(d, 'blank.xlsx'), [['Notes'], ['no schedule here']])
        assert fund_anchor.build_fund_anchors([blank]) == {}        # valid file, zero companies → {}


def test_true_duplicate_file_is_not_double_counted():
    with tempfile.TemporaryDirectory() as d:
        sched = _valid_xlsx(os.path.join(d, 's.xlsx'),
                            [['Company', 'Cost', 'Fair Value', 'Ownership %'],
                             ['Acme Labs', 40, 90, 25]])
        once = fund_anchor.build_fund_anchors([sched])
        twice = fund_anchor.build_fund_anchors([sched, sched])      # same file passed twice
        assert once and set(once) == set(twice)
        for k in once:
            assert once[k].cost_cr == twice[k].cost_cr              # MAX-dedup → no doubling
            assert twice[k].cost_cr == Decimal('40')


def test_all_figures_held_company_never_zeros_the_total():
    cir = CIR(as_of='2026-02-28')
    cir.add(Record('mis', entity_id='HeldCo', fields={
        'company': 'HeldCo',
        'revenue': Figure('revenue', None, None, _PV, held=True, hold_reason='frame unresolved'),
        'ebitda': Figure('ebitda', None, None, _PV, held=True, hold_reason='EBIT not EBITDA')}))
    wb = assemble.build(cir)
    cover = {r[0]: r[1] for r in wb['Cover'].iter_rows(values_only=True)
             if r and isinstance(r[0], str)}
    assert cover.get('Portfolio Revenue (annualized)') == assemble.INCOMPLETE   # never 0.0


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'ok  {name}')
    print('ALL PASS')
