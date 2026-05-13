"""
Email MIS ingestor — polls IMAP mailbox and triggers import on attachments.

Uses imapclient for IMAP access. Gemini is used to semantically match
the sender/subject to the correct portfolio company.

Security notes:
  - Email credentials stored in environment variables only (never in DB)
  - Attachment bytes are never stored on disk permanently — processed in-memory
    then saved via Django's storage backend

v5 additions:
  - _send_bounce_email: Sends bounce reply to sender when no valid attachment found
  - _notify_stakeholders: Sends Notification records to GP/analyst users after import
"""

import email
import logging
import os
import tempfile
from datetime import datetime
from email.header import decode_header, make_header

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.mail import send_mail
from django.utils import timezone

logger = logging.getLogger(__name__)


def _send_bounce_email(sender_email: str, subject: str, reason: str):
    """
    Send a bounce/rejection email to the original sender when an MIS submission
    cannot be processed (no valid Excel attachment, unrecognised company, etc.).
    """
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@trackfundai.com')
    try:
        send_mail(
            subject=f'Re: {subject} — MIS Submission Could Not Be Processed',
            message=(
                f'Dear Sender,\n\n'
                f'Thank you for submitting your MIS data to TrackFundAI.\n\n'
                f'Unfortunately, your submission could not be processed automatically.\n\n'
                f'Reason: {reason}\n\n'
                f'Please check the following and resubmit:\n'
                f'  1. Attach an Excel file (.xlsx) or PDF with your MIS data.\n'
                f'  2. Ensure the file follows the expected format (contact your fund manager if unsure).\n'
                f'  3. Include the company name in the email subject for accurate routing.\n\n'
                f'If you believe this is an error, please contact your fund administrator.\n\n'
                f'This is an automated message. Please do not reply to this email.\n\n'
                f'— TrackFundAI Automated Ingestion System'
            ),
            from_email=from_email,
            recipient_list=[sender_email],
            fail_silently=True,
        )
        logger.info(f'Bounce email sent to {sender_email} — reason: {reason}')
    except Exception as e:
        logger.warning(f'Failed to send bounce email to {sender_email}: {e}')


def _notify_stakeholders(organization, submission, company):
    """
    After a successful MIS import, notify relevant stakeholders (GP Admin + Analysts)
    via internal Notification records and optionally email.
    """
    try:
        from accounts.models import User, Notification
        stakeholders = User.objects.filter(
            organization=organization,
            role__in=['gp_admin', 'analyst', 'fund_accountant'],
            is_active=True,
        )
        company_name = company.name if company else 'Unknown Company'
        title = f'MIS Import Complete — {company_name}'
        message = (
            f'A new MIS submission from {submission.sender_email} '
            f'has been automatically imported for {company_name}.\n'
            f'File: {submission.attachment_filename}\n'
            f'Received: {submission.received_at.strftime("%d %b %Y %H:%M")}'
        )
        for user in stakeholders:
            Notification.objects.create(
                user=user,
                title=title,
                message=message,
                notification_type='mis_import',
                severity='low',
            )
        logger.info(f'Stakeholder notifications sent for MIS import: {company_name}')
    except Exception as e:
        logger.warning(f'Stakeholder notification failed: {e}')


def _decode_header_value(raw):
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw) if raw else ''


def _match_company_with_gemini(organization, sender_email: str, subject: str):
    """
    Use Gemini to semantically match the email sender/subject to a
    PortfolioCompany in the organization.

    Returns PortfolioCompany instance or None.
    """
    from investments.models import PortfolioCompany
    import google.generativeai as genai

    companies = list(
        PortfolioCompany.objects.filter(organization=organization, is_active=True)
        .values('id', 'name', 'website', 'cin')
    )
    if not companies:
        return None

    company_list = '\n'.join(
        f"- {c['name']} (website: {c.get('website','')}, CIN: {c.get('cin','')})"
        for c in companies
    )

    prompt = f"""You are a portfolio company identifier for a fund management system.

Given this email:
Sender: {sender_email}
Subject: {subject}

And this list of portfolio companies:
{company_list}

Which portfolio company is MOST LIKELY sending this email?
Return ONLY the exact company name from the list above, or "UNKNOWN" if no match.
Do not include any explanation."""

    try:
        api_key = settings.GEMINI_API_KEY
        if not api_key:
            return None
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(settings.GEMINI_MODEL)
        result = model.generate_content(prompt)
        matched_name = result.text.strip().strip('"').strip("'")

        if matched_name and matched_name != 'UNKNOWN':
            company = PortfolioCompany.objects.filter(
                organization=organization,
                name__iexact=matched_name,
            ).first()
            return company
    except Exception as e:
        logger.warning(f'Gemini company match failed: {e}')

    return None


