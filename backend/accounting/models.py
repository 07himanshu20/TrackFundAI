"""
Fund Accounting app models — Module 4 of FundOS India schema.

Tables: ChartOfAccounts, NAVRecord, CarriedInterest, FundLedger,
ManagementFeeSchedule.

This module handles NAV computation, carried interest waterfall,
double-entry fund accounting, and management fee scheduling.
"""

import uuid
from django.conf import settings
from django.db import models


class ChartOfAccounts(models.Model):
    """
    Chart of accounts for double-entry fund accounting.
    Maps to FundOS: chart_of_accounts (implied).

    Standard fund accounting accounts: Cash, Investments at Cost,
    Unrealized Gain/Loss, Management Fee Payable, Carried Interest Payable, etc.
    """
    ACCOUNT_TYPE_CHOICES = [
        ('asset', 'Asset'),
        ('liability', 'Liability'),
        ('equity', 'Equity'),
        ('income', 'Income'),
        ('expense', 'Expense'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='chart_of_accounts',
    )
    account_code = models.CharField(
        max_length=20,
        help_text='Account code (e.g., 1000 = Cash, 1100 = Investments at Cost)',
    )
    account_name = models.CharField(max_length=255)
    account_type = models.CharField(max_length=10, choices=ACCOUNT_TYPE_CHOICES)
    parent_account = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='sub_accounts',
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['account_code']
        unique_together = ('organization', 'account_code')
        verbose_name_plural = 'chart of accounts'

    def __str__(self):
        return f'{self.account_code} — {self.account_name}'


class NAVRecord(models.Model):
    """
    NAV record per scheme per date.
    Maps to FundOS: nav_records table.

    SEBI Critical — NAV must match depository records (CDSL/NSDL).
    This drives LP capital accounts and is reported in QAR/AAR.
    """
    DEPOSITORY_CHOICES = [
        ('cdsl', 'CDSL'),
        ('nsdl', 'NSDL'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scheme = models.ForeignKey(
        'funds.Scheme',
        on_delete=models.CASCADE,
        related_name='nav_records',
    )
    nav_date = models.DateField(
        help_text='Date of NAV calculation',
    )

    # NAV computation
    total_nav = models.DecimalField(
        max_digits=18, decimal_places=2,
        help_text='Total NAV of the scheme (sum of all asset values)',
    )
    total_units_outstanding = models.DecimalField(
        max_digits=18, decimal_places=6,
        help_text='Total units outstanding for the scheme',
    )
    nav_per_unit = models.DecimalField(
        max_digits=18, decimal_places=6,
        help_text='SEBI: NAV per unit — must match depository records',
    )

    # NAV components
    investments_at_fair_value = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Total fair value of all investments',
    )
    cash_and_equivalents = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
    )
    receivables = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
    )
    management_fee_payable = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
    )
    other_liabilities = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
    )

    # Depository reconciliation (SEBI mandatory)
    depository_type = models.CharField(
        max_length=4, choices=DEPOSITORY_CHOICES, blank=True,
        help_text='SEBI: CDSL or NSDL — for reconciliation',
    )
    depository_reconciled = models.BooleanField(
        default=False, db_index=True,
        help_text='SEBI: FALSE = incomplete for AAR filing',
    )
    depository_variance_amount = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Difference if any — must be zero for clean AAR',
    )

    # Gains (imported from NAV/Accounting sheet)
    unrealized_gains = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Unrealized gains from portfolio revaluation',
    )
    realized_gains = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Realized gains from exits/distributions',
    )

    # Approval
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
        ordering = ['scheme', '-nav_date']
        unique_together = ('scheme', 'nav_date')
        indexes = [
            models.Index(fields=['scheme', 'nav_date']),
            models.Index(fields=['depository_reconciled']),
        ]

    def __str__(self):
        return f'{self.scheme} — NAV {self.nav_date} ({self.nav_per_unit})'


