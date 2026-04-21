import uuid
from django.conf import settings
from django.db import models


def document_upload_path(instance, filename):
    """Store documents under media/documents/<org_slug>/<fund_id>/filename."""
    org_slug = 'default'
    if instance.fund and instance.fund.organization:
        org_slug = instance.fund.organization.slug
    fund_id = instance.fund_id or 'general'
    return f'documents/{org_slug}/{fund_id}/{filename}'


class Document(models.Model):
    """Fund document stored in the vault."""

    CATEGORY_CHOICES = [
        ('ppm', 'Private Placement Memorandum'),
        ('subscription', 'Subscription Agreement'),
        ('contribution', 'Contribution Agreement'),
        ('capital_call', 'Capital Call Notice'),
        ('distribution', 'Distribution Notice'),
        ('valuation', 'Valuation Report'),
        ('audit', 'Audit Report'),
        ('compliance', 'Compliance Report'),
        ('financial', 'Financial Statement'),
        ('legal', 'Legal Document'),
        ('board', 'Board Resolution / Minutes'),
        ('kyc', 'KYC Document'),
        ('other', 'Other'),
    ]

    VISIBILITY_CHOICES = [
        ('internal', 'Internal Only (GP)'),
        ('lp_visible', 'LP Visible'),
        ('public', 'Public'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='documents',
    )
    fund = models.ForeignKey(
        'funds.Fund',
        on_delete=models.CASCADE,
        related_name='documents',
        null=True, blank=True,
        help_text='Null for organization-level documents',
    )
    scheme = models.ForeignKey(
        'funds.Scheme',
        on_delete=models.SET_NULL,
        related_name='documents',
        null=True, blank=True,
    )

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='other')
    visibility = models.CharField(max_length=15, choices=VISIBILITY_CHOICES, default='internal')

    file = models.FileField(upload_to=document_upload_path)
    file_name = models.CharField(max_length=255)
    file_size = models.PositiveIntegerField(help_text='File size in bytes')
    mime_type = models.CharField(max_length=100, blank=True)

    version = models.PositiveIntegerField(default=1)
    tags = models.JSONField(default=list, blank=True)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', '-created_at']),
            models.Index(fields=['fund', 'category']),
        ]

    def __str__(self):
        return f'{self.title} (v{self.version})'


class DocumentAccessLog(models.Model):
    """Tracks who accessed/downloaded a document."""

    ACTION_CHOICES = [
        ('view', 'Viewed'),
        ('download', 'Downloaded'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name='access_logs',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f'{self.user} {self.action} {self.document.title}'
