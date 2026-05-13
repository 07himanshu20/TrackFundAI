"""
Celery tasks for email MIS ingestion.

Scheduled task: poll_all_mailboxes runs every 30 minutes via django-celery-beat.
"""

from celery import shared_task
import logging

logger = logging.getLogger(__name__)


@shared_task(name='emailingestion.poll_all_mailboxes', bind=True, max_retries=3)
def poll_all_mailboxes(self):
    """
    Poll IMAP mailboxes for all active organizations.
    Scheduled every 30 minutes via celery-beat.
    """
    from accounts.models import Organization
    from emailingestion.ingestor import poll_mailbox

    orgs = Organization.objects.filter(is_active=True)
    results = []

    for org in orgs:
        try:
            stats = poll_mailbox(org)
            results.append({'org': org.name, **stats})
            if stats['new'] > 0:
                logger.info(f'Org {org.name}: {stats["new"]} new emails, {stats["processed"]} processed')
        except Exception as exc:
            logger.error(f'Mailbox poll failed for org {org.name}: {exc}')
            results.append({'org': org.name, 'error': str(exc)})

    return results


@shared_task(name='emailingestion.poll_single_org')
def poll_single_org(org_id: str):
    """On-demand poll for a single organization (triggered from the UI)."""
    from accounts.models import Organization
    from emailingestion.ingestor import poll_mailbox

    try:
        org = Organization.objects.get(pk=org_id)
        return poll_mailbox(org)
    except Organization.DoesNotExist:
        logger.error(f'Organization {org_id} not found')
        return {'error': 'Organization not found'}
