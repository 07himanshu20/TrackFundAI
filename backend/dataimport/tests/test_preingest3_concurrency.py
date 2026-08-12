"""Multi-tenant CONCURRENCY guarantee for the preingest3 review-gate store.

The single-user HTTP proof shows the loop works; this proves it stays correct under
the concurrency a real deployment has: many runs in flight at once across tenants,
and a reviewer confirming an alias WHILE runs are executing. The DB-backed alias
ledger (select_for_update, per-org rows) must yield: no exceptions, no cross-tenant
attribution, and no lost/corrupted writes — the multi-worker safety the JSON+
threading.Lock store could not give.

Uses TransactionTestCase so each thread's own DB connection sees committed rows.
"""
import os
import tempfile
import threading

import openpyxl
from django.db import connection
from django.test import TransactionTestCase

from accounts.models import Organization
from dataimport.models import PreIngestAlias
from dataimport.preingest3 import pipeline
from dataimport.preingest3.ratecard import default_inr_card
from dataimport.preingest3_store import DbAliasLedger


def _xlsx(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


class PreIngest3ConcurrencyTest(TransactionTestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        fund = _xlsx(os.path.join(d, 'fund.xlsx'),
                     [['Company', 'Cost', 'Fair Value', 'Ownership %'],
                      ['Acme Labs', 40, 90, 25], ['Zephyr Diagnostics', 60, 150, 18]])
        mis = _xlsx(os.path.join(d, 'monthly_report_q2.xlsx'),
                    [['Particulars', 'Apr-25', 'May-25', 'Jun-25'],
                     ['Revenue', 10, 11, 12], ['EBITDA', 2, 2, 3],
                     ['Closing Cash', 5, 6, 7], ['Headcount', 20, 21, 22]])
        self.files = [('fund', fund), ('monthly_report_q2', mis)]
        self.rc = default_inr_card('2026-06-30')
        self.A = Organization.objects.create(slug='cc-A', name='CC A')
        self.B = Organization.objects.create(slug='cc-B', name='CC B')

    def _run(self, org):
        return pipeline.run(self.files, as_of='2026-06-30', org=str(org.id),
                            rate_card=self.rc, alias_store=DbAliasLedger(org))

    def test_two_orgs_concurrent_plus_resolve_during_run(self):
        # discover identifiers + candidate ids, confirm DIFFERENT targets per org
        r0 = self._run(self.A)
        ids = r0.review_queue[0].identifiers
        acme = next(c['id'] for c in r0.review_queue[0].candidates if 'Acme' in c['name'])
        zeph = next(c['id'] for c in r0.review_queue[0].candidates if 'Zephyr' in c['name'])
        DbAliasLedger(self.A).learn(ids, acme, provenance='human')
        DbAliasLedger(self.B).learn(ids, zeph, provenance='human')

        results, errors = {}, []

        def worker(org, expect, idx):
            try:
                r = self._run(org)
                rep = next(fr for fr in r.files if fr.label == 'monthly_report_q2')
                results[idx] = (rep.entity_id, expect)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
            finally:
                connection.close()

        def racer():
            try:
                for _ in range(30):
                    DbAliasLedger(self.A).learn(ids, acme, provenance='human')
            finally:
                connection.close()

        threads = []
        for i in range(20):
            org, exp = (self.A, 'Acme Labs') if i % 2 == 0 else (self.B, 'Zephyr Diagnostics')
            threads.append(threading.Thread(target=worker, args=(org, exp, i)))
        threads.append(threading.Thread(target=racer))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])                              # no concurrency exceptions
        self.assertEqual(len(results), 20)
        bleeds = [v for v in results.values() if v[0] != v[1]]
        self.assertEqual(bleeds, [])                              # no cross-tenant attribution
        # both orgs' human bindings intact (no lost update / corruption)
        self.assertEqual(set(PreIngestAlias.objects.filter(organization=self.A)
                             .values_list('entity_id', flat=True)), {acme})
        self.assertEqual(set(PreIngestAlias.objects.filter(organization=self.B)
                             .values_list('entity_id', flat=True)), {zeph})
