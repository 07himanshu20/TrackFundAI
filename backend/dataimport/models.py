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

    # Pass 3.5 variant tag — for metrics that come in gross/net (or other)
    # semantic variants. Null when the metric has no variant distinction.
    # The canonical_schema entry's `requires_variant` field declares
    # which variants are valid for each metric_key; this column stores
    # whichever variant the extracted cell represents.
    variant = models.CharField(
        max_length=32, null=True, blank=True,
        help_text='Semantic variant tag (e.g. "gross", "net", "pre_fee", "post_fee") '
                  'for metrics that have multiple variants. Null when not applicable.',
    )

    derived_at = models.DateTimeField(auto_now=True)

    class Meta:
        # The unique_together is widened to include `variant` so the same
        # metric can store both gross and net values side-by-side without
        # collision.
        unique_together = [('scheme', 'metric_key', 'variant')]
        indexes = [
            models.Index(fields=['scheme', 'metric_key']),
            models.Index(fields=['organization', 'metric_key']),
            models.Index(fields=['scheme', 'metric_key', 'variant']),
        ]

    def __str__(self):
        return f'DerivedMetric {self.metric_key}={self.value} (scheme={self.scheme_id})'


class MetricCandidate(models.Model):
    """One row per (Pass × scheme × metric_key × variant). Preserves every
    fund-level metric value any Pass produced during an import, so the
    Metric Arbiter can pick the winner deterministically.

    Architectural role
    ──────────────────
    Before this table existed, each fund-metric Pass (3.5, 4, 8, 9)
    wrote directly to DerivedMetric. Because DerivedMetric has
    unique_together=(scheme, metric_key, variant), the last Pass to
    write WON unconditionally — there was no global arbitration. That
    let a low-confidence Pass-4 catalogue formula silently overwrite a
    high-confidence Pass-9 direct read.

    Now, every Pass ALSO records its result here. After all Passes
    finish, the Metric Arbiter:
      1. Reads every MetricCandidate row for the scheme.
      2. Classifies each by trust tier (using pass_id).
      3. Applies universal accounting identity guards
         (net = gross − clawback, etc.).
      4. Writes the winner to DerivedMetric.
      5. Records the rejected candidates as alternates so the
         provenance panel can show "Pass 4 said X but Pass 9 said Y
         and we chose Y because <reason>".

    Why a separate table (not a JSONField on DerivedMetric)
    ───────────────────────────────────────────────────────
    - One indexed row per (scheme, metric_key, variant, pass_id) is
      queryable; nested JSON would not be.
    - The Arbiter's policy can evolve over time (new tiers, new
      identity rules) WITHOUT migrating historical data — each import
      writes its own candidates, the Arbiter just decides each time.
    - Future "ultra-review" or audit features can read every candidate
      across every fund/import without joins on JSON.
    """

    PASS_CHOICES = [
        ('P35', 'Pass 3.5 — direct value imported'),
        ('P4',  'Pass 4 — catalogue formula derivation'),
        ('P8',  'Pass 8 — direct waterfall sheet read'),
        ('P9',  'Pass 9 — unified fund metrics compute'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='metric_candidates',
    )
    scheme = models.ForeignKey(
        'funds.Scheme',
        on_delete=models.CASCADE,
        related_name='metric_candidates',
    )

    metric_key = models.CharField(max_length=64)
    variant = models.CharField(
        max_length=32, null=True, blank=True,
        help_text='gross/net/pre_fee/post_fee/... — null when the metric '
                  'has no variant distinction.',
    )
    pass_id = models.CharField(max_length=8, choices=PASS_CHOICES)

    value = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    formula_expression = models.TextField(blank=True)
    confidence = models.FloatField(default=0.0)
    inputs_used = models.JSONField(default=dict, blank=True)
    source_cells = models.JSONField(default=list, blank=True)
    gemini_reasoning = models.TextField(blank=True)

    source_import_file = models.ForeignKey(
        ImportFile, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='metric_candidates',
    )

    # Arbiter decision flags. Filled in by the Arbiter run after all
    # Passes complete. The frontend continues to read DerivedMetric;
    # these flags exist purely so audit can answer "why did the Arbiter
    # pick this candidate over the others?".
    arbiter_decision = models.CharField(
        max_length=24, blank=True,
        help_text='winner / rejected / superseded / identity_clamped / unused',
    )
    arbiter_reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # NO unique constraint — multiple candidates per (scheme,
        # metric, variant) from different passes is the whole point.
        # Within ONE import, a re-run of the same Pass overwrites
        # its own candidate via the (scheme, metric, variant, pass_id)
        # index lookup the recorder uses.
        indexes = [
            models.Index(fields=['scheme', 'metric_key']),
            models.Index(fields=['scheme', 'metric_key', 'variant']),
            models.Index(fields=['scheme', 'metric_key', 'variant', 'pass_id']),
        ]
        ordering = ['scheme', 'metric_key', 'variant', 'pass_id']

    def __str__(self):
        return (f'MetricCandidate {self.metric_key}/{self.variant or "-"} '
                f'@ {self.pass_id} = {self.value}')


class FundMetric(models.Model):
    """Single source of truth for every fund-level metric, written by
    the anchor pipeline (workbook_census → identity → anchor_extract →
    cash_flows → compute → audit → persist).

    Replaces the multi-pass DerivedMetric + MetricCandidate + Arbiter
    architecture. One row per (scheme, metric_key). Re-running the
    pipeline for the same scheme atomically deletes and rewrites all
    rows — values are fully reproducible from the source file.
    """

    SOURCE_CHOICES = [
        ('extracted', 'Extracted directly from a cell in the document'),
        ('computed',  'Computed in Python from anchor values'),
        ('conflict',  'Stated value disagrees with computed value'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='fund_metrics',
    )
    scheme = models.ForeignKey(
        'funds.Scheme',
        on_delete=models.CASCADE,
        related_name='fund_metrics',
    )

    metric_key = models.CharField(
        max_length=64,
        help_text='Canonical metric key (moic, tvpi, dpi, rvpi, net_irr, '
                  'nav, carry_base, carry_amount_gross, carry_amount_net, '
                  'gp_clawback_provision, gp_catchup_amount, '
                  'preferred_return_amount, return_of_capital_amount, '
                  'total_committed_capital, total_called_capital, ...)',
    )
    value = models.DecimalField(
        max_digits=24, decimal_places=8,
        null=True, blank=True,
        help_text='The metric value. Null when inputs were insufficient.',
    )

    formula_expression = models.TextField(
        blank=True,
        help_text='The canonical formula used (textbook definition)',
    )
    inputs_used = models.JSONField(
        default=dict, blank=True,
        help_text='Map of anchor_name -> value used in the formula',
    )
    provenance = models.JSONField(
        default=dict, blank=True,
        help_text='For each input anchor: {value, sheet, cell, reasoning}',
    )

    source = models.CharField(
        max_length=16, choices=SOURCE_CHOICES, default='computed',
    )

    source_import_file = models.ForeignKey(
        ImportFile,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='fund_metrics',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('scheme', 'metric_key')]
        indexes = [
            models.Index(fields=['scheme', 'metric_key']),
            models.Index(fields=['organization', 'metric_key']),
        ]

    def __str__(self):
        v = self.value if self.value is not None else 'null'
        return f'FundMetric {self.metric_key}={v} (scheme={self.scheme_id})'
