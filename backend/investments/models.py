"""
Investments app models — Phase 2 of TrackFundAI.

Tracks investments from schemes into portfolio companies, tranches,
IPEV-standard valuations, KPI definitions & submissions, exit events,
and board meetings.
"""

import uuid
from django.conf import settings
from django.db import models


class Investment(models.Model):
    """
    An investment from a scheme into a portfolio company.
    Tracks instrument type, ownership %, and links to Fund Admin scheme.
    """
    INSTRUMENT_CHOICES = [
        ('equity', 'Equity'),
        ('ccps', 'CCPS (Compulsorily Convertible Preference Shares)'),
        ('ccd', 'CCD (Compulsorily Convertible Debentures)'),
        ('ncd', 'NCD (Non-Convertible Debentures)'),
        ('safe', 'SAFE'),
        ('convertible_note', 'Convertible Note'),
    ]
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('partially_exited', 'Partially Exited'),
        ('fully_exited', 'Fully Exited'),
        ('written_off', 'Written Off'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scheme = models.ForeignKey(
        'funds.Scheme',
        on_delete=models.CASCADE,
        related_name='investments',
    )
    company_name = models.CharField(max_length=255)
    portfolio_node_id = models.CharField(
        max_length=500, blank=True,
        help_text='Links to PortfolioNode.node_id in the dashboard hierarchy',
    )
    instrument_type = models.CharField(max_length=20, choices=INSTRUMENT_CHOICES, default='equity')
    ownership_pct = models.DecimalField(
        max_digits=7, decimal_places=4, null=True, blank=True,
        help_text='Ownership percentage (e.g., 15.5000)',
    )
    total_invested = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Total amount invested (sum of tranches) in scheme currency',
    )
    investment_date = models.DateField(null=True, blank=True)
    currency = models.CharField(max_length=3, default='INR')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    sector = models.CharField(max_length=100, blank=True)
    description = models.TextField(blank=True)
    board_seat = models.BooleanField(
        default=False,
        help_text='Whether the fund has a board seat',
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['company_name']
        unique_together = ('scheme', 'company_name', 'instrument_type')

    def __str__(self):
        return f'{self.company_name} ({self.get_instrument_type_display()}) — {self.scheme}'


class InvestmentTranche(models.Model):
    """
    A single tranche (drawdown) within an investment.
    Multiple tranches can exist per investment over time.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    investment = models.ForeignKey(
        Investment,
        on_delete=models.CASCADE,
        related_name='tranches',
    )
    tranche_number = models.PositiveIntegerField(default=1)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    date = models.DateField()
    shares_acquired = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True,
    )
    price_per_share = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True,
    )
    pre_money_valuation = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
    )
    post_money_valuation = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
    )
    round_name = models.CharField(
        max_length=100, blank=True,
        help_text='e.g., Series A, Series B, Bridge',
    )
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['investment', 'tranche_number']
        unique_together = ('investment', 'tranche_number')

    def __str__(self):
        return f'{self.investment.company_name} — Tranche {self.tranche_number}'


class Valuation(models.Model):
    """
    IPEV-standard fair value assessment for an investment.
    Supports multiple methodologies and approval workflow.
    """
    METHOD_CHOICES = [
        ('dcf', 'Discounted Cash Flow'),
        ('comparables', 'Market Comparables'),
        ('recent_transaction', 'Recent Transaction'),
        ('net_assets', 'Net Assets'),
        ('cost', 'Cost (at cost)'),
    ]
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    investment = models.ForeignKey(
        Investment,
        on_delete=models.CASCADE,
        related_name='valuations',
    )
    valuation_date = models.DateField()
    methodology = models.CharField(max_length=20, choices=METHOD_CHOICES)
    fair_value = models.DecimalField(
        max_digits=18, decimal_places=2,
        help_text='Fair value of the investment in scheme currency',
    )
    cost_basis = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Original cost basis for gain/loss calculation',
    )
    unrealized_gain_loss = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
    )
    multiple = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text='MOIC — fair_value / cost_basis',
    )
    discount_rate = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='Discount rate used for DCF (%)',
    )
    comparable_companies = models.JSONField(
        default=list, blank=True,
        help_text='List of comparable company names/multiples',
    )
    assumptions = models.TextField(blank=True, help_text='Valuation assumptions and notes')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['investment', '-valuation_date']
        unique_together = ('investment', 'valuation_date', 'methodology')

    def __str__(self):
        return f'{self.investment.company_name} — {self.valuation_date} ({self.get_methodology_display()})'


class KPIDefinition(models.Model):
    """
    Defines a KPI that portfolio companies report on.
    These are org-level definitions; actual values are in PortfolioKPI.
    """
    FORMAT_CHOICES = [
        ('number', 'Number'),
        ('currency', 'Currency'),
        ('percent', 'Percentage'),
        ('ratio', 'Ratio'),
        ('boolean', 'Yes/No'),
    ]
    FREQUENCY_CHOICES = [
        ('monthly', 'Monthly'),
        ('quarterly', 'Quarterly'),
        ('annual', 'Annual'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='kpi_definitions',
    )
    name = models.CharField(max_length=100, help_text='e.g., MRR, Burn Rate, Headcount')
    slug = models.SlugField(max_length=100)
    description = models.TextField(blank=True)
    format = models.CharField(max_length=10, choices=FORMAT_CHOICES, default='number')
    frequency = models.CharField(max_length=10, choices=FREQUENCY_CHOICES, default='monthly')
    is_required = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sort_order', 'name']
        unique_together = ('organization', 'slug')

    def __str__(self):
        return self.name


class PortfolioKPI(models.Model):
    """
    A KPI value submitted by a founder for a specific investment & period.
    GP reviews/approves these.
    """
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    investment = models.ForeignKey(
        Investment,
        on_delete=models.CASCADE,
        related_name='kpis',
    )
    kpi_definition = models.ForeignKey(
        KPIDefinition,
        on_delete=models.CASCADE,
        related_name='values',
    )
    period = models.DateField(help_text='First day of the reporting period (e.g., 2025-04-01)')
    value = models.DecimalField(max_digits=18, decimal_places=4)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')

    submitted_by = models.ForeignKey(
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
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['investment', '-period']
        unique_together = ('investment', 'kpi_definition', 'period')

    def __str__(self):
        return f'{self.investment.company_name} — {self.kpi_definition.name} — {self.period}'


class ExitEvent(models.Model):
    """
    Models exit scenarios (IPO, M&A, secondary sale, write-off) per investment.
    """
    EXIT_TYPE_CHOICES = [
        ('ipo', 'IPO'),
        ('merger_acquisition', 'Merger & Acquisition'),
        ('secondary_sale', 'Secondary Sale'),
        ('buyback', 'Buyback'),
        ('write_off', 'Write-Off'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    investment = models.ForeignKey(
        Investment,
        on_delete=models.CASCADE,
        related_name='exit_scenarios',
    )
    exit_type = models.CharField(max_length=20, choices=EXIT_TYPE_CHOICES)
    is_actual = models.BooleanField(
        default=False,
        help_text='True if this exit has actually occurred; False if it is a scenario/model',
    )
    exit_date = models.DateField(null=True, blank=True)
    exit_valuation = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Company valuation at exit',
    )
    proceeds = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Proceeds to the fund from this exit',
    )
    realized_gain_loss = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
    )
    moic = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text='Multiple on invested capital',
    )
    irr_pct = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text='Gross IRR %',
    )
    buyer_name = models.CharField(max_length=255, blank=True, help_text='Acquirer/buyer (for M&A/secondary)')
    assumptions = models.TextField(blank=True, help_text='Scenario assumptions')

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['investment', '-exit_date']

    def __str__(self):
        kind = 'Actual' if self.is_actual else 'Scenario'
        return f'{self.investment.company_name} — {self.get_exit_type_display()} ({kind})'


class BoardMeeting(models.Model):
    """
    Tracks board meetings for portfolio companies.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    investment = models.ForeignKey(
        Investment,
        on_delete=models.CASCADE,
        related_name='board_meetings',
    )
    meeting_date = models.DateField()
    meeting_number = models.PositiveIntegerField(null=True, blank=True)
    agenda = models.TextField(blank=True)
    minutes = models.TextField(blank=True)
    attendees = models.JSONField(default=list, blank=True, help_text='List of attendee names')
    resolutions = models.JSONField(default=list, blank=True, help_text='Key resolutions passed')
    next_meeting_date = models.DateField(null=True, blank=True)
    document = models.ForeignKey(
        'documents.Document',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='board_meetings',
        help_text='Attached board pack document',
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['investment', '-meeting_date']

    def __str__(self):
        return f'{self.investment.company_name} — Board Meeting {self.meeting_date}'
