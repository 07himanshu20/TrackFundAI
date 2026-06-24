"""
Phase 2 persister dry-run harness.

Loads the generic_extraction.json fixture and runs `persist_phase2` against
a temporary org + import_file. Surfaces ALL persister bugs in one pass —
no Gemini calls, no real fund data, no production DB writes (uses a
nested transaction that rolls back at the end).

Usage:
    python manage.py shell -c "from dataimport.tests.persister_dryrun import run_dryrun; run_dryrun()"

Returns a dict with counts and any errors caught per section.
"""

import json
import os
import sys
import traceback
import uuid
from pathlib import Path

from django.db import transaction


FIXTURE_PATH = Path(__file__).parent / 'fixtures' / 'generic_extraction.json'


def _ensure_test_org_and_user():
    """Get-or-create a dedicated test organization + user for dry runs.

    Lives outside the rollback so we can re-run the dry-run multiple times
    without recreating these.
    """
    from accounts.models import Organization, User

    org, _ = Organization.objects.get_or_create(
        slug='phase2-dryrun',
        defaults={'name': 'Phase2 Dryrun Org'},
    )
    user, _ = User.objects.get_or_create(
        email='phase2-dryrun@test.local',
        defaults={'username': 'phase2_dryrun', 'organization': org},
    )
    return org, user


def _real_import_file(organization, user):
    """Create a REAL ImportJob + ImportFile inside the dry-run transaction.

    The persister uses these as FKs (FundMetric.source_import_file etc.)
    so they must be real DB rows, not Python doubles. They get rolled
    back along with everything else when the outer transaction unwinds.
    """
    from dataimport.models import ImportJob, ImportFile
    from django.core.files.base import ContentFile

    job = ImportJob.objects.create(
        organization=organization,
        uploaded_by=user,
        status='processing',
    )
    f = ImportFile(job=job, original_filename='generic_extraction_fixture.xlsx',
                   status='importing', file_size=0)
    # We need to give the FileField something — use a tiny in-memory blob
    f.file.save('phase2_dryrun.bin', ContentFile(b''), save=False)
    f.save()
    return f


def run_dryrun(rollback: bool = True) -> dict:
    """Run the persister against the generic fixture.

    rollback=True (default) rolls back ALL DB writes at the end — so the
    test never pollutes real fund data. Use rollback=False only if you
    explicitly want to inspect persisted rows manually.
    """
    if not FIXTURE_PATH.exists():
        raise FileNotFoundError(f'Fixture not found: {FIXTURE_PATH}')

    with open(FIXTURE_PATH) as f:
        data = json.load(f)

    org, user = _ensure_test_org_and_user()

    from dataimport.phase2_persister import persist_phase2

    result = {'status': 'pending', 'counts': {}, 'errors': []}

    def progress_cb(pct, msg):
        print(f'  [{pct:>3}%] {msg}')

    def _run(import_file):
        return persist_phase2(data, import_file, org, user, progress_cb=progress_cb)

    try:
        if rollback:
            # Outer atomic + savepoint that we'll roll back at the end.
            # The ImportFile is created inside the savepoint so it's rolled
            # back too — nothing persists past the dry-run.
            with transaction.atomic():
                sid = transaction.savepoint()
                try:
                    import_file = _real_import_file(org, user)
                    persist_result = _run(import_file)
                    result['counts'] = persist_result.get('counts', {})
                    result['summary'] = persist_result.get('summary', '')
                    result['status'] = 'success'
                finally:
                    transaction.savepoint_rollback(sid)
        else:
            import_file = _real_import_file(org, user)
            persist_result = _run(import_file)
            result['counts'] = persist_result.get('counts', {})
            result['summary'] = persist_result.get('summary', '')
            result['status'] = 'success'
    except Exception as e:
        result['status'] = 'failed'
        result['error_type'] = type(e).__name__
        result['error_message'] = str(e)
        result['traceback'] = traceback.format_exc()

    return result


if __name__ == '__main__':
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    django.setup()
    result = run_dryrun()
    print('\n' + '=' * 80)
    print('DRY-RUN RESULT')
    print('=' * 80)
    print(f'Status: {result["status"]}')
    if result['status'] == 'success':
        print(f'Counts: {result["counts"]}')
        print(f'Summary: {result.get("summary","")}')
    else:
        print(f'Error: {result.get("error_type")}: {result.get("error_message")}')
        print('Traceback:')
        print(result.get('traceback', ''))
