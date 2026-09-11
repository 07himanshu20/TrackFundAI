"""The re-run trigger must NEVER dead-end — liveness-aware /run/ (root-cause fix for the GUI 409).

Root cause proven from production ground truth: a cold model-on run takes many minutes; during that
window a second re-run trigger (a second Confirm, a Delete, a rate-card/base-currency re-run) hit a job
still in 'processing' and the old view answered with a flat 409 that the client surfaced as a fatal
error — even though the run the user wanted was already in flight. The same flat 409 also wedged an
ORPHANED job forever: if the process was restarted mid-run the daemon worker died before writing a
terminal status, and every later /run/ refused it.

Universal fix: distinguish a LIVE run (a worker thread is alive) from an ORPHANED one (status stuck at
'processing' with no worker) — the exact liveness signal for the single-process threading model.
  • LIVE     → 202, no duplicate _kick (attach & poll the run already running).
  • ORPHANED → reclaim: status → pending and start a fresh run.
  • TERMINAL → unchanged: start a fresh run.

Each test is a NEGATIVE CONTROL against the pre-fix behaviour:
  - the old code answered 202-case and orphaned-case BOTH with 409 and never reclaimed, so the 202 and
    the reclaim assertions REDDEN on the bug — a gate only ever seen green would not prove the fix.
No Gemini, no pipeline — the worker is stubbed, so this is deterministic and token-free.
"""
import threading

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Organization, User
from dataimport import preingest3_views
from dataimport.models import PreIngestJob


class RerunLivenessTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(slug='rerun-live', name='Rerun Liveness Org')
        self.user = User.objects.create(username='gp-rerun', role='gp_admin', organization=self.org)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        # Record re-kicks instead of launching a real run (no pipeline, no Gemini).
        self._orig_kick = preingest3_views._kick
        self.kicks = []
        preingest3_views._kick = lambda job_id: self.kicks.append(str(job_id))

    def tearDown(self):
        preingest3_views._kick = self._orig_kick

    def _job(self, status):
        return PreIngestJob.objects.create(
            organization=self.org, uploaded_by=self.user, total_files=1,
            status=status, summary={'engine': preingest3_views._ENGINE})

    def _run_url(self, job):
        return reverse('preingest3-run', args=[str(job.id)])

    # ── the liveness primitive ────────────────────────────────────────────────
    def test_worker_alive_reflects_a_real_named_thread(self):
        job = self._job('processing')
        self.assertFalse(preingest3_views._worker_alive(job.id))  # no worker → not alive
        gate = threading.Event()
        t = threading.Thread(target=gate.wait, name=preingest3_views._worker_name(job.id),
                             daemon=True)
        t.start()
        try:
            self.assertTrue(preingest3_views._worker_alive(job.id))  # named worker alive
        finally:
            gate.set(); t.join(timeout=2)
        self.assertFalse(preingest3_views._worker_alive(job.id))     # gone again after it exits

    # ── LIVE run: attach, do not duplicate (reddens on the old flat 409) ───────
    def test_live_run_returns_202_and_does_not_re_kick(self):
        job = self._job('processing')
        gate = threading.Event()
        t = threading.Thread(target=gate.wait, name=preingest3_views._worker_name(job.id),
                             daemon=True)
        t.start()
        try:
            resp = self.client.post(self._run_url(job), {}, format='json')
        finally:
            gate.set(); t.join(timeout=2)
        self.assertEqual(resp.status_code, 202)              # NOT 409 — attach, don't error
        self.assertEqual(resp.data['status'], 'processing')
        self.assertEqual(self.kicks, [])                     # no duplicate run launched
        job.refresh_from_db()
        self.assertEqual(job.status, 'processing')           # left untouched

    # ── ORPHANED run: reclaim and restart (reddens on the old flat 409) ────────
    def test_orphaned_processing_job_is_reclaimed_and_restarted(self):
        job = self._job('processing')                        # 'processing' but NO worker thread alive
        self.assertFalse(preingest3_views._worker_alive(job.id))
        resp = self.client.post(self._run_url(job), {}, format='json')
        self.assertEqual(resp.status_code, 200)              # NOT 409 — reclaimed, not wedged
        self.assertEqual(resp.data['status'], 'pending')
        self.assertEqual(self.kicks, [str(job.id)])          # a fresh run was launched
        job.refresh_from_db()
        self.assertEqual(job.status, 'pending')

    # ── TERMINAL run: unchanged normal re-run ─────────────────────────────────
    def test_completed_job_reruns_normally(self):
        job = self._job('completed_with_errors')
        resp = self.client.post(self._run_url(job), {}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'pending')
        self.assertEqual(self.kicks, [str(job.id)])