class CarriedInterest(models.Model):
    """
    Carried interest calculation per scheme per period.
    Maps to FundOS: carried_interest table.

    Implements the waterfall: preferred return → catch-up → carry split.
    """
    CALCULATION_STATUS_CHOICES = [
        ('indicative', 'Indicative'),
        ('crystallised', 'Crystallised'),
        ('paid', 'Paid'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scheme = models.ForeignKey(
        'funds.Scheme',
        on_delete=models.CASCADE,
        related_name='carried_interest_records',
    )
    calculation_date = models.DateField(
        help_text='Date of carry calculation',
    )

    # Waterfall inputs
    total_distributions = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Total distributions to LPs to date',
    )
    total_called_capital = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Total capital called to date',
    )
    preferred_return_amount = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Hurdle cleared before carry kicks in',
    )
    carry_base = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Profit above hurdle subject to carry',
    )

    # Carry amounts
    carry_amount_gross = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='GP\'s carried interest entitlement (gross)',
    )
    carry_amount_net = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='GP\'s carried interest after clawback provisions',
    )
    gp_clawback_provision = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Excess carry that may be returned to LPs',
    )

    calculation_status = models.CharField(
        max_length=15, choices=CALCULATION_STATUS_CHOICES, default='indicative',
    )
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['scheme', '-calculation_date']

    def __str__(self):
        return f'{self.scheme} — Carry {self.calculation_date} ({self.calculation_status})'


class FundLedger(models.Model):
    """
    Double-entry fund accounting ledger.
    Maps to FundOS: fund_ledger table.

    Every financial transaction (capital call, investment, distribution, fee)
    posts debit/credit entries here. This is the accounting backbone.
    """
    REFERENCE_TYPE_CHOICES = [
        ('capital_call', 'Capital Call'),
        ('investment', 'Investment'),
        ('distribution', 'Distribution'),
        ('management_fee', 'Management Fee'),
        ('carried_interest', 'Carried Interest'),
        ('valuation_adjustment', 'Valuation Adjustment'),
        ('expense', 'Expense'),
        ('other', 'Other'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scheme = models.ForeignKey(
        'funds.Scheme',
        on_delete=models.CASCADE,
        related_name='ledger_entries',
    )
    journal_entry_number = models.CharField(
        max_length=30,
        help_text='Sequential JE number for audit trail',
    )
    entry_date = models.DateField()
    description = models.CharField(max_length=500, blank=True)

    # Double-entry
    debit_account = models.ForeignKey(
        ChartOfAccounts,
        on_delete=models.PROTECT,
        related_name='debit_entries',
        help_text='Account debited',
    )
    credit_account = models.ForeignKey(
        ChartOfAccounts,
        on_delete=models.PROTECT,
        related_name='credit_entries',
        help_text='Account credited',
    )
    amount = models.DecimalField(max_digits=18, decimal_places=2)

    # Reference to source transaction
    reference_type = models.CharField(
        max_length=25, choices=REFERENCE_TYPE_CHOICES,
    )
    reference_id = models.UUIDField(
        null=True, blank=True,
        help_text='Links to source record (capital_call, investment, distribution, etc.)',
    )

    # Audit
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    is_reversed = models.BooleanField(
        default=False,
        help_text='Whether this entry has been reversed (never delete ledger entries)',
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['scheme', '-entry_date', 'journal_entry_number']
        indexes = [
            models.Index(fields=['scheme', 'entry_date']),
            models.Index(fields=['reference_type', 'reference_id']),
        ]

    def __str__(self):
        return f'JE {self.journal_entry_number} — {self.entry_date} ({self.amount})'


class ManagementFeeSchedule(models.Model):
    """
    Management fee schedule per scheme per period.
    Maps to FundOS: management_fee_schedules (implied).

    Tracks fee calculation basis (committed/called/NAV), rate, and amounts.
    """
    FEE_STATUS_CHOICES = [
        ('calculated', 'Calculated'),
        ('invoiced', 'Invoiced'),
        ('paid', 'Paid'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scheme = models.ForeignKey(
        'funds.Scheme',
        on_delete=models.CASCADE,
        related_name='management_fee_schedules',
    )
    period_start = models.DateField()
    period_end = models.DateField()

    fee_basis_amount = models.DecimalField(
        max_digits=18, decimal_places=2,
        help_text='The base amount (committed, called, or NAV) used to calculate fee',
    )
    fee_rate = models.DecimalField(
        max_digits=5, decimal_places=2,
        help_text='Annual fee rate (e.g., 2.00 = 2%)',
    )
    fee_amount = models.DecimalField(
        max_digits=18, decimal_places=2,
        help_text='Calculated management fee for this period',
    )
    gst_amount = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='GST on management fee (18% in India)',
    )
    total_fee_with_gst = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='fee_amount + gst_amount',
    )

    fee_status = models.CharField(
        max_length=15, choices=FEE_STATUS_CHOICES, default='calculated',
    )
    invoice_number = models.CharField(max_length=50, blank=True)
    invoice_date = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['scheme', '-period_start']
        unique_together = ('scheme', 'period_start', 'period_end')

    def __str__(self):
        return f'{self.scheme} — Fee {self.period_start} to {self.period_end}'
