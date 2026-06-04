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

    # Track which fund was created/updated by this file (for cascading delete)
    fund = models.ForeignKey(
        'funds.Fund',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='import_files',
        help_text='The fund created or updated by importing this file',
    )
    fund_name = models.CharField(
        max_length=255, blank=True,
        help_text='Fund name extracted during import (for display even if fund is deleted)',
    )

    error_detail = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.original_filename} — {self.status}'


class DerivedMetric(models.Model):
    """Pass 4: a fund-level metric whose value was computed by Gemini-chosen
    formula because no direct value was present in the imported Excel.

    Stores full provenance — chosen formula, inputs used (with values and
    source), Gemini's reasoning, and the alternate formulas it considered.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='derived_metrics',
    )
    scheme = models.ForeignKey(
        'funds.Scheme',
        on_delete=models.CASCADE,
        related_name='derived_metrics',
    )

    metric_key = models.CharField(
        max_length=64,
        help_text='Canonical metric key (e.g. net_irr, moic, tvpi, dpi, nav, rvpi)',
    )
    value = models.DecimalField(
        max_digits=20, decimal_places=6,
        null=True, blank=True,
        help_text='Computed value; null if Gemini could not pick a viable formula',
    )

    formula_expression = models.TextField(
        blank=True,
        help_text='Human-readable formula chosen by Gemini',
    )
    inputs_used = models.JSONField(
        default=dict, blank=True,
        help_text='Map of input_name -> {value, source} used in the formula',
    )
    confidence = models.FloatField(
        null=True, blank=True,
        help_text='Gemini confidence 0.0-1.0',
    )
    gemini_reasoning = models.TextField(
        blank=True,
        help_text='Why this formula was chosen over alternates',
    )
    candidate_formulas = models.JSONField(
        default=list, blank=True,
        help_text='All formulas Gemini considered: [{formula, inputs_required, available, reason_rejected}]',
    )

    source_import_file = models.ForeignKey(
        ImportFile,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='derived_metrics',
    )

    derived_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('scheme', 'metric_key')]
        indexes = [
            models.Index(fields=['scheme', 'metric_key']),
            models.Index(fields=['organization', 'metric_key']),
        ]

    def __str__(self):
        return f'DerivedMetric {self.metric_key}={self.value} (scheme={self.scheme_id})'
