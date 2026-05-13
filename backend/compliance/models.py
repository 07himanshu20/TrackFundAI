"""
SEBI Compliance app models — Module 5 of FundOS India schema.

Tables: SEBIReport, AMLDueDiligence, ComplianceTestReport,
CTRChecklistItem, EquityThresholdAlert, ComplianceCalendar.

This module handles all SEBI regulatory compliance requirements:
QAR/AAR filing, AML/PMLA tracking, Compliance Test Reports,
equity threshold alerts, and compliance calendar deadlines.
"""

import uuid
from django.conf import settings
from django.db import models


class SEBIReport(models.Model):
    """
    SEBI quarterly/annual report (QAR / AAR).
    Maps to FundOS: sebi_reports table.

    QAR: Due within 15 days of quarter end.
    AAR: Due by May 31 for the preceding financial year.
    report_data stores the full IVCA format data as JSONB for regulatory flexibility.
    """
    REPORT_TYPE_CHOICES = [
        ('qar', 'Quarterly Activity Report'),
        ('aar', 'Annual Activity Report'),
    ]
    FILING_STATUS_CHOICES = [
        ('not_started', 'Not Started'),
        ('data_collection', 'Data Collection'),
        ('in_review', 'In Review'),
        ('filed', 'Filed'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected — Resubmission Required'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fund = models.ForeignKey(
        'funds.Fund',
        on_delete=models.CASCADE,
        related_name='sebi_reports',
    )
    scheme = models.ForeignKey(
        'funds.Scheme',
        on_delete=models.CASCADE,
        related_name='sebi_reports',
        null=True, blank=True,
        help_text='Scheme-level report (some reports are fund-level)',
    )

    report_type = models.CharField(
        max_length=3, choices=REPORT_TYPE_CHOICES,
        help_text='SEBI: QAR (quarterly) or AAR (annual)',
    )
    reporting_period_start = models.DateField()
    reporting_period_end = models.DateField()
    due_date = models.DateField(
        db_index=True,
        help_text='SEBI: May 31 for AAR, 15 days after quarter for QAR',
    )

    filing_status = models.CharField(
        max_length=20, choices=FILING_STATUS_CHOICES, default='not_started',
        db_index=True,
    )
    filed_date = models.DateField(null=True, blank=True)
    si_portal_reference_number = models.CharField(
        max_length=50, blank=True,
        help_text='SEBI SI Portal acknowledgement number',
    )

    # IVCA format data — flexible JSONB schema
    report_data = models.JSONField(
        default=dict, blank=True,
        help_text='SEBI: Full IVCA format data — flexible schema that evolves with SEBI updates',
    )
    ivca_format_version = models.CharField(
        max_length=20, blank=True,
        help_text='Track which IVCA format version was used',
    )

    # NAV reconciliation (required before AAR filing)
    nav_reconciled_with_depository = models.BooleanField(
        default=False,
        help_text='SEBI: Must be TRUE before AAR filing',
    )

    # Audit trail
    prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-due_date']
        indexes = [
            models.Index(fields=['fund', '-due_date']),
            models.Index(fields=['filing_status']),
        ]

    def __str__(self):
        return f'{self.fund.name} — {self.get_report_type_display()} ({self.reporting_period_end})'


class AMLDueDiligence(models.Model):
    """
    AML (Anti-Money Laundering) / PMLA due diligence record per investor.
    Maps to FundOS: aml_due_diligence table.

    Tracks SEBI Oct 2024 circular requirements: land-border country investor
    flagging, 50% corpus threshold, beneficial ownership, custodian reporting.
    """
    RISK_RATING_CHOICES = [
        ('low', 'Low'),
        ('normal', 'Normal'),
        ('high', 'High'),
        ('very_high', 'Very High'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    investor = models.ForeignKey(
        'lp.Investor',
        on_delete=models.CASCADE,
        related_name='aml_records',
    )

    # SEBI land-border country checks (Oct 2024 circular)
    is_land_border_country_investor = models.BooleanField(
        default=False, db_index=True,
        help_text='SEBI: Auto-flagged by trigger — China, Pakistan, Bangladesh, etc.',
    )
    exceeds_50pct_threshold = models.BooleanField(
        default=False,
        help_text='SEBI: If ≥50% corpus from land-border investors — triggers enhanced scrutiny',
    )

    # Beneficial ownership
    beneficial_owner_details = models.JSONField(
        default=dict, blank=True,
        help_text='UBO information — flexible structure for complex ownership chains',
    )
    beneficial_owner_identified = models.BooleanField(default=False)

    # Risk assessment
    risk_rating = models.CharField(
        max_length=10, choices=RISK_RATING_CHOICES, default='normal',
    )
    risk_assessment_date = models.DateField(null=True, blank=True)
    risk_notes = models.TextField(blank=True)

    # Custodian reporting
    custodian_reported = models.BooleanField(
        default=False,
        help_text='SEBI: Monthly custodian report filed',
    )
    custodian_report_date = models.DateField(null=True, blank=True)

    # STR (Suspicious Transaction Report)
    str_filed = models.BooleanField(default=False)
    str_reference = models.CharField(max_length=50, blank=True)

    assessed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['investor']
        verbose_name = 'AML due diligence'
        verbose_name_plural = 'AML due diligence records'

    def __str__(self):
        return f'{self.investor.investor_name} — AML ({self.risk_rating})'


class ComplianceTestReport(models.Model):
    """
    Compliance Test Report (CTR) per scheme per financial year.
    Maps to FundOS: compliance_test_reports table.

    SEBI requires annual CTR preparation and submission to trustee.
    """
    COMPLIANCE_STATUS_CHOICES = [
        ('compliant', 'Compliant'),
        ('non_compliant', 'Non-Compliant'),
        ('partially_compliant', 'Partially Compliant'),
    ]
    REPORT_STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('in_review', 'In Review'),
        ('submitted_to_trustee', 'Submitted to Trustee'),
        ('finalized', 'Finalized'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scheme = models.ForeignKey(
        'funds.Scheme',
        on_delete=models.CASCADE,
        related_name='compliance_test_reports',
    )
    financial_year = models.CharField(
        max_length=10,
        help_text='SEBI: Financial year (e.g., FY2025-26) — annual CTR required',
    )

    overall_compliance_status = models.CharField(
        max_length=20, choices=COMPLIANCE_STATUS_CHOICES, default='compliant',
    )
    report_status = models.CharField(
        max_length=25, choices=REPORT_STATUS_CHOICES, default='draft',
    )

    submitted_to_trustee_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Mandatory annual submission to trustee',
    )
    trustee_acknowledged_at = models.DateTimeField(null=True, blank=True)

    observations = models.TextField(
        blank=True,
        help_text='Compliance observations and findings',
    )
    remediation_plan = models.TextField(
        blank=True,
        help_text='Plan to address non-compliance findings',
    )

    prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['scheme', '-financial_year']
        unique_together = ('scheme', 'financial_year')

    def __str__(self):
        return f'{self.scheme} — CTR {self.financial_year}'


class CTRChecklistItem(models.Model):
    """
    Individual checklist item within a CTR.
    Maps to FundOS: ctr_checklist_items table.

    Each item represents a specific SEBI compliance check
    (e.g., investment concentration limits, co-investment restrictions, etc.)
    """
    STATUS_CHOICES = [
        ('compliant', 'Compliant'),
        ('non_compliant', 'Non-Compliant'),
        ('not_applicable', 'Not Applicable'),
        ('pending_review', 'Pending Review'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    compliance_test_report = models.ForeignKey(
        ComplianceTestReport,
        on_delete=models.CASCADE,
        related_name='checklist_items',
    )
    check_number = models.PositiveIntegerField(
        help_text='Sequential check number within the CTR',
    )
    regulation_reference = models.CharField(
        max_length=100,
        help_text='SEBI regulation reference (e.g., Reg 15(1)(a))',
    )
    description = models.TextField(
        help_text='Description of the compliance requirement',
    )
    compliance_status = models.CharField(
        max_length=15, choices=STATUS_CHOICES, default='pending_review',
    )
    evidence = models.TextField(
        blank=True,
        help_text='Evidence or reference supporting the compliance status',
    )
    remarks = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['compliance_test_report', 'check_number']

    def __str__(self):
        return f'Check #{self.check_number} — {self.regulation_reference}'


class EquityThresholdAlert(models.Model):
    """
    Auto-generated alert when an investment exceeds 10% equity threshold.
    Maps to FundOS: equity_threshold_alerts table.

    SEBI requires custodian notification within 30 days (T+30) when
    a fund's stake in any company exceeds 10% on a fully diluted basis.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    investment = models.ForeignKey(
        'investments.Investment',
        on_delete=models.CASCADE,
        related_name='threshold_alerts',
    )
    threshold_breached = models.BooleanField(
        default=True, db_index=True,
        help_text='SEBI: Auto-set by trigger on investments table',
    )
    breach_date = models.DateField(
        help_text='SEBI: T+30 = custodian notification deadline',
    )
    stake_percentage = models.DecimalField(
        max_digits=8, decimal_places=4,
        help_text='Ownership % at the time of breach',
    )

    # Custodian notification tracking
    custodian_notification_deadline = models.DateField(
        help_text='T+30 calendar days from breach_date',
    )
    custodian_notified = models.BooleanField(
        default=False,
        help_text='SEBI: Tracked for compliance evidence',
    )
    custodian_notified_date = models.DateField(null=True, blank=True)
    custodian_reference = models.CharField(
        max_length=100, blank=True,
        help_text='Reference number of custodian notification',
    )

    SEVERITY_CHOICES = [
        ('urgent', 'URGENT — Breach >25%, immediate action required'),
        ('high',   'HIGH — Breach >10%, action within 7 days'),
        ('medium', 'MEDIUM — Approaching breach, monitor closely'),
    ]
    severity = models.CharField(
        max_length=8,
        choices=SEVERITY_CHOICES,
        default='high',
        help_text='Auto-classified by stake_percentage at breach time',
    )
    is_escalated = models.BooleanField(
        default=False,
        help_text='Whether this breach has been escalated up the chain',
    )

    resolved = models.BooleanField(
        default=False,
        help_text='Whether the threshold has been resolved (e.g., stake reduced below 10%)',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-breach_date']
        indexes = [
            models.Index(fields=['threshold_breached']),
            models.Index(fields=['custodian_notified']),
        ]

    def save(self, *args, **kwargs):
        # Auto-classify severity from stake_percentage
        if self.stake_percentage is not None:
            if self.stake_percentage > 25:
                self.severity = 'urgent'
            elif self.stake_percentage > 10:
                self.severity = 'high'
            else:
                self.severity = 'medium'
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.investment.company_name} — {self.stake_percentage}% ({self.breach_date}) [{self.severity.upper()}]'


class ComplianceCalendar(models.Model):
    """
    Compliance deadline tracker.
    Maps to FundOS: compliance_calendar table.

    Tracks all regulatory deadlines: QAR, AAR, CTR, GST filing,
    custodian reports, auditor appointments, etc.
    """
    COMPLIANCE_TYPE_CHOICES = [
        ('sebi_qar', 'SEBI QAR Filing'),
        ('sebi_aar', 'SEBI AAR Filing'),
        ('ctr_preparation', 'CTR Preparation'),
        ('gst_filing', 'GST Filing'),
        ('tds_filing', 'TDS Filing'),
        ('custodian_report', 'Custodian Report'),
        ('auditor_appointment', 'Auditor Appointment'),
        ('board_meeting', 'Board Meeting'),
        ('nav_declaration', 'NAV Declaration'),
        ('depository_reconciliation', 'Depository Reconciliation'),
        ('kyc_renewal', 'KYC Renewal'),
        ('other', 'Other'),
    ]
    RECURRENCE_CHOICES = [
        ('one_time', 'One-Time'),
        ('monthly', 'Monthly'),
        ('quarterly', 'Quarterly'),
        ('semi_annual', 'Semi-Annual'),
        ('annual', 'Annual'),
    ]
    STATUS_CHOICES = [
        ('upcoming', 'Upcoming'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('overdue', 'Overdue'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='compliance_calendar',
    )
    fund = models.ForeignKey(
        'funds.Fund',
        on_delete=models.CASCADE,
        related_name='compliance_calendar',
        null=True, blank=True,
        help_text='Fund-specific deadline (null for org-level)',
    )
    scheme = models.ForeignKey(
        'funds.Scheme',
        on_delete=models.CASCADE,
        related_name='compliance_calendar',
        null=True, blank=True,
    )

    compliance_type = models.CharField(
        max_length=30, choices=COMPLIANCE_TYPE_CHOICES,
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    due_date = models.DateField(
        db_index=True,
        help_text='Indexed — only upcoming/in_progress filtered typically',
    )
    recurrence = models.CharField(
        max_length=15, choices=RECURRENCE_CHOICES, default='one_time',
    )
    advance_reminder_days = models.PositiveIntegerField(
        default=14,
        help_text='Send reminder this many days before due date',
    )

    status = models.CharField(
        max_length=15, choices=STATUS_CHOICES, default='upcoming',
    )
    completed_date = models.DateField(null=True, blank=True)

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='compliance_assignments',
    )
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['due_date']
        indexes = [
            models.Index(fields=['due_date', 'status']),
            models.Index(fields=['organization', 'status']),
        ]

    def __str__(self):
        return f'{self.title} — Due {self.due_date}'


class PPMAmendment(models.Model):
    """
    Private Placement Memorandum (PPM) amendment log.
    Maps to FundOS: ppm_amendments table.

    SEBI requires all changes to the PPM to be logged, notified to investors,
    and filed with the regulator within 21 days. Investors have an exit window.
    """
    AMENDMENT_TYPE_CHOICES = [
        ('investment_strategy', 'Investment Strategy Change'),
        ('fee_structure', 'Fee Structure Change'),
        ('key_personnel', 'Key Personnel Change'),
        ('scheme_tenure', 'Scheme Tenure Change'),
        ('corpus_limit', 'Target Corpus Change'),
        ('investment_restrictions', 'Investment Restrictions Change'),
        ('distribution_policy', 'Distribution Policy Change'),
        ('other', 'Other Material Change'),
    ]
    APPROVAL_STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('under_review', 'Under Review'),
        ('trustee_approved', 'Trustee Approved'),
        ('sebi_filed', 'Filed with SEBI'),
        ('investor_notified', 'Investors Notified'),
        ('effective', 'Effective'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fund = models.ForeignKey(
        'funds.Fund',
        on_delete=models.CASCADE,
        related_name='ppm_amendments',
    )
    scheme = models.ForeignKey(
        'funds.Scheme',
        on_delete=models.CASCADE,
        related_name='ppm_amendments',
        null=True, blank=True,
        help_text='Scheme-level PPM amendment (null = fund-level)',
    )

    amendment_number = models.PositiveIntegerField(
        help_text='Sequential amendment number (Amendment 1, 2, 3...)',
    )
    amendment_type = models.CharField(max_length=30, choices=AMENDMENT_TYPE_CHOICES)
    title = models.CharField(max_length=255, help_text='Short title of the amendment')
    description = models.TextField(help_text='Detailed description of what changed and why')

    # Key dates
    board_approval_date = models.DateField(
        null=True, blank=True,
        help_text='Date board/investment committee approved the amendment',
    )
    trustee_approval_date = models.DateField(
        null=True, blank=True,
        help_text='Date trustee approved the amendment',
    )
    sebi_filing_date = models.DateField(
        null=True, blank=True,
        help_text='SEBI: Must be filed within 21 days of approval',
    )
    investor_notification_date = models.DateField(
        null=True, blank=True,
        help_text='Date investors were notified — they have an exit window',
    )
    effective_date = models.DateField(
        null=True, blank=True,
        help_text='Date the amendment becomes effective',
    )

    # Investor exit window tracking
    investor_exit_window_days = models.PositiveIntegerField(
        default=30,
        help_text='Days investors have to exit after notification (typically 30 days)',
    )
    investor_exit_window_expiry = models.DateField(
        null=True, blank=True,
        help_text='Last date for investors to exercise exit right',
    )

    approval_status = models.CharField(
        max_length=20, choices=APPROVAL_STATUS_CHOICES, default='draft',
    )
    sebi_acknowledgement_number = models.CharField(
        max_length=50, blank=True,
        help_text='SEBI acknowledgement number for the filing',
    )
    document_url = models.URLField(
        max_length=500, blank=True,
        help_text='Link to the amended PPM document',
    )
    notes = models.TextField(blank=True)

    prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['fund', '-amendment_number']
        unique_together = ('fund', 'amendment_number')

    def __str__(self):
        return f'{self.fund.name} — PPM Amendment #{self.amendment_number}: {self.title}'


class SEBICircular(models.Model):
    """
    SEBI circular tracker — AI-parsed regulatory circulars with fund-specific action items.
    Maps to FundOS: sebi_circulars table.

    SEBI issues circulars that may require fund managers to take specific actions.
    This model tracks the circular, its requirements, and compliance actions.
    """
    APPLICABILITY_CHOICES = [
        ('all_aif', 'All AIFs'),
        ('cat_i', 'Category I AIF Only'),
        ('cat_ii', 'Category II AIF Only'),
        ('cat_iii', 'Category III AIF Only'),
        ('cat_i_ii', 'Category I & II AIF'),
        ('gift_city', 'GIFT City / IFSC AIF'),
        ('specific', 'Specific Funds (see notes)'),
    ]
    IMPACT_LEVEL_CHOICES = [
        ('low', 'Low — Informational Only'),
        ('medium', 'Medium — Process Change Required'),
        ('high', 'High — Immediate Action Required'),
        ('critical', 'Critical — Regulatory Deadline'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='sebi_circulars',
    )

    circular_number = models.CharField(
        max_length=100,
        help_text='SEBI circular number e.g. SEBI/HO/AFD/SEC-1/P/CIR/2024/104',
    )
    circular_date = models.DateField(help_text='Date of the circular')
    title = models.CharField(max_length=500, help_text='Title / subject of the circular')
    summary = models.TextField(
        blank=True,
        help_text='AI-generated or manually written summary of key requirements',
    )

    # Classification
    applicability = models.CharField(
        max_length=20, choices=APPLICABILITY_CHOICES, default='all_aif',
    )
    impact_level = models.CharField(
        max_length=10, choices=IMPACT_LEVEL_CHOICES, default='medium',
    )

    # Key dates
    compliance_deadline = models.DateField(
        null=True, blank=True,
        help_text='Deadline by which all action items must be completed',
    )

    # Source
    sebi_url = models.URLField(
        max_length=500, blank=True,
        help_text='URL to the circular on SEBI website',
    )
    full_text = models.TextField(
        blank=True,
        help_text='Full text of the circular (for AI parsing)',
    )

    # AI parsing metadata
    ai_parsed = models.BooleanField(
        default=False,
        help_text='Whether the circular has been parsed by AI to extract action items',
    )
    ai_parsed_at = models.DateTimeField(null=True, blank=True)

    is_superseded = models.BooleanField(
        default=False,
        help_text='Whether this circular has been superseded by a newer one',
    )
    superseded_by = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='supersedes',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-circular_date']
        indexes = [
            models.Index(fields=['-circular_date']),
            models.Index(fields=['impact_level']),
        ]

    def __str__(self):
        return f'{self.circular_number} — {self.title[:60]}'


class CircularAction(models.Model):
    """
    Fund-specific action item derived from a SEBI circular.
    Maps to FundOS: circular_actions table.

    Each circular may require different actions for different funds.
    This model tracks each required action and its completion status.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('not_applicable', 'Not Applicable'),
        ('deferred', 'Deferred (with justification)'),
    ]
    PRIORITY_CHOICES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('critical', 'Critical'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    circular = models.ForeignKey(
        SEBICircular,
        on_delete=models.CASCADE,
        related_name='actions',
    )
    fund = models.ForeignKey(
        'funds.Fund',
        on_delete=models.CASCADE,
        related_name='circular_actions',
        null=True, blank=True,
        help_text='Fund-specific action (null = applies to all org funds)',
    )

    action_title = models.CharField(max_length=255)
    action_description = models.TextField(
        help_text='Detailed description of the required action',
    )
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='medium')
    due_date = models.DateField(
        null=True, blank=True,
        help_text='Deadline for this specific action (may differ from circular deadline)',
    )

    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='pending')
    completion_date = models.DateField(null=True, blank=True)
    completion_notes = models.TextField(
        blank=True,
        help_text='Evidence or notes on how the action was completed',
    )
    deferred_reason = models.TextField(
        blank=True,
        help_text='Reason for deferral if status is deferred',
    )

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='circular_actions',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['circular', 'priority', 'due_date']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['due_date']),
        ]

    def __str__(self):
        return f'{self.circular.circular_number} — {self.action_title}'


# ═══════════════════════════════════════════════════════════════
# Compliance 2.0 — Portfolio Company-Level Obligations (v5)
# ═══════════════════════════════════════════════════════════════

class PortfolioCompanyCompliance(models.Model):
    """
    Tracks a single compliance obligation for a specific portfolio company.

    Covers: ROC/MCA, GST, Labour laws (PF/ESI/Factories Act), EPF deposits,
    Board meetings, Statutory audit, Income tax (TDS/advance tax),
    RERA, and sector-specific compliance.

    Each obligation has a period, deadline, status (RAG), and composite score input.
    """
    OBLIGATION_TYPE_CHOICES = [
        ('roc_annual_return',   'ROC/MCA Annual Return'),
        ('gst_gstr3b',          'GST GSTR-3B Monthly'),
        ('labour_pf_esi',       'Labour Laws — PF/ESI'),
        ('labour_factories_act','Labour Laws — Factories Act'),
        ('epf_monthly',         'EPF Monthly Deposit'),
        ('board_meeting',       'Board Meeting Compliance'),
        ('statutory_audit',     'Statutory Audit'),
        ('income_tax_tds',      'Income Tax — TDS'),
        ('income_tax_advance',  'Income Tax — Advance Tax'),
        ('rera',                'RERA (Real Estate)'),
        ('sector_specific',     'Sector-Specific Obligation'),
        ('other',               'Other'),
    ]
    STATUS_CHOICES = [
        ('compliant',  'Compliant'),
        ('due',        'Due Soon'),
        ('overdue',    'Overdue'),
        ('filed',      'Filed'),
        ('not_applicable', 'N/A'),
    ]
    # RAG status for heatmap
    RAG_CHOICES = [
        ('green',  'Green — Compliant'),
        ('amber',  'Amber — Due/Minor Issues'),
        ('red',    'Red — Overdue/Non-Compliant'),
        ('grey',   'Grey — N/A'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    portfolio_company = models.ForeignKey(
        'investments.PortfolioCompany',
        on_delete=models.CASCADE,
        related_name='compliance_obligations',
    )
    obligation_type = models.CharField(max_length=25, choices=OBLIGATION_TYPE_CHOICES)
    obligation_name = models.CharField(
        max_length=200,
        help_text='e.g., "GST GSTR-3B — April 2025", "EPF Deposit — March 2025"',
    )

    # Period
    period_start = models.DateField(null=True, blank=True)
    period_end   = models.DateField(null=True, blank=True)
    deadline     = models.DateField(help_text='Filing/deposit deadline')

    # Status
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='due')
    rag_status = models.CharField(max_length=6, choices=RAG_CHOICES, default='amber')

    # Filing details
    filed_at    = models.DateField(null=True, blank=True)
    challan_no  = models.CharField(max_length=100, blank=True)
    reference_no = models.CharField(max_length=100, blank=True)
    penalty_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text='Penalty imposed for late filing (if any)',
    )

    # Tracking
    notes = models.TextField(blank=True)
    document = models.ForeignKey(
        'documents.Document',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='compliance_obligations',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['portfolio_company', 'deadline']
        indexes = [
            models.Index(fields=['portfolio_company', 'rag_status']),
            models.Index(fields=['deadline', 'status']),
        ]

    def save(self, *args, **kwargs):
        # Auto-compute RAG from status
        if self.status == 'compliant' or self.status == 'filed':
            self.rag_status = 'green'
        elif self.status == 'overdue':
            self.rag_status = 'red'
        elif self.status == 'not_applicable':
            self.rag_status = 'grey'
        else:
            self.rag_status = 'amber'
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.portfolio_company.name} — {self.obligation_name} ({self.rag_status.upper()})'


class PortfolioComplianceScore(models.Model):
    """
    Composite compliance score (0-100) for a portfolio company as of a given date.
    Computed from PortfolioCompanyCompliance obligations.

    Score = (compliant_obligations / total_obligations) × 100
    Capped by penalty amounts and overdue duration.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    portfolio_company = models.ForeignKey(
        'investments.PortfolioCompany',
        on_delete=models.CASCADE,
        related_name='compliance_scores',
    )
    score_date = models.DateField()
    compliance_score = models.DecimalField(
        max_digits=5, decimal_places=2,
        help_text='0 = fully non-compliant, 100 = fully compliant',
    )
    total_obligations = models.IntegerField(default=0)
    compliant_count   = models.IntegerField(default=0)
    overdue_count     = models.IntegerField(default=0)
    amber_count       = models.IntegerField(default=0)

    computed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-score_date']
        unique_together = ('portfolio_company', 'score_date')

    def __str__(self):
        return f'{self.portfolio_company.name} — Compliance {self.compliance_score} @ {self.score_date}'


class EscalationLog(models.Model):
    """
    Tracks each step in the compliance escalation chain:
    Level 1 → GP Partner, Level 2 → CFO/Fund Accountant, Level 3 → Compliance Officer.
    Auto-created by ComplianceEscalationService when a breach is detected.
    """
    ESCALATION_TYPE_CHOICES = [
        ('equity_threshold_breach', 'Equity Threshold Breach'),
        ('sebi_deadline_breach',    'SEBI Deadline Breach'),
        ('ctr_overdue',             'CTR Overdue'),
        ('aml_high_risk',           'AML High Risk Investor'),
        ('fema_overdue',            'FEMA Filing Overdue'),
        ('portfolio_non_compliant', 'Portfolio Company Non-Compliant'),
        ('circular_action_overdue', 'Circular Action Overdue'),
    ]
    ROLE_CHOICES = [
        ('gp_admin',           'GP Partner'),
        ('fund_accountant',    'CFO / Fund Accountant'),
        ('compliance_officer', 'Compliance Officer'),
        ('platform_admin',     'Platform Admin'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='escalation_logs',
    )
    escalation_type = models.CharField(max_length=30, choices=ESCALATION_TYPE_CHOICES, default='equity_threshold_breach')
    level = models.PositiveSmallIntegerField(help_text='1=GP Partner, 2=CFO, 3=Compliance Officer')
    escalated_to_role = models.CharField(max_length=30, choices=ROLE_CHOICES)
    message = models.TextField()
    resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Links to trigger objects (at most one will be non-null)
    equity_alert = models.ForeignKey(
        EquityThresholdAlert,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='escalations',
    )
    sebi_report = models.ForeignKey(
        SEBIReport,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='escalations',
    )
    circular_action = models.ForeignKey(
        CircularAction,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='escalations',
    )
    escalated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='escalations_triggered',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', '-created_at'], name='esclog_org_created_idx'),
            models.Index(fields=['resolved', 'escalation_type'], name='esclog_resolved_type_idx'),
        ]

    def __str__(self):
        return f'L{self.level} {self.escalation_type} → {self.escalated_to_role} ({self.created_at:%Y-%m-%d})'


class FundComplianceScore(models.Model):
    """
    Combined fund-level compliance score (0-100).
    Weighted composite:
      SEBI Filings   30% — QAR/AAR/CTR filing status
      AML            20% — High/very-high risk investors, STRs
      Equity Alerts  20% — Unresolved threshold breaches
      Portfolio Cos  20% — Avg PortfolioComplianceScore across portfolio
      Circulars      10% — Overdue circular action items
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fund = models.ForeignKey(
        'funds.Fund',
        on_delete=models.CASCADE,
        related_name='compliance_scores',
    )
    score_date = models.DateField()
    sebi_filing_score       = models.DecimalField(max_digits=5, decimal_places=2, default=100)
    aml_score               = models.DecimalField(max_digits=5, decimal_places=2, default=100)
    equity_threshold_score  = models.DecimalField(max_digits=5, decimal_places=2, default=100)
    portfolio_company_score = models.DecimalField(max_digits=5, decimal_places=2, default=100)
    circular_action_score   = models.DecimalField(max_digits=5, decimal_places=2, default=100)
    combined_score = models.DecimalField(
        max_digits=5, decimal_places=2, default=100,
        help_text='Weighted composite 0-100',
    )
    score_detail = models.JSONField(default=dict)
    computed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-score_date']
        unique_together = ('fund', 'score_date')

    def __str__(self):
        return f'{self.fund.name} — Compliance {self.combined_score} @ {self.score_date}'


class FEMACompliance(models.Model):
    """
    FEMA / RBI compliance tracking for cross-border investments.
    FC-GPR filings, APR (Annual Performance Report) submissions.
    """
    FORM_TYPE_CHOICES = [
        ('fc_gpr',  'FC-GPR (Foreign Currency — Gross Provisional Return)'),
        ('apr',     'APR (Annual Performance Report)'),
        ('fc_trs',  'FC-TRS (Transfer of Shares)'),
        ('llp_i',   'LLP-I (FDI in LLP)'),
        ('llp_ii',  'LLP-II (Disinvestment from LLP)'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('filed',   'Filed'),
        ('accepted','Accepted'),
        ('rejected','Rejected'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    investment = models.ForeignKey(
        'investments.Investment',
        on_delete=models.CASCADE,
        related_name='fema_compliance',
    )
    form_type    = models.CharField(max_length=10, choices=FORM_TYPE_CHOICES)
    filing_date  = models.DateField(null=True, blank=True)
    due_date     = models.DateField(null=True, blank=True)
    status       = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    rbi_arn      = models.CharField(max_length=50, blank=True, help_text='RBI Acknowledgement Number')
    amount_usd   = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    notes        = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-due_date']

    def __str__(self):
        return f'{self.investment.company_name} — {self.get_form_type_display()} ({self.status})'
