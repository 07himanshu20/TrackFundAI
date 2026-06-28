"""
LP Management app models — Module 2 of FundOS India schema.

Tables: BankAccount, Investor, Commitment, CapitalCall, CapitalCallLineItem,
Distribution, DistributionLineItem, LPCapitalAccount.

This is the core LP lifecycle: investor onboarding → commitment → capital calls
→ distributions → capital account tracking with IRR/TVPI/DPI/RVPI.
"""

import uuid
from django.conf import settings
from django.db import models


class BankAccount(models.Model):
    """
    Bank account details for investors and fund entities.
    Maps to FundOS: bank_accounts (implied).
    """
    ACCOUNT_TYPE_CHOICES = [
        ('savings', 'Savings'),
        ('current', 'Current'),
        ('nre', 'NRE'),
        ('nro', 'NRO'),
        ('fcnr', 'FCNR'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='bank_accounts',
    )
    account_holder_name = models.CharField(max_length=255)
    bank_name = models.CharField(max_length=255)
    branch_name = models.CharField(max_length=255, blank=True)
    account_number = models.CharField(
        max_length=50,
        help_text='Should be encrypted at rest in production (pgcrypto AES-256)',
    )
    ifsc_code = models.CharField(
        max_length=11, blank=True,
        help_text='IFSC code for Indian banks',
    )
    swift_code = models.CharField(
        max_length=11, blank=True,
        help_text='SWIFT/BIC code for international transfers',
    )
    account_type = models.CharField(
        max_length=10, choices=ACCOUNT_TYPE_CHOICES, default='current',
    )
    is_primary = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_primary', 'bank_name']

    def __str__(self):
        return f'{self.account_holder_name} — {self.bank_name} ({self.get_account_type_display()})'


class Investor(models.Model):
    """
    LP / Investor record.
    Maps to FundOS: investors table.

    Covers India-specific investor types, SEBI compliance fields
    (accredited investor, land-border country, PEP status), and KYC tracking.
    """
    INVESTOR_TYPE_CHOICES = [
        ('individual', 'Individual'),
        ('huf', 'HUF'),
        ('company', 'Company'),
        ('partnership', 'Partnership Firm'),
        ('llp', 'LLP'),
        ('trust', 'Trust'),
        ('society', 'Society'),
        ('aop', 'AOP/BOI'),
        ('fpi', 'Foreign Portfolio Investor'),
        ('fii', 'Foreign Institutional Investor'),
        ('nri', 'NRI'),
        ('bank', 'Bank'),
        ('insurance', 'Insurance Company'),
        ('pension', 'Pension Fund'),
        ('sovereign', 'Sovereign Wealth Fund'),
        ('endowment', 'Endowment'),
        ('fund_of_funds', 'Fund of Funds'),
        ('family_office', 'Family Office'),
        ('other', 'Other'),
    ]
    KYC_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('expired', 'Expired'),
        ('rejected', 'Rejected'),
    ]
    FATCA_STATUS_CHOICES = [
        ('not_applicable', 'Not Applicable'),
        ('compliant', 'Compliant'),
        ('pending', 'Pending'),
        ('non_compliant', 'Non-Compliant'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='investors',
    )

    # Basic info
    investor_name = models.CharField(max_length=255, help_text='Legal name of the investor')
    investor_type = models.CharField(max_length=20, choices=INVESTOR_TYPE_CHOICES)
    contact_person = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, default='India')

    # India regulatory identifiers
    pan = models.CharField(
        max_length=10, blank=True,
        help_text='Mandatory for India-resident investors',
    )
    aadhaar_last_4 = models.CharField(
        max_length=4, blank=True,
        help_text='Last 4 digits only — never store full Aadhaar',
    )
    ckyc_number = models.CharField(
        max_length=14, blank=True,
        help_text='CERSAI KYC number',
    )

    # KYC status
    kyc_status = models.CharField(
        max_length=15, choices=KYC_STATUS_CHOICES, default='pending',
    )
    kyc_completed_date = models.DateField(null=True, blank=True)
    kyc_expiry_date = models.DateField(null=True, blank=True)

    # SEBI compliance flags
    is_accredited_investor = models.BooleanField(
        default=False,
        help_text='SEBI: Required for LVF and accredited investor-only schemes',
    )
    accreditation_date = models.DateField(null=True, blank=True)

    is_land_border_country = models.BooleanField(
        default=False,
        help_text='SEBI: Auto-flagged — AML Oct 2024 circular (China, Pakistan, Bangladesh, etc.)',
    )
    land_border_country_name = models.CharField(
        max_length=100, blank=True,
        help_text='SEBI: Country name if land-border flagged',
    )

    is_politically_exposed = models.BooleanField(
        default=False,
        help_text='PEP (Politically Exposed Person) flag — heightened due diligence required',
    )

    # FATCA compliance (for US persons / reporting)
    fatca_status = models.CharField(
        max_length=15, choices=FATCA_STATUS_CHOICES, default='not_applicable',
    )

    # Bank account linkage
    primary_bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='primary_for_investors',
    )

    # Portal user linkage
    portal_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='investor_profile',
        help_text='LP user who can log into the portal to view their data',
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['investor_name']
        indexes = [
            models.Index(fields=['organization', 'investor_type']),
            models.Index(fields=['organization', 'kyc_status']),
        ]

    def __str__(self):
        return f'{self.investor_name} ({self.get_investor_type_display()})'


