"""Reddening controls for the DUAL-RUN SAFETY GATE (dualrun.py).

The gate's one job is to make the wrong-number door loud: a figure that was NOT a trusted
number before and IS an emitted value now (held/gap → emit, or a brand-new emitted record)
must be classified CRITICAL and must BLOCK. These tests prove that property by FEEDING THE
GATE THE BUG and asserting it reddens — and, as the negative control, that an unchanged
signature stays green (no false alarms). The risk ORDER is pinned too, because a mis-ranked
change (a value move filed as LOW) would ship unreviewed.

The classify/diff logic is pure, so these run on synthetic signatures — no customer data.
The real-corpus tripwire lives at the bottom, skip-if-absent (the codebase @_real idiom)."""
import os
from decimal import Decimal

import pytest

from backend.dataimport.preingest3 import dualrun
from backend.dataimport.preingest3.dualrun import (
    CRITICAL, HIGH, MEDIUM, LOW, diff, signature, save_golden, load_golden,
)
from backend.dataimport.preingest3.cir import CIR, Record, Figure, Provenance
from backend.dataimport.preingest3.pipeline import RunResult


def _row(state='EMIT', value_cr='100', basis='point_in_time', value_basis='',
         sheet='P&L', cell='B10', hold_reason=''):
    return {'state': state, 'value_cr': value_cr, 'basis': basis, 'value_basis': value_basis,
            'sheet': sheet, 'cell': cell, 'hold_reason': hold_reason}


def _sev(rep, key):
    for c in rep.changes:
        if c.key == key:
            return c.severity
    return None


# ══════════════════════════════════════════════════════════════════════════════════
# NEGATIVE CONTROL — an unchanged signature is clean (no false alarms, nothing blocks)
# ══════════════════════════════════════════════════════════════════════════════════
def test_identical_signature_is_clean():
    sig = {'mis|Acme|revenue': _row(), 'mis|Acme|cash': _row(state='HELD', value_cr='2.2',
                                                             hold_reason='ambiguous')}
    rep = diff(sig, dict(sig))
    assert rep.changes == []
    assert not rep.blocks


# ══════════════════════════════════════════════════════════════════════════════════
# THE REDDENING CONTROL — a number APPEARING is CRITICAL and BLOCKS (the wrong-number door)
# ══════════════════════════════════════════════════════════════════════════════════
def test_held_to_emit_is_critical_and_blocks():
    k = 'mis|Acme|cash'
    golden = {k: _row(state='HELD', value_cr='2.2', hold_reason='wrong row?')}
    current = {k: _row(state='EMIT', value_cr='86.33')}      # the held figure became a shipped number
    rep = diff(golden, current)
    assert _sev(rep, k) == CRITICAL
    assert rep.blocks
    # control on the SAME data: if it had STAYED held, the gate must NOT redden critical
    rep_same = diff(golden, {k: _row(state='HELD', value_cr='2.2', hold_reason='wrong row?')})
    assert not rep_same.blocks
    assert _sev(rep_same, k) is None                         # identical held → no change at all


def test_gap_to_emit_is_critical():
    k = 'mis|Zephyr|ebitda'
    rep = diff({k: _row(state='GAP', value_cr='')}, {k: _row(state='EMIT', value_cr='12.4')})
    assert _sev(rep, k) == CRITICAL and rep.blocks


def test_new_emitted_record_is_critical():
    k = 'mis|BrandNewCo|revenue'
    rep = diff({}, {k: _row(state='EMIT', value_cr='55')})
    assert _sev(rep, k) == CRITICAL and rep.blocks
    assert rep.changes[0].kind == 'NEW'


# ══════════════════════════════════════════════════════════════════════════════════
# HIGH — a trusted number, or its MEANING, moves (blocks; human decides which is right)
# ══════════════════════════════════════════════════════════════════════════════════
def test_emit_value_change_is_high_and_blocks():
    k = 'mis|Acme|revenue'
    rep = diff({k: _row(value_cr='111.40')}, {k: _row(value_cr='108.90')})
    assert _sev(rep, k) == HIGH and rep.blocks


def test_emit_basis_change_is_high_even_when_value_equal():
    # a TTM number relabelled point_in_time is effectively a wrong figure though the digits match
    k = 'mis|Acme|revenue'
    rep = diff({k: _row(value_cr='120', basis='TTM')}, {k: _row(value_cr='120', basis='point_in_time')})
    assert _sev(rep, k) == HIGH and rep.blocks


def test_value_basis_gross_net_flip_is_high():
    k = 'fund|—|moic'
    rep = diff({k: _row(value_cr='1.8', value_basis='net')},
               {k: _row(value_cr='1.8', value_basis='gross')})
    assert _sev(rep, k) == HIGH and rep.blocks


# ══════════════════════════════════════════════════════════════════════════════════
# MEDIUM — coverage loss is fail-closed: surfaced, but ships no wrong number → NOT blocking
# (this is exactly the CPC-revenue regression class: EMIT 111.40 → HELD)
# ══════════════════════════════════════════════════════════════════════════════════
def test_emit_to_held_is_medium_and_does_not_block():
    k = 'mis|CPC|revenue'
    rep = diff({k: _row(value_cr='111.40')}, {k: _row(state='HELD', value_cr='0.005',
                                                      hold_reason='schedule sheet')})
    assert _sev(rep, k) == MEDIUM
    assert not rep.blocks                                    # coverage loss is safe — does not block
    assert len(rep.changes) == 1                             # but it IS surfaced


def test_removed_emit_is_medium():
    k = 'mis|Acme|revenue'
    rep = diff({k: _row(value_cr='111.40')}, {})
    assert _sev(rep, k) == MEDIUM and not rep.blocks
    assert rep.changes[0].kind == 'REMOVED'


