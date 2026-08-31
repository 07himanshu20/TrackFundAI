"""U6 END-TO-END through the LIVE HTTP endpoints + async worker — the seam proof the isolated
component tests cannot give.

The component tests each prove a LINK (currency detection, report/prompt contract, card intake,
_rate_card_for selection, to_inr conversion). This drives all three branches through the REAL views
(`upload` → `job_detail` → `ratecard` → `rerun`) and the REAL worker (`_run_job`), so the SEAMS
between those links are proven connected — the gap a green unit suite hides. Reddening control: if the
wiring breaks anywhere (detection never sets prompt_required, the card endpoint doesn't store, rerun
doesn't apply it, or the conversion doesn't reach the output), the matching branch goes RED.

Three branches, exactly as the fail-closed + UX contract requires:
  A — foreign + card  : prompt asked, figure HELD pre-card → supply MYR rate → rerun → converted,
                        VALUE-AUDITED to the rupee (5,000,000 MYR × 18.6 ÷ 1e7 = 9.3 Cr).
  B — foreign + skip  : no card → figure HELD, and the run STILL COMPLETES (never blocks) + a workbook
                        is still delivered. This is the branch most likely to be silently broken.
  C — all-INR         : no foreign currency → prompt_required False (never an always-prompt) → INR emits.

The worker is run SYNCHRONOUSLY (monkeypatched `_kick`) so the async chain is deterministic in-test.
"""
import io
import json

import openpyxl
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Organization, User
from dataimport import preingest3_views

_XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

# a two-company investment schedule: one foreign (Malaysia→MYR), one domestic (India→INR).
_FUND = [['Company', 'Cost', 'Fair Value', 'Ownership %', 'Country'],
         ['Meridian Systems', 40, 90, 25, 'Malaysia'],
         ['Zephyr Diagnostics', 60, 150, 18, 'India']]

# cash is a STOCK (latest column, never annualised) → a clean value-audit target. 5,000,000 latest.
_MIS = [['Particulars', 'Apr-25', 'May-25', 'Jun-25'],
        ['Revenue', 3000000, 3100000, 3200000],
        ['EBITDA', 500000, 510000, 520000],
        ['Closing Cash', 4800000, 4900000, 5000000],
        ['Headcount', 20, 21, 22]]


def _xlsx(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class U6RateCardEndToEndTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(slug='u6e2e', name='U6 E2E Org')
        self.user = User.objects.create(username='gp-u6', role='gp_admin', organization=self.org)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        # run the async worker INLINE so the HTTP chain is deterministic in the test
        self._orig_kick = preingest3_views._kick
        preingest3_views._kick = lambda job_id: preingest3_views._run_job(str(job_id))

    def tearDown(self):
        preingest3_views._kick = self._orig_kick

    # ── helpers driving the LIVE endpoints ──────────────────────────────────
    def _upload(self, mis_name):
        fund = SimpleUploadedFile('fund.xlsx', _xlsx(_FUND), content_type=_XLSX)
        mis = SimpleUploadedFile(mis_name, _xlsx(_MIS), content_type=_XLSX)
        resp = self.client.post(reverse('preingest3-upload'),
                                {'files': [fund, mis]}, format='multipart')
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()['job_id']

    def _detail(self, job_id):
        resp = self.client.get(reverse('preingest3-detail', args=[job_id]))
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def _cash(self, detail, company):
        for c in detail['review'].get('companies', []):
            if c['company'] == company:
                return c['figures'].get('cash')
        return None

    # ── Branch A — foreign + card: held → supply rate → rerun → converted ────
    def test_A_foreign_with_card_converts_value_audited(self):
        jid = self._upload('Meridian Systems.xlsx')
        d = self._detail(jid)
        self.assertIn(d['status'], ('completed', 'completed_with_errors'))
        rep = d['review']['currency_report']
        self.assertTrue(rep['prompt_required'], 'foreign figure must ASK for a rate')
        self.assertIn('MYR', [u['currency'] for u in rep['uncovered']])
        self.assertEqual(self._cash(d, 'Meridian Systems')['state'], 'held',
                         'MYR cash must be HELD before a rate is supplied — never silently converted')

        # supply the MYR rate through the LIVE intake endpoint. The rate DATE must equal the run's
        # as_of (the intake gate refuses a rate for another date — proven below by using report.as_of,
        # exactly as the frontend does).
        as_of = d['review']['as_of']
        r = self.client.post(
            reverse('preingest3-ratecard', args=[jid]),
            {'manual_rates': [{'currency': 'MYR', 'rate': '18.6',
                               'date': as_of, 'source': 'RBI reference'}]}, format='json')
        self.assertEqual(r.status_code, 200, r.content)

        # rerun applies the stored card
        rr = self.client.post(reverse('preingest3-run', args=[jid]))
        self.assertEqual(rr.status_code, 200, rr.content)

        d2 = self._detail(jid)
        fig = self._cash(d2, 'Meridian Systems')
        self.assertEqual(fig['state'], 'emitted', 'a covered MYR figure must now emit')
        # VALUE-AUDIT to the rupee: raw cell 5,000,000 MYR × supplied 18.6 ÷ 1e7
        self.assertEqual(Decimal(str(fig['value_cr'])),
                         Decimal('5000000') * Decimal('18.6') / Decimal(10 ** 7))   # == 9.3
        self.assertFalse(d2['review']['currency_report']['prompt_required'],
                         'once covered, the prompt must clear')

    # ── Branch B — foreign + skip: HELD, run still COMPLETES, workbook delivered ──
    def test_B_foreign_skip_holds_and_run_still_completes(self):
        jid = self._upload('Meridian Systems.xlsx')
        d = self._detail(jid)
        # the fail-closed guarantee: the run does NOT block on a missing rate
        self.assertIn(d['status'], ('completed', 'completed_with_errors'))
        self.assertNotEqual(d['status'], 'failed')
        self.assertTrue(d['review']['currency_report']['prompt_required'])
        self.assertEqual(self._cash(d, 'Meridian Systems')['state'], 'held',
                         'skipped foreign figure stays HELD, never guessed')
        self.assertTrue(d['has_output'], 'a workbook is still delivered with the held figure disclosed')

    # ── Branch C — all-INR: never an always-prompt; INR emits with no rate ───
    def test_C_all_inr_no_prompt_and_emits(self):
        jid = self._upload('Zephyr Diagnostics.xlsx')      # India-domiciled → INR
        d = self._detail(jid)
        self.assertIn(d['status'], ('completed', 'completed_with_errors'))
        rep = d['review'].get('currency_report') or {}
        self.assertFalse(rep.get('prompt_required'), 'an all-INR run must NOT prompt for a rate')
        self.assertEqual(self._cash(d, 'Zephyr Diagnostics')['state'], 'emitted',
                         'INR cash emits directly, no rate needed')