class Commitment(models.Model):
    """
    LP commitment to a scheme.
    Maps to FundOS: commitments table.
    """
    CLOSE_TYPE_CHOICES = [
        ('first_close', 'First Close'),
        ('subsequent_close', 'Subsequent Close'),
        ('final_close', 'Final Close'),
    ]
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('defaulted', 'Defaulted'),
        ('transferred', 'Transferred'),
        ('cancelled', 'Cancelled'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    investor = models.ForeignKey(
        Investor,
        on_delete=models.CASCADE,
        related_name='commitments',
    )
    scheme = models.ForeignKey(
        'funds.Scheme',
        on_delete=models.CASCADE,
        related_name='commitments',
    )

    commitment_amount = models.DecimalField(
        max_digits=18, decimal_places=2,
        help_text='Total LP commitment (INR or scheme currency)',
    )
    commitment_date = models.DateField(null=True, blank=True)
    close_type = models.CharField(max_length=20, choices=CLOSE_TYPE_CHOICES, default='first_close')

    units_allocated = models.DecimalField(
        max_digits=18, decimal_places=6, null=True, blank=True,
        help_text='Units allocated to this LP',
    )
    side_letter_exists = models.BooleanField(
        default=False,
        help_text='Whether this LP has differential rights via side letter',
    )
    subscription_form_url = models.URLField(
        max_length=500, blank=True,
        help_text='Link to signed subscription form',
    )

    commitment_status = models.CharField(
        max_length=15, choices=STATUS_CHOICES, default='active',
    )

    # Per-LP cumulative figures. Many fund-admin Excels publish per-LP
    # cumulative drawn-down and cumulative distributed amounts directly on
    # the Investors / LP-Master sheet, separately from the per-event
    # Capital Calls / Distributions sheets. We persist them here so the
    # Called Capital / DPI dashboard tiles reflect reality on funds whose
    # explicit event sheets are sparse. Both nullable — sparse Excels skip.
    cumulative_called = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Per-LP cumulative drawdown to date (from Investors/LP-Master sheet)',
    )
    cumulative_distributed = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Per-LP cumulative distributions received to date',
    )

    # Bank account for capital call payments
    primary_bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='commitments',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['scheme', 'investor']
        indexes = [
            models.Index(fields=['investor']),
            models.Index(fields=['scheme']),
        ]

    def __str__(self):
        return f'{self.investor.investor_name} → {self.scheme} ({self.commitment_amount})'