# ══════════════════════════════════════════════════════════════════════════════════
# LOW — reason / provenance churn on a figure that ships no new number → informational
# ══════════════════════════════════════════════════════════════════════════════════
def test_same_value_source_move_is_low():
    k = 'mis|Acme|revenue'
    rep = diff({k: _row(cell='B10')}, {k: _row(cell='C10')})
    assert _sev(rep, k) == LOW and not rep.blocks


def test_hold_reason_change_is_low():
    k = 'mis|Acme|cash'
    rep = diff({k: _row(state='HELD', hold_reason='reason A')},
               {k: _row(state='HELD', hold_reason='reason B')})
    assert _sev(rep, k) == LOW and not rep.blocks


def test_new_held_is_low_not_critical():
    # a new HELD (not a number) must NOT be treated as the wrong-number door
    k = 'mis|NewCo|cash'
    rep = diff({}, {k: _row(state='HELD', value_cr='9', hold_reason='ambiguous')})
    assert _sev(rep, k) == LOW and not rep.blocks


# ══════════════════════════════════════════════════════════════════════════════════
# REPORT — CRITICAL is ordered first and the verdict blocks when a number appears
# ══════════════════════════════════════════════════════════════════════════════════
def test_report_orders_critical_first_and_verdict_blocks():
    golden = {'a': _row(state='HELD', value_cr='1'), 'b': _row(value_cr='5'),
              'c': _row(value_cr='7')}
    current = {'a': _row(state='EMIT', value_cr='1'),        # CRITICAL
               'b': _row(value_cr='6'),                       # HIGH
               'c': _row(value_cr='7', cell='Z9')}            # LOW (same value, source moved)
    rep = diff(golden, current)
    text = dualrun.format_report(rep)
    assert text.index('CRITICAL') < text.index('HIGH') < text.index('LOW')
    assert 'WRONG-NUMBER DOOR' in text
    assert 'VERDICT: BLOCKED' in text


# ══════════════════════════════════════════════════════════════════════════════════
# EXTRACTOR — signature() reads a real RunResult shape (production path), states correct
# ══════════════════════════════════════════════════════════════════════════════════
def _fig(concept, value, sheet='P&L', cell='B2', held=False, gap=False, reason=''):
    prov = Provenance(source_file='f', content_fingerprint='fp', sheet=sheet, cell=cell)
    return Figure(concept, value, None, prov, gap=gap, held=held, hold_reason=reason)


def test_signature_from_runresult_maps_identity_and_state():
    rec = Record('mis', entity_id='Acme', fields={
        'company': 'Acme',
        'revenue': _fig('revenue', Decimal('111.40')),
        'cash': _fig('cash', Decimal('2.2'), held=True, reason='wrong row?'),
        'ebitda': _fig('ebitda', None, gap=True),
    })
    res = RunResult(cir=CIR(records=[rec]))
    sig = signature(res)
    assert sig['mis|Acme|revenue']['state'] == 'EMIT'
    assert sig['mis|Acme|revenue']['value_cr'] == '111.40'
    assert sig['mis|Acme|cash']['state'] == 'HELD'
    assert sig['mis|Acme|cash']['hold_reason'] == 'wrong row?'
    assert sig['mis|Acme|ebitda']['state'] == 'GAP'
    assert 'company' not in ''.join(sig)                     # scalar fields are not figures → excluded


def test_signature_disambiguates_concept_collision():
    # two figures for the SAME concept on one entity → 1:1 map by source location, no clobber
    rec = Record('mis', entity_id='Acme', fields={
        'cash_a': _fig('cash', Decimal('86.3'), cell='B10'),
        'cash_b': _fig('cash', Decimal('2.2'), cell='B45'),
    })
    sig = signature(RunResult(cir=CIR(records=[rec])))
    cash_keys = [k for k in sig if k.startswith('mis|Acme|cash')]
    assert len(cash_keys) == 2                               # both survive, distinct keys


# ══════════════════════════════════════════════════════════════════════════════════
# GOLDEN persistence round-trips exactly (deterministic JSON)
# ══════════════════════════════════════════════════════════════════════════════════
def test_golden_roundtrip(tmp_path):
    sig = {'mis|Acme|revenue': _row(), 'mis|Acme|cash': _row(state='HELD', value_cr='2.2')}
    p = str(tmp_path / 'g' / 'golden.json')
    save_golden(p, sig)
    assert load_golden(p) == sig
    assert load_golden(str(tmp_path / 'absent.json')) == {}   # missing golden → empty, never raises


# ══════════════════════════════════════════════════════════════════════════════════
# REAL-CORPUS TRIPWIRE — run the production path over the local corpus and assert no
# blocking drift vs the local golden. Skipped if the corpus or the golden is absent
# (fresh checkout / CI); a developer creates the golden with `dualrun snapshot`.
# ══════════════════════════════════════════════════════════════════════════════════
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
_IN = os.path.join(_BACKEND, dualrun._DEFAULT_IN)
_GOLDEN = os.path.join(_BACKEND, dualrun._DEFAULT_GOLDEN)


@pytest.mark.slow
@pytest.mark.skipif(not os.path.isdir(_IN) or not os.path.isfile(_GOLDEN),
                    reason='real corpus or local golden not present (run `dualrun snapshot`)')
def test_corpus_has_no_blocking_drift_vs_local_golden():
    cur = signature(dualrun.run_corpus(_IN))
    rep = diff(load_golden(_GOLDEN), cur)
    assert not rep.blocks, '\n' + dualrun.format_report(rep)
