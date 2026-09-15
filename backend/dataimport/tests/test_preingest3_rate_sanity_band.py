"""Foreign-safety #2 — the rate-entry sanity band at the /ratecard/ endpoint.

A typo band is a COMPARISON: 23.38 and 233.8 are both valid-looking; only a REFERENCE distinguishes them.
The reference is the currency's OWN prior entered rate (this job's stored card, then the most recent prior
org job that carried it — reference-only, never a conversion input, self-healing). A near-power-of-ten
shift is a decimal-point typo → the rate is HELD for review ('confirm or re-enter') and dropped from the
card, so the currency holds via the existing uniform-currency (FX_UNCOVERED) engine — no new hold type. A
genuine ±% move converts. A FIRST upload (no prior to check) converts with a soft 'unverified' disclosure —
never a forced confirmation (a confirm with nothing to check is theatre). An explicit confirmed:true
overrides. Reddening BOTH directions + the cross-job reference lookback.
"""
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Organization, User
from dataimport import preingest3_views
from dataimport.models import PreIngestJob

AS_OF = '2026-09-10'


class RateSanityBandTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(slug='rcband', name='RC Band Org')
        self.user = User.objects.create(username='gp-band', role='gp_admin', organization=self.org)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _job(self, *, stored=None, uncovered=('MYR',), status='completed'):
        summary = {'engine': preingest3_views._ENGINE, 'as_of': AS_OF,
                   'currency_report': {'uncovered': [{'currency': c} for c in uncovered]}}
        if stored:
            summary['rate_card'] = {'as_of': AS_OF,
                                    'rates': [{'currency': c, 'inr_per_unit': r, 'rate_date': AS_OF,
                                               'source': 'prior'} for c, r in stored.items()]}
        return PreIngestJob.objects.create(organization=self.org, uploaded_by=self.user,
                                           total_files=1, status=status, summary=summary)

    def _supply(self, job, currency, rate, *, confirmed=False):
        entry = {'currency': currency, 'rate': rate, 'date': AS_OF, 'source': 'test'}
        if confirmed:
            entry['confirmed'] = True
        return self.client.post(reverse('preingest3-ratecard', args=[str(job.id)]),
                                {'manual_rates': [entry]}, format='json')

    def _stored(self, job):
        job.refresh_from_db()
        return {r['currency']: str(r['inr_per_unit']) for r in (job.summary.get('rate_card') or {}).get('rates', [])}

    # ── MUST-HANDLE: a ×10 decimal-shift vs the currency's own prior rate is held for review ───────────
    def test_decimal_shift_typo_is_held_for_review_not_converted(self):
        job = self._job(stored={'MYR': '23.38'})            # the prior (in-loop) reference
        r = self._supply(job, 'MYR', '233.8')               # ×10 fat-finger
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(any(s['currency'] == 'MYR' for s in r.data['suspect_rates']),
                        'a ×10 shift vs the prior rate must be held for review')
        self.assertEqual(self._stored(job)['MYR'], '23.38',
                         'the suspected typo must NOT overwrite the good stored rate (held, not applied)')

    def test_reference_is_found_cross_job_and_held_currency_stays_uncovered(self):
        # the reference lookback spans prior ORG jobs, not just this job's own card; and with no good rate
        # on THIS job, a held suspect leaves the currency uncovered → it holds via the uniform-currency
        # engine (no new hold type).
        self._job(stored={'MYR': '23.38'})                  # a prior completed job carrying MYR
        fresh = self._job(stored=None)                      # a new job with no stored card
        r = self._supply(fresh, 'MYR', '233.8')
        self.assertTrue(any(s['currency'] == 'MYR' for s in r.data['suspect_rates']),
                        'the prior job\'s MYR rate must serve as the cross-job reference')
        self.assertNotIn('MYR', self._stored(fresh), 'a held suspect must not land on the card')
        self.assertIn('MYR', r.data['still_uncovered'], 'held suspect → currency uncovered → holds')

    # ── MUST-NOT-MISFIRE ──────────────────────────────────────────────────────────────────────────────
    def test_genuine_fx_move_converts(self):
        job = self._job(stored={'MYR': '18.6'})
        r = self._supply(job, 'MYR', '19.2')                # a real ~3% move — not a power of ten
        self.assertEqual(self._stored(job)['MYR'], '19.2', 'a genuine ±% move must convert, not hold')
        self.assertFalse(r.data['suspect_rates'])

    def test_first_upload_converts_with_unverified_disclosure(self):
        job = self._job(stored=None)                        # no prior MYR anywhere → nothing to check
        r = self._supply(job, 'MYR', '233.8')
        self.assertEqual(self._stored(job)['MYR'], '233.8',
                         'a first-upload rate must convert (a confirm with nothing to check is theatre)')
        self.assertFalse(r.data['suspect_rates'])
        self.assertTrue(any(u['currency'] == 'MYR' for u in r.data['unverified_rates']),
                        'a first upload must be disclosed as unverified (no prior to check)')

    def test_explicit_confirm_overrides_the_hold(self):
        job = self._job(stored={'MYR': '23.38'})
        r = self._supply(job, 'MYR', '233.8', confirmed=True)   # human attests the unusual rate
        self.assertEqual(self._stored(job)['MYR'], '233.8', 'confirmed:true must apply the unusual rate')
        self.assertFalse(r.data['suspect_rates'])

    def test_suspect_clears_when_the_rate_is_corrected(self):
        job = self._job(stored={'MYR': '23.38'})
        self._supply(job, 'MYR', '233.8')                   # held for review
        r = self._supply(job, 'MYR', '23.50')               # re-enter the correct rate
        self.assertEqual(self._stored(job)['MYR'], '23.50')
        self.assertFalse(any(s['currency'] == 'MYR' for s in r.data['suspect_rates']),
                         'correcting the rate must clear the suspect-review flag')