class CapitalCall(models.Model):
    """
    A capital call event at the scheme level.
    Maps to FundOS: capital_calls table.
    """
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('approved', 'Approved'),
        ('sent', 'Sent'),
        ('paid', 'Paid'),
        ('defaulted', 'Defaulted'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scheme = models.ForeignKey(
        'funds.Scheme',
        on_delete=models.CASCADE,
        related_name='capital_calls',
    )
    call_number = models.PositiveIntegerField(
        help_text='Sequential call number per scheme (1, 2, 3...)',
    )
    call_date = models.DateField()
    payment_due_date = models.DateField(
        db_index=True,
        help_text='Indexed for deadline tracking',
    )
    call_percentage = models.DecimalField(
        max_digits=5, decimal_places=2,
        help_text='Percentage of commitment being called (e.g., 25.00)',
    )
    total_call_amount = models.DecimalField(
        max_digits=18, decimal_places=2,
        help_text='Total amount being called across all LPs',
    )
    purpose = models.TextField(
        blank=True,
        help_text='Purpose of the capital call (investment, fees, expenses)',
    )
    call_status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default='draft',
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
        ordering = ['scheme', 'call_number']
        unique_together = ('scheme', 'call_number')

    def __str__(self):
        return f'{self.scheme} — Call #{self.call_number} ({self.call_percentage}%)'


class CapitalCallLineItem(models.Model):
    """
    Per-LP breakdown of a capital call.
    Maps to FundOS: capital_call_line_items table.
    """
    PAYMENT_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('partial', 'Partially Paid'),
        ('defaulted', 'Defaulted'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    capital_call = models.ForeignKey(
        CapitalCall,
        on_delete=models.CASCADE,
        related_name='line_items',
    )
    commitment = models.ForeignKey(
        Commitment,
        on_delete=models.CASCADE,
        related_name='call_line_items',
    )

    called_amount = models.DecimalField(
        max_digits=18, decimal_places=2,
        help_text='This LP\'s share of this call',
    )
    cumulative_called_pct = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='Percentage of commitment called to date',
    )
    units_allotted = models.DecimalField(
        max_digits=18, decimal_places=6, null=True, blank=True,
        help_text='Units allotted to LP for this call',
    )

    # Payment tracking
    payment_status = models.CharField(
        max_length=10, choices=PAYMENT_STATUS_CHOICES, default='pending',
    )
    amount_received = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
    )
    payment_date = models.DateField(null=True, blank=True)
    utr_number = models.CharField(
        max_length=50, blank=True,
        help_text='Unique Transaction Reference for payment verification',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['capital_call', 'commitment']
        indexes = [
            models.Index(fields=['capital_call']),
            models.Index(fields=['commitment']),
        ]

    def __str__(self):
        return f'{self.commitment.investor.investor_name} — {self.called_amount}'


class Distribution(models.Model):
    """
    A distribution event at the scheme level.
    Maps to FundOS: distributions table.
    """
    DISTRIBUTION_TYPE_CHOICES = [
        ('return_of_capital', 'Return of Capital'),
        ('stcg', 'Short Term Capital Gain'),
        ('ltcg', 'Long Term Capital Gain'),
        ('interest', 'Interest Income'),
        ('dividend', 'Dividend'),
        ('carry', 'Carried Interest Distribution'),
        ('other', 'Other'),
    ]
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('approved', 'Approved'),
        ('distributed', 'Distributed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scheme = models.ForeignKey(
        'funds.Scheme',
        on_delete=models.CASCADE,
        related_name='distributions',
    )
    distribution_number = models.PositiveIntegerField(
        help_text='Sequential distribution number per scheme',
    )
    distribution_date = models.DateField()
    distribution_type = models.CharField(
        max_length=20, choices=DISTRIBUTION_TYPE_CHOICES,
    )

    total_gross_amount = models.DecimalField(
        max_digits=18, decimal_places=2,
        help_text='Total gross distribution before TDS',
    )
    total_tds_amount = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Total TDS withheld across all LPs',
    )
    total_net_amount = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Total net distribution after TDS',
    )

    # Source of distribution
    related_exit_event = models.ForeignKey(
        'investments.ExitEvent',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='distributions',
        help_text='Link to the exit event that generated this distribution',
    )

    distribution_status = models.CharField(
        max_length=15, choices=STATUS_CHOICES, default='draft',
    )
    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['scheme', 'distribution_number']
        unique_together = ('scheme', 'distribution_number')

    def __str__(self):
        return f'{self.scheme} — Distribution #{self.distribution_number}'


