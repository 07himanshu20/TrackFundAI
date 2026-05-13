"""
Investments app models — aligned with FundOS India Module 3: Portfolio Monitoring.

Tables: PortfolioCompany, Investment, InvestmentTranche, Valuation,
KPIDefinition, PortfolioKPI, ExitEvent, BoardMeeting.
"""

import uuid
from django.conf import settings
from django.db import models


class PortfolioCompany(models.Model):
    """
    First-class portfolio company record.
    Maps to FundOS: portfolio_companies table.

    Decoupled from the dashboard hierarchy (PortfolioNode) — this is the
    master record for a company that investments reference.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='portfolio_companies',
    )
    name = models.CharField(max_length=255)
    cin = models.CharField(
        max_length=21, blank=True,
        help_text='Corporate Identity Number (MCA India)',
    )
    pan = models.CharField(
        max_length=10, blank=True,
        help_text='PAN of the portfolio company',
    )
    sector = models.CharField(max_length=100, blank=True)
    sub_sector = models.CharField(max_length=100, blank=True)
    incorporation_date = models.DateField(null=True, blank=True)
    headquarters_city = models.CharField(max_length=100, blank=True)
    headquarters_country = models.CharField(max_length=100, default='India')
    website = models.URLField(max_length=500, blank=True)
    founder_names = models.JSONField(
        default=list, blank=True,
        help_text='List of founder names',
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    # Listing status — for Quoted & Unquoted analysis
    is_quoted = models.BooleanField(
        default=False,
        help_text='True if the company is publicly listed on a stock exchange',
    )
    listing_exchange = models.CharField(
        max_length=20, blank=True,
        help_text='Stock exchange: NSE, BSE, NYSE, NASDAQ, LSE, etc.',
    )

    # Link to dashboard hierarchy node (optional — for dashboard rendering)
    portfolio_node_id = models.CharField(
        max_length=500, blank=True,
        help_text='Links to PortfolioNode.node_id in the dashboard hierarchy',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        unique_together = ('organization', 'name')
        verbose_name_plural = 'portfolio companies'

    def __str__(self):
        return self.name


class Investment(models.Model):
    """
    An investment from a scheme into a portfolio company.
    Maps to FundOS: investments table.

    Added: portfolio_company FK, SEBI 10% threshold fields,
    is_lead_investor, write_off_date.
    """
    INSTRUMENT_CHOICES = [
        ('equity', 'Equity'),
        ('ccps', 'CCPS (Compulsorily Convertible Preference Shares)'),
        ('ccd', 'CCD (Compulsorily Convertible Debentures)'),
        ('ncd', 'NCD (Non-Convertible Debentures)'),
        ('odi', 'ODI (Optionally Convertible Debentures)'),
        ('safe', 'SAFE'),
        ('convertible_note', 'Convertible Note'),
        ('term_loan', 'Term Loan'),
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
    portfolio_company = models.ForeignKey(
        PortfolioCompany,
        on_delete=models.CASCADE,
        related_name='investments',
        null=True, blank=True,
        help_text='Link to master portfolio company record',
    )
    company_name = models.CharField(
        max_length=255,
        help_text='Denormalized company name for quick access',
    )
    portfolio_node_id = models.CharField(
        max_length=500, blank=True,
        help_text='Links to PortfolioNode.node_id in the dashboard hierarchy',
    )
    instrument_type = models.CharField(max_length=20, choices=INSTRUMENT_CHOICES, default='equity')

    # Ownership
    ownership_pct = models.DecimalField(
        max_digits=7, decimal_places=4, null=True, blank=True,
        help_text='Ownership percentage (e.g., 15.5000)',
    )
    percentage_stake_fully_diluted = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text='Ownership % on fully diluted basis',
    )

    # SEBI 10% equity threshold (auto-trigger in FundOS)
    exceeds_10pct_threshold = models.BooleanField(
        default=False, db_index=True,
        help_text='SEBI: Auto-set when ownership >= 10% — requires custodian notification',
    )
    threshold_breach_date = models.DateField(
        null=True, blank=True,
        help_text='SEBI: Date when 10% threshold was breached — T+30 = custodian notification deadline',
    )

    total_invested = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Total amount invested (sum of tranches) in scheme currency',
    )
    investment_date = models.DateField(null=True, blank=True)
    currency = models.CharField(max_length=3, default='INR')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    sector = models.CharField(max_length=100, blank=True)
    stage = models.CharField(
        max_length=100, blank=True,
        help_text='Funding stage / round name (e.g. Seed, Series A, Series B, Bridge)',
    )
    irr_pct = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text='Gross IRR % for this investment (e.g. 45.92 means 45.92%)',
    )
    description = models.TextField(blank=True)

    # Governance
    board_seat = models.BooleanField(
        default=False,
        help_text='Whether the fund has a board seat',
    )
    is_lead_investor = models.BooleanField(
        default=False,
        help_text='Whether this fund is the lead investor in this round',
    )

    # Write-off tracking
    write_off_date = models.DateField(
        null=True, blank=True,
        help_text='Date investment was written off (if applicable)',
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

    def save(self, *args, **kwargs):
        # Auto-set 10% threshold flag
        pct = self.percentage_stake_fully_diluted or self.ownership_pct
        if pct and pct >= 10 and not self.exceeds_10pct_threshold:
            self.exceeds_10pct_threshold = True
            if not self.threshold_breach_date:
                from django.utils import timezone
                self.threshold_breach_date = timezone.now().date()
        super().save(*args, **kwargs)


class InvestmentTranche(models.Model):
    """
    A single tranche (drawdown) within an investment.
    Maps to FundOS: investment_tranches table.
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
    IPEV / Ind AS 109 fair value assessment for an investment.
    Maps to FundOS: valuations table.

    Added: fvtpl_movement (Ind AS 109), valuer_reg_number (IBBI),
    fair_value_of_holding, enterprise_value, IPEV Level 1/2/3 fields,
    DLOM, DRHP value, Pre-IPO track, corporate action support.
    """
    METHOD_CHOICES = [
        ('dcf', 'Discounted Cash Flow'),
        ('comparables', 'Market Comparables'),
        ('recent_transaction', 'Recent Transaction'),
        ('net_assets', 'Net Assets'),
        ('cost', 'Cost (at cost)'),
        ('option_pricing', 'Option Pricing Model'),
    ]
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    IPEV_LEVEL_CHOICES = [
        (1, 'Level 1 — Quoted (Exchange Listed, CMP × shares)'),
        (2, 'Level 2 — Observable Inputs (Peer Multiples, IBBI certified)'),
        (3, 'Level 3 — Unobservable Inputs (DCF / Last Round, Board + IBBI certified)'),
    ]
    CORPORATE_ACTION_CHOICES = [
        ('none',         'None'),
        ('stock_split',  'Stock Split'),
        ('bonus',        'Bonus Issue'),
        ('rights',       'Rights Issue'),
        ('dividend',     'Dividend'),
        ('buyback',      'Buyback'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    investment = models.ForeignKey(
        Investment,
        on_delete=models.CASCADE,
        related_name='valuations',
    )
    valuation_date = models.DateField(
        help_text='SEBI: Quarterly valuation required',
    )
    methodology = models.CharField(max_length=20, choices=METHOD_CHOICES)

    # Value fields
    fair_value = models.DecimalField(
        max_digits=18, decimal_places=2,
        help_text='Fair value of the investment in scheme currency',
    )
    fair_value_of_holding = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='FMV of fund\'s stake — drives NAV calculation',
    )
    enterprise_value = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Enterprise value of the portfolio company',
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

    # Ind AS 109 compliance
    fvtpl_movement = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='SEBI: Ind AS 109 FVTPL (Fair Value Through Profit & Loss) movement',
    )

    # DCF-specific
    discount_rate = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='Discount rate used for DCF (%)',
    )
    comparable_companies = models.JSONField(
        default=list, blank=True,
        help_text='List of comparable company names/multiples',
    )
    assumptions = models.TextField(blank=True, help_text='Valuation assumptions and notes')

    # IPEV Classification (v5 — International Private Equity Valuation)
    ipev_level = models.PositiveSmallIntegerField(
        null=True, blank=True,
        choices=IPEV_LEVEL_CHOICES,
        help_text='IPEV Level 1/2/3 classification — determines valuation method and certification',
    )

    # Pre-IPO track
    is_pre_ipo = models.BooleanField(
        default=False,
        help_text='True if company has filed DRHP — enables Pre-IPO valuation track',
    )
    drhp_value = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Valuation implied by DRHP filing (basis for Pre-IPO track)',
    )

    # DLOM — Discount for Lack of Marketability (applied to Pre-IPO and Level 3)
    dlom_pct = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='DLOM % applied to fair value (e.g., 20.00 = 20% discount)',
    )

    # Level 2/3 specific fields
    peer_multiples_used = models.JSONField(
        default=list, blank=True,
        help_text='List of peer EV/Revenue or EV/EBITDA multiples used in Level 2 valuation',
    )
    dcf_terminal_growth_rate = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='Terminal growth rate used in DCF (Level 3) — percentage',
    )
    last_round_premium_discount_pct = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='Premium or discount to last round price (Level 3 last-round method)',
    )

    # Corporate action handling (Level 1 listed securities)
    corporate_action_type = models.CharField(
        max_length=15,
        choices=CORPORATE_ACTION_CHOICES,
        default='none',
        help_text='Corporate action since last valuation — adjusts share count/price basis',
    )
    corporate_action_ratio = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text='Ratio for split/bonus (e.g., 2.0 for 2:1 split, 0.5 for 1:2 consolidation)',
    )

    # IBBI Registered Valuer
    valuer_name = models.CharField(
        max_length=255, blank=True,
        help_text='Name of the IBBI Registered Valuer',
    )
    valuer_reg_number = models.CharField(
        max_length=50, blank=True,
        help_text='IBBI Registered Valuer registration number',
    )

    # Workflow
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
    Maps to FundOS: kpi_definitions table.
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

    SECTOR_TEMPLATE_CHOICES = [
        ('generic',       'Generic'),
        ('saas',          'SaaS'),
        ('healthcare',    'Healthcare'),
        ('manufacturing', 'Manufacturing'),
        ('nbfc',          'NBFC / Fintech'),
        ('consumer',      'Consumer'),
        ('realestate',    'Real Estate'),
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

    # Sector-specific KPI template (v5)
    sector_template = models.CharField(
        max_length=15, choices=SECTOR_TEMPLATE_CHOICES, default='generic',
        help_text='Which sector template this KPI belongs to',
    )
    is_system_kpi = models.BooleanField(
        default=False,
        help_text='True for seeded system KPIs (not org-created) — prevents deletion',
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sort_order', 'name']
        unique_together = ('organization', 'slug')

    def __str__(self):
        return self.name


class PortfolioKPI(models.Model):
    """
    A KPI value submitted by a founder for a specific investment & period.
    Maps to FundOS: portfolio_kpis table.

    Added: source field, period_end_date, portfolio_company FK.
    """
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    SOURCE_CHOICES = [
        ('manual', 'Manual Entry'),
        ('tally_import', 'Tally Import'),
        ('api_integration', 'API Integration'),
        ('excel_upload', 'Excel Upload'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    investment = models.ForeignKey(
        Investment,
        on_delete=models.CASCADE,
        related_name='kpis',
    )
    portfolio_company = models.ForeignKey(
        PortfolioCompany,
        on_delete=models.CASCADE,
        related_name='kpis',
        null=True, blank=True,
        help_text='Direct link to portfolio company (denormalized for queries)',
    )
    kpi_definition = models.ForeignKey(
        KPIDefinition,
        on_delete=models.CASCADE,
        related_name='values',
    )
    period = models.DateField(help_text='First day of the reporting period (e.g., 2025-04-01)')
    period_end_date = models.DateField(
        null=True, blank=True,
        help_text='Last day of the reporting period',
    )
    value = models.DecimalField(max_digits=22, decimal_places=4,
                                help_text='High precision for ratios and large INR values')
    notes = models.TextField(blank=True)
    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default='manual',
        help_text='How this KPI value was captured',
    )
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


class CompanyFinancials(models.Model):
    """
    Monthly burn rate, cash balance, and runway for a portfolio company.
    Stores structured financial metrics extracted from fund Excel files.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    investment = models.ForeignKey(
        Investment,
        on_delete=models.CASCADE,
        related_name='financials',
    )
    portfolio_company = models.ForeignKey(
        PortfolioCompany,
        on_delete=models.CASCADE,
        related_name='financials',
        null=True, blank=True,
    )
    period = models.DateField(
        help_text='First day of the reporting month (e.g., 2025-04-01)',
    )
    gross_burn = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Total monthly cash outflow in Cr (expenses + capex)',
    )
    net_burn = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Net monthly burn in Cr = gross burn minus revenue',
    )
    cash_balance = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Cash and equivalents at period end in Cr',
    )
    runway_months = models.DecimalField(
        max_digits=6, decimal_places=1, null=True, blank=True,
        help_text='Computed runway = cash_balance / net_burn. Null if burn is zero or not known.',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['investment', '-period']
        unique_together = ('investment', 'period')

    def __str__(self):
        return f'{self.investment.company_name} — {self.period}'


class ExitEvent(models.Model):
    """
    Models exit scenarios and actual exits.
    Maps to FundOS: exit_events table.

    Added: gain_loss_nature (SEBI: LTCG/STCG classification),
    net_exit_proceeds, exit_multiple, irr_on_exit.
    """
    EXIT_TYPE_CHOICES = [
        ('ipo', 'IPO'),
        ('merger_acquisition', 'Merger & Acquisition'),
        ('secondary_sale', 'Secondary Sale'),
        ('buyback', 'Buyback'),
        ('write_off', 'Write-Off'),
    ]
    GAIN_LOSS_NATURE_CHOICES = [
        ('ltcg', 'Long Term Capital Gain'),
        ('stcg', 'Short Term Capital Gain'),
        ('short_term_loss', 'Short Term Loss'),
        ('long_term_loss', 'Long Term Loss'),
        ('na', 'Not Applicable'),
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
        help_text='Gross proceeds to the fund from this exit',
    )
    net_exit_proceeds = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Net proceeds after transaction costs',
    )
    realized_gain_loss = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
    )

    # SEBI capital gains classification
    gain_loss_nature = models.CharField(
        max_length=20, choices=GAIN_LOSS_NATURE_CHOICES,
        default='na',
        help_text='SEBI: Capital gains classification — LTCG/STCG determines TDS rate',
    )

    # Multiples and returns
    moic = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text='Multiple on invested capital',
    )
    exit_multiple = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text='MoIC on this specific exit event',
    )
    irr_pct = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text='Gross IRR %',
    )
    irr_on_exit = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text='IRR realised at exit',
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
    Maps to FundOS: board_meetings table.
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
