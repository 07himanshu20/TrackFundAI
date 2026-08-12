"""Library-level proof of the review-gate WRITE-BACK loop (Django-free).

The whole 'learn once' promise in one test: a company MIS whose name/content do not
uniquely match any fund anchor is HELD for review (never mis-attributed). A human
confirms the alias into the org-scoped store; on RE-RUN the ledger lookup resolves
it and the file leaves the review queue — attributed, permanently, for that org.
Also proves org isolation: org B's store never sees org A's confirmed alias.
"""
import os
import tempfile

import openpyxl

from backend.dataimport.preingest3 import pipeline
from backend.dataimport.preingest3.alias_ledger import AliasLedger
from backend.dataimport.preingest3.ratecard import default_inr_card


def _xlsx(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def _fund(d):
    return _xlsx(os.path.join(d, 'fund_schedule.xlsx'),
                 [['Company', 'Cost', 'Fair Value', 'Ownership %'],
                  ['Acme Labs', 40, 90, 25],
                  ['Zephyr Diagnostics', 60, 150, 18]])


def _mis(d):
    # a company MIS whose filename/content carry NO distinctive token of either
    # anchor → the closed-set matcher holds it (correctly refuses to guess).
    return _xlsx(os.path.join(d, 'monthly_report_q2.xlsx'),
                 [['Particulars', 'Apr-25', 'May-25', 'Jun-25'],
                  ['Revenue', 10, 11, 12],
                  ['EBITDA', 2, 2, 3],
                  ['Closing Cash', 5, 6, 7],
                  ['Headcount', 20, 21, 22]])


def _store(d):
    return AliasLedger(org='orgA', path=os.path.join(d, 'aliases.json'))


def test_held_then_confirm_then_resolves_on_rerun():
    with tempfile.TemporaryDirectory() as d:
        files = [('fund_schedule', _fund(d)), ('monthly_report_q2', _mis(d))]
        store = _store(d)
        rc = default_inr_card('2026-06-30')

        # run 1 — the MIS file holds for review (no unique match), never guessed
        r1 = pipeline.run(files, as_of='2026-06-30', org='orgA', rate_card=rc, alias_store=store)
        held = [rf for rf in r1.review_queue if rf.label == 'monthly_report_q2']
        assert held, 'unmatched MIS must be HELD for alias review, not mis-attributed'
        names = {c['name'] for c in held[0].candidates}
        assert names == {'Acme Labs', 'Zephyr Diagnostics'}          # closed candidate set shown

        # human confirms the alias → org-scoped store, provenance='human'
        store.learn(held[0].identifiers, 'zephyr diagnostics', provenance='human')

        # run 2 — ledger hit resolves it; it LEAVES the review queue, attributed
        r2 = pipeline.run(files, as_of='2026-06-30', org='orgA', rate_card=rc, alias_store=store)
        assert not any(rf.label == 'monthly_report_q2' for rf in r2.review_queue)
        rep = next(fr for fr in r2.files if fr.label == 'monthly_report_q2')
        assert rep.entity_id == 'Zephyr Diagnostics'                 # attribution stuck


def test_bijection_guard_holds_colliding_files():
    # two files that both resolve to the SAME company must BOTH hold — never emit
    # scrambled records. This makes the 'Hubbler x3' mis-attribution structurally
    # impossible: a company claimed by >1 file this run is a resolution error.
    with tempfile.TemporaryDirectory() as d:
        pl = [['Particulars', 'Apr-25', 'May-25', 'Jun-25'],
              ['Revenue', 10, 11, 12], ['EBITDA', 2, 2, 3],
              ['Closing Cash', 5, 6, 7], ['Headcount', 20, 21, 22]]
        files = [('fund_schedule', _fund(d)),
                 ('zephyr_report_A', _xlsx(os.path.join(d, 'zephyr_report_A.xlsx'), pl)),
                 ('zephyr_report_B', _xlsx(os.path.join(d, 'zephyr_report_B.xlsx'), pl))]
        r = pipeline.run(files, as_of='2026-06-30', org='bij',
                         rate_card=default_inr_card('2026-06-30'), alias_store=_store(d))
        zs = [fr for fr in r.files if fr.label.startswith('zephyr_report')]
        assert len(zs) == 2 and all(fr.status == 'held' for fr in zs)     # both held
        assert any('collision' in (fr.reason or '') for fr in zs)
        # not one scrambled Zephyr MIS record emitted
        assert not any(rec.domain == 'mis' and rec.entity_id == 'Zephyr Diagnostics'
                       for rec in r.cir.records)


def test_confirmed_alias_is_org_scoped_no_cross_tenant_bleed():
    with tempfile.TemporaryDirectory() as d:
        files = [('fund_schedule', _fund(d)), ('monthly_report_q2', _mis(d))]
        rc = default_inr_card('2026-06-30')
        a = AliasLedger(org='orgA', path=os.path.join(d, 'a.json'))
        b = AliasLedger(org='orgB', path=os.path.join(d, 'b.json'))

        r1 = pipeline.run(files, as_of='2026-06-30', org='orgA', rate_card=rc, alias_store=a)
        ids = r1.review_queue[0].identifiers
        a.learn(ids, 'zephyr diagnostics', provenance='human')       # org A confirms

        # org B, same files, its OWN store → still held (A's alias must not leak)
        r2 = pipeline.run(files, as_of='2026-06-30', org='orgB', rate_card=rc, alias_store=b)
        assert any(rf.label == 'monthly_report_q2' for rf in r2.review_queue)


if __name__ == '__main__':
    test_held_then_confirm_then_resolves_on_rerun()
    test_confirmed_alias_is_org_scoped_no_cross_tenant_bleed()
    print('ALL PASS — held → confirm → resolves; org-scoped, no cross-tenant bleed')