def poll_mailbox(organization):
    """
    Poll the IMAP mailbox for new MIS emails and process attachments.

    Args:
        organization: accounts.Organization instance

    Returns:
        dict with { polled: int, new: int, processed: int, errors: int }
    """
    from emailingestion.models import EmailMISSubmission, MailboxPollLog

    host = settings.MIS_EMAIL_HOST
    port = settings.MIS_EMAIL_PORT
    user = settings.MIS_EMAIL_USER
    password = settings.MIS_EMAIL_PASSWORD
    folder = settings.MIS_EMAIL_FOLDER

    if not user or not password:
        logger.warning('Email credentials not configured — skipping mailbox poll')
        return {'polled': 0, 'new': 0, 'processed': 0, 'errors': 0}

    stats = {'polled': 0, 'new': 0, 'processed': 0, 'errors': 0}

    try:
        import imapclient
        server = imapclient.IMAPClient(host, port=port, ssl=True)
        server.login(user, password)
        server.select_folder(folder, readonly=False)

        # Fetch all UNSEEN messages
        message_ids = server.search(['UNSEEN'])
        stats['polled'] = len(message_ids)

        if not message_ids:
            server.logout()
            MailboxPollLog.objects.create(
                organization=organization,
                emails_found=0, emails_new=0, emails_processed=0,
            )
            return stats

        # Fetch full message bodies
        messages = server.fetch(message_ids, ['RFC822', 'UID'])

        for msg_id, data in messages.items():
            uid = str(data.get(b'UID', msg_id))
            raw = data.get(b'RFC822', b'')
            if not raw:
                continue

            msg = email.message_from_bytes(raw)
            sender = _decode_header_value(msg.get('From', ''))
            subject = _decode_header_value(msg.get('Subject', ''))
            date_str = msg.get('Date', '')

            # Parse received_at
            try:
                from email.utils import parsedate_to_datetime
                received_at = parsedate_to_datetime(date_str)
                if received_at.tzinfo is None:
                    received_at = timezone.make_aware(received_at)
            except Exception:
                received_at = timezone.now()

            # Extract sender email
            import re
            email_match = re.search(r'<([^>]+)>', sender)
            sender_email_addr = email_match.group(1) if email_match else sender.strip()

            # Duplicate check
            if EmailMISSubmission.objects.filter(
                organization=organization, email_uid=uid
            ).exists():
                continue

            stats['new'] += 1

            # Create submission record
            submission = EmailMISSubmission.objects.create(
                organization=organization,
                email_uid=uid,
                sender_email=sender_email_addr,
                sender_name=sender,
                subject=subject,
                received_at=received_at,
                status='received',
            )

            # Match portfolio company
            company = _match_company_with_gemini(organization, sender_email_addr, subject)
            if company:
                submission.portfolio_company = company
                submission.save(update_fields=['portfolio_company'])

            # Find Excel/PDF attachments
            processed = False
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get('Content-Disposition', ''))

                is_excel = content_type in (
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    'application/vnd.ms-excel',
                    'application/octet-stream',
                )
                is_pdf = content_type == 'application/pdf'
                has_attachment = 'attachment' in content_disposition

                if (is_excel or is_pdf) and has_attachment:
                    filename = part.get_filename('')
                    if not filename:
                        continue

                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue

                    submission.attachment_filename = filename
                    submission.attachment_content_type = content_type
                    submission.status = 'parsing'
                    submission.save(update_fields=['attachment_filename', 'attachment_content_type', 'status'])

                    try:
                        _trigger_import(organization, submission, payload, filename)
                        submission.status = 'imported'
                        stats['processed'] += 1
                        processed = True
                        # Notify stakeholders of successful import
                        _notify_stakeholders(organization, submission, company)
                    except Exception as e:
                        submission.status = 'failed'
                        submission.error_message = str(e)
                        stats['errors'] += 1
                        logger.error(f'Import failed for email {uid}: {e}')
                        # Send bounce to sender on import failure
                        _send_bounce_email(
                            sender_email_addr, subject,
                            f'File processing error: {str(e)[:200]}. '
                            f'Please verify the file format and resubmit.',
                        )

                    submission.save(update_fields=['status', 'error_message'])
                    break  # Process first valid attachment only

            if not processed and submission.status == 'received':
                submission.status = 'no_attachment'
                submission.save(update_fields=['status'])
                # Bounce: no valid Excel/PDF attachment found
                _send_bounce_email(
                    sender_email_addr, subject,
                    'No valid Excel (.xlsx) or PDF attachment was found in your email. '
                    'Please attach your MIS file and send again.',
                )

        server.logout()

    except Exception as e:
        logger.error(f'Mailbox poll error: {e}')
        stats['errors'] += 1
        MailboxPollLog.objects.create(
            organization=organization,
            emails_found=stats['polled'],
            emails_new=stats['new'],
            emails_processed=stats['processed'],
            error_message=str(e),
            success=False,
        )
        return stats

    MailboxPollLog.objects.create(
        organization=organization,
        emails_found=stats['polled'],
        emails_new=stats['new'],
        emails_processed=stats['processed'],
        success=True,
    )
    return stats


def _trigger_import(organization, submission, payload: bytes, filename: str):
    """
    Save attachment to Django storage and trigger the dataimport pipeline.
    Creates an ImportJob + ImportFile, then runs the import.
    """
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage
    from dataimport.models import ImportJob, ImportFile
    from dataimport.import_service import run_import

    # Get or create a system user for this org
    from django.contrib.auth import get_user_model
    User = get_user_model()
    system_user = User.objects.filter(organization=organization).first()

    # Create ImportJob
    import_job = ImportJob.objects.create(
        organization=organization,
        uploaded_by=system_user,
        status='pending',
        progress_message=f'Auto-imported from email: {submission.sender_email}',
    )

    # Save file content as ContentFile (in-memory, goes to Django storage)
    file_content = ContentFile(payload, name=filename)

    # Create ImportFile
    import_file = ImportFile.objects.create(
        job=import_job,
        file=file_content,
        original_filename=filename,
        file_size=len(payload),
        status='pending',
    )

    submission.import_file = import_file
    submission.save(update_fields=['import_file'])

    # Run import (sync — Celery task wraps this for async)
    run_import(import_file)