class DistributionLineItem(models.Model):
    """
    Per-LP breakdown of a distribution, including TDS calculation.
    Maps to FundOS: distribution_line_items (implied).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    distribution = models.ForeignKey(
        Distribution,
        on_delete=models.CASCADE,
        related_name='line_items',
    )
    commitment = models.ForeignKey(
        Commitment,
        on_delete=models.CASCADE,
        related_name='distribution_line_items',
    )

    gross_amount = models.DecimalField(
        max_digits=18, decimal_places=2,
        help_text='This LP\'s share of gross distribution',
    )
    tds_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text='TDS rate applied (varies by distribution type + investor type)',
    )
    tds_amount = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='TDS withheld for this LP',
    )
    net_amount = models.DecimalField(
        max_digits=18, decimal_places=2,
        help_text='Net amount payable to LP after TDS',
    )
    units_redeemed = models.DecimalField(
        max_digits=18, decimal_places=6, null=True, blank=True,
        help_text='Units redeemed for this distribution',
    )

    # Payment tracking
    payment_date = models.DateField(null=True, blank=True)
    utr_number = models.CharField(max_length=50, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['distribution', 'commitment']

    def __str__(self):
        return f'{self.commitment.investor.investor_name} — Net {self.net_amount}'


class LPCapitalAccount(models.Model):
    """
    LP capital account snapshot — point-in-time record per LP per date.
    Maps to FundOS: lp_capital_accounts table.

    SOURCE OF TRUTH for LP economics. SEBI's AAR requires this historical data.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    commitment = models.ForeignKey(
        Commitment,
        on_delete=models.CASCADE,
        related_name='capital_accounts',
    )
    as_of_date = models.DateField(
        db_index=True,
        help_text='Snapshot date — UNIQUE per commitment per date',
    )

    # Capital flows
    committed_capital = models.DecimalField(
        max_digits=18, decimal_places=2,
        help_text='Total LP commitment',
    )
    called_capital = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Total drawn to date',
    )
    uncalled_capital = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Remaining commitment (committed - called)',
    )
    distributed_capital = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Total returned to LP to date',
    )

    # Valuation
    unrealized_value = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='FMV of portfolio at this date (LP\'s share)',
    )
    total_value = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='distributed_capital + unrealized_value',
    )

    # Performance metrics (SEBI reporting)
    irr = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text='Since-inception IRR for this LP (e.g., 0.1520 = 15.20%)',
    )
    tvpi = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text='Total Value to Paid-In multiple',
    )
    dpi = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text='Distribution to Paid-In',
    )
    rvpi = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text='Residual Value to Paid-In',
    )
    moic = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text='Multiple on Invested Capital',
    )

    # Units
    units_held = models.DecimalField(
        max_digits=18, decimal_places=6, null=True, blank=True,
    )

    # Fees charged
    management_fee_charged = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
    )
    carried_interest_charged = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['commitment', '-as_of_date']
        unique_together = ('commitment', 'as_of_date')
        indexes = [
            models.Index(fields=['as_of_date']),
        ]

    def __str__(self):
        return f'{self.commitment.investor.investor_name} — {self.as_of_date}'
