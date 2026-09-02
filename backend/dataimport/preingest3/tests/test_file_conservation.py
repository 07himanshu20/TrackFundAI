"""File-conservation invariant (spec: file_conservation_and_measurement_spec.md §1).

Proves "no uploaded file is ever dropped" is a CHECKED property, not a hope:
  • every input file → exactly one terminal report; the assertion REDDENS when one is missing;
  • a per-file parse FAILURE becomes HELD(reason_code=parse_error), never a crash or a silent drop —
    the exact parallelism failure mode (a swallowed worker exception) the invariant exists to kill;
  • a corrupt/unreadable file is accounted as an error(parse_error) report, conservation still balances.
"""
import os
import tempfile

import openpyxl
import pytest

from backend.dataimport.preingest3 import pipeline, fund_anchor
from backend.dataimport.preingest3.alias_ledger import AliasLedger
from backend.dataimport.preingest3.pipeline import (
    FileReport, PreingestConservationError, _assert_conservation, HELD_PARSE_ERROR)
from backend.dataimport.preingest3.ratecard import default_inr_card


def _xlsx(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def _good_fund(d):
    sched = _xlsx(os.path.join(d, 'schedule.xlsx'),
                  [['Company', 'Cost', 'Fair Value'], ['Acme Labs', 40, 90], ['Beta Corp', 60, 150]])
    mis = _xlsx(os.path.join(d, 'Acme Labs monthly.xlsx'),
                [['Particulars', 'Apr-25', 'May-25', 'Jun-25'],
                 ['Revenue', 10, 11, 12], ['EBITDA', 2, 2, 3], ['Closing Cash', 5, 6, 7]])
    return [('schedule', sched), ('Acme Labs', mis)]


def _seeded_store(d, files):
    anchors = fund_anchor.build_fund_anchors([files[0][1]])
    name_to_key = {ca.company: k for k, ca in anchors.items()}
    store = AliasLedger(org='cons', path=os.path.join(d, 'al.json'))
    for label, _p in files[1:]:
        k = name_to_key.get(label)
        if k:
            store.learn([label], k, provenance='human')
    return store


# ── the assertion itself: RED-before / GREEN-after (a guard that can't go red proves nothing) ──
def test_assert_conservation_reddens_on_dropped_file():
    files = [('a', '/x/a'), ('b', '/x/b')]
    _assert_conservation(files, [FileReport('a', 'mis', 'attributed', path='/x/a'),
                                 FileReport('b', 'mis', 'held', path='/x/b')])       # GREEN: balances
    with pytest.raises(PreingestConservationError) as ei:
        _assert_conservation(files, [FileReport('a', 'mis', 'attributed', path='/x/a')])  # RED: 'b' lost
    assert '/x/b' in str(ei.value)                                          # names the missing file


# ── per-INSTANCE bijection: keyed on path, catches the same-label residual a label-multiset misses ──
def test_conservation_per_file_bijection_catches_same_label_drop():
    """Two DISTINCT files sharing a label. A label-multiset balances (2 in, 2 out) even when one instance
    is double-reported and the other dropped — a silent loss. Path-keyed conservation reddens on exactly
    that residual, so 'no uploaded file is ever dropped' is airtight on its own, not delegated to U4."""
    files = [('dup', '/x/a'), ('dup', '/x/b')]
    _assert_conservation(files, [FileReport('dup', 'mis', 'attributed', path='/x/a'),
                                 FileReport('dup', 'mis', 'held', path='/x/b')])     # GREEN: each instance
    # RED: '/x/a' double-reported, '/x/b' dropped. label-multiset would MISS this (in=2, out=2); path won't.
    with pytest.raises(PreingestConservationError) as ei:
        _assert_conservation(files, [FileReport('dup', 'mis', 'attributed', path='/x/a'),
                                     FileReport('dup', 'mis', 'held', path='/x/a')])
    assert '/x/b' in str(ei.value)                                          # the dropped instance is named


# ── a per-file parse failure must become HELD(parse_error), never a drop or a crash ──
def test_parse_failure_becomes_held_not_dropped(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        files = _good_fund(d)
        store = _seeded_store(d, files)                     # Acme resolves → reaches extraction

        def _boom(*a, **k):
            raise RuntimeError('simulated corrupt/OOM parse failure')
        monkeypatch.setattr(pipeline, 'extract_company', _boom)            # mw=1 → in-process patch applies

        res = pipeline.run(files, as_of='2026-06-30', org='cons',
                           rate_card=default_inr_card('2026-06-30'), alias_store=store, max_workers=1)
        assert len(res.files) == len(files)                                # conservation held: nothing dropped
        rep = next(r for r in res.files if r.label == 'Acme Labs')
        assert rep.status == 'held'
        assert rep.reason_code == HELD_PARSE_ERROR                         # accounted + categorised, not vanished


# ── a corrupt/unreadable file is accounted (error/parse_error), conservation balances ──
def test_corrupt_file_is_accounted_not_dropped():
    with tempfile.TemporaryDirectory() as d:
        files = _good_fund(d)
        bad = os.path.join(d, 'corrupt.xlsx')
        with open(bad, 'wb') as fh:
            fh.write(b'this is not a real xlsx zip archive')
        files.append(('corrupt', bad))
        res = pipeline.run(files, as_of='2026-06-30', org='cons',
                           rate_card=default_inr_card('2026-06-30'), max_workers=1)
        assert len(res.files) == len(files)                               # conservation: nothing dropped
        rep = next(r for r in res.files if r.label == 'corrupt')
        assert rep.reason_code == HELD_PARSE_ERROR


if __name__ == '__main__':
    test_assert_conservation_reddens_on_dropped_file()
    print('PASS')
