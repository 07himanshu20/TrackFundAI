"""
Email MIS Ingestion models.

Portfolio companies email P&L / Balance Sheet / Cash Flow data to the GP mailbox.
This app polls the mailbox (IMAP), downloads attachments, and triggers the
dataimport pipeline on each attachment.

Flow:
  MailboxPollJob → ingests → EmailMISSubmission → triggers → ImportFile (dataimport)
"""

import uuid
from django.conf import settings
from django.db import models


class EmailMISSubmission(models.Model):
    """
    Tracks each email received from a portfolio company with MIS data.
    """
    STATUS_CHOICES = [
        ('received',   'Received'),
        ('parsing',    'Parsing'),
        ('imported',   'Imported'),
        ('failed',     'Failed'),
        ('duplicate',  'Duplicate'),
        ('ignored',    'Ignored — no attachment'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='email_mis_submissions',
    )

    # Email metadata (from IMAP headers)
    email_uid = models.CharField(
        max_length=100,
        help_text='IMAP UID of the email — used to detect duplicates',
    )
    sender_email = models.EmailField(help_text='Sender email address')
    sender_name = models.CharField(max_length=255, blank=True)
    subject = models.CharField(max_length=500, blank=True)
    received_at = models.DateTimeField(help_text='When the email arrived in the mailbox')

    # Matched portfolio company (Gemini-matched from sender email + subject)
    portfolio_company = models.ForeignKey(
        'investments.PortfolioCompany',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='email_submissions',
        help_text='Matched portfolio company (Gemini semantic match on sender + subject)',
    )

    # Attachment processing
    attachment_filename = models.CharField(max_length=500, blank=True)
    attachment_content_type = models.CharField(max_length=100, blank=True)

    # Link to import file (created after attachment is saved and processed)
    import_file = models.ForeignKey(
        'dataimport.ImportFile',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='email_submission',
    )

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='received')
    error_message = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-received_at']
        unique_together = ('organization', 'email_uid')

    def __str__(self):
        return f'Email from {self.sender_email} — {self.received_at:%Y-%m-%d} ({self.status})'


class MailboxPollLog(models.Model):
    """
    Log of each IMAP polling run — for debugging and scheduling health.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='mailbox_poll_logs',
    )
    polled_at = models.DateTimeField(auto_now_add=True)
    emails_found = models.IntegerField(default=0)
    emails_new = models.IntegerField(default=0, help_text='New emails not seen before')
    emails_processed = models.IntegerField(default=0)
    error_message = models.TextField(blank=True)
    success = models.BooleanField(default=True)

    class Meta:
        ordering = ['-polled_at']

    def __str__(self):
        return f'Poll {self.polled_at:%Y-%m-%d %H:%M} — {self.emails_new} new'
