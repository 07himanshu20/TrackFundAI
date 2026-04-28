import uuid
from django.conf import settings
from django.db import models


def import_file_path(instance, filename):
    org_slug = instance.job.organization.slug if instance.job.organization else 'default'
    job_id = str(instance.job_id)[:8]
    return f'dataimport/{org_slug}/{job_id}/{filename}'


class ImportJob(models.Model):
    """Tracks an upload session — one or more fund Excel files from a user."""

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('completed_with_errors', 'Completed With Errors'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='import_jobs',
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='import_jobs',
    )

    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default='pending')
    progress_pct = models.IntegerField(default=0)
    progress_message = models.CharField(max_length=500, blank=True)

    total_files = models.IntegerField(default=0)
    completed_files = models.IntegerField(default=0)

    error_log = models.JSONField(
        default=list, blank=True,
        help_text='List of {file, section, row, error}',
    )
    result_summary = models.JSONField(
        default=dict, blank=True,
        help_text='Record counts per model after import',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'ImportJob {str(self.id)[:8]} — {self.status} ({self.progress_pct}%)'


class ImportFile(models.Model):
    """One uploaded Excel file within an import job."""

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('mapping', 'Column Mapping'),
        ('importing', 'Importing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(
        ImportJob,
        on_delete=models.CASCADE,
        related_name='files',
    )
    file = models.FileField(upload_to=import_file_path)
    original_filename = models.CharField(max_length=500)
    file_size = models.IntegerField(default=0)

    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='pending')
    column_mapping = models.JSONField(
        default=dict, blank=True,
        help_text='Gemini column mapping output per sheet',
    )
    gemini_confidence = models.FloatField(default=0.0)
    sheet_names = models.JSONField(default=list, blank=True)

    error_detail = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.original_filename} — {self.status}'
