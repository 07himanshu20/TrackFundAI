"""
TDS Module — TDS withholding on distributions + 26Q quarterly return filing.
v5 Compliance: TDS 26Q model + quarterly filing engine.

Indian tax law context (as CA):
- Section 194LBA: TDS on business trust income @ 10% (residents), 5%+surcharge (NR)
- Section 115UA: Pass-through taxation for Category I/II AIFs
- Form 26Q: Quarterly TDS return for non-salary payments
- TDS on capital gains at exit: Section 194E/195 for NR investors
"""
import uuid
from decimal import Decimal
from django.conf import settings
from django.db import models


class TDSWithholding(models.Model):
    """
    Individual TDS withholding record per distribution / exit payment.
    One record per payment event (capital call return, distribution, exit proceeds).
    """
    PAYMENT_NATURE_CHOICES = [
        ('distribution', 'Distribution of Profits — 194LBA'),
        ('return_of_capital', 'Return of Capital — Not Taxable'),
        ('interest', 'Interest — 194A'),
        ('exit_proceeds_resident', 'Exit Proceeds — Resident — 194'),
        ('exit_proceeds_nri', 'Exit Proceeds — NRI — 195'),
        ('management_fee', 'Management Fee — 194J'),
        ('other', 'Other Payment'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE, related_name='tds_withholdings',
    )
    fund = models.ForeignKey(
        'funds.Fund', on_delete=models.CASCADE, related_name='tds_withholdings',
    )

    # Deductee (LP / counterparty)
    deductee_name = models.CharField(max_length=255)
    deductee_pan = models.CharField(max_length=10, blank=True, help_text='10-character PAN')
    deductee_tan = models.CharField(max_length=10, blank=True, help_text='TAN of deductor')
    deductee_is_nri = models.BooleanField(default=False)
    deductee_country = models.CharField(max_length=100, blank=True, default='India')

    # Linked LP investor (if applicable)
    investor = models.ForeignKey(
        'lp.Investor', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='tds_withholdings',
    )

    # Payment details
    payment_date = models.DateField()
    payment_nature = models.CharField(max_length=30, choices=PAYMENT_NATURE_CHOICES)
    gross_amount_inr = models.DecimalField(max_digits=18, decimal_places=2)
    tds_rate_pct = models.DecimalField(
        max_digits=5, decimal_places=3,
        help_text='TDS rate applied (e.g., 10.000 for 10%)',
    )
    surcharge_pct = models.DecimalField(max_digits=5, decimal_places=3, default=Decimal('0.000'))
    cess_pct = models.DecimalField(
        max_digits=5, decimal_places=3, default=Decimal('4.000'),
        help_text='Health & Education Cess on tax',
    )

    # Computed fields
    base_tax_inr = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    surcharge_inr = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    cess_inr = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_tds_inr = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    net_payment_inr = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    # Filing status
    challan_no = models.CharField(max_length=50, blank=True)
    challan_date = models.DateField(null=True, blank=True)
    bsr_code = models.CharField(max_length=7, blank=True, help_text='Bank BSR code for challan')
    deposited_to_govt = models.BooleanField(default=False)
    deposit_date = models.DateField(null=True, blank=True)

    # Form 26Q quarter reference
    quarter = models.CharField(
        max_length=5, blank=True,
        help_text='e.g. Q1, Q2, Q3, Q4 of FY (Apr-Jun, Jul-Sep, Oct-Dec, Jan-Mar)',
    )
    financial_year = models.CharField(
        max_length=7, blank=True,
        help_text='e.g. FY2025 (Apr 2024 — Mar 2025)',
    )

    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-payment_date']
        indexes = [
            models.Index(fields=['organization', 'financial_year', 'quarter'], name='tds_org_fy_q_idx'),
        ]

    def save(self, *args, **kwargs):
        """Auto-compute TDS components before saving."""
        self.base_tax_inr = self.gross_amount_inr * (self.tds_rate_pct / 100)
        self.surcharge_inr = self.base_tax_inr * (self.surcharge_pct / 100)
        tax_plus_surcharge = self.base_tax_inr + self.surcharge_inr
        self.cess_inr = tax_plus_surcharge * (self.cess_pct / 100)
        self.total_tds_inr = tax_plus_surcharge + self.cess_inr
        self.net_payment_inr = self.gross_amount_inr - self.total_tds_inr

        # Auto-set quarter and FY from payment_date
        if self.payment_date and not self.quarter:
            month = self.payment_date.month
            year = self.payment_date.year
            if month in (4, 5, 6):
                self.quarter = 'Q1'
                fy_year = year
            elif month in (7, 8, 9):
                self.quarter = 'Q2'
                fy_year = year
            elif month in (10, 11, 12):
                self.quarter = 'Q3'
                fy_year = year
            else:  # Jan, Feb, Mar
                self.quarter = 'Q4'
                fy_year = year - 1
            self.financial_year = f'FY{fy_year + 1}'
        super().save(*args, **kwargs)

    def __str__(self):
        return f'TDS: {self.deductee_name} — {self.gross_amount_inr} @ {self.tds_rate_pct}% on {self.payment_date}'


class Form26QReturn(models.Model):
    """
    Quarterly Form 26Q TDS return for non-salary deductions.
    One per organization per FY per quarter.
    """
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('computed', 'Computed'),
        ('filed', 'Filed with TRACES'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected — Resubmit'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE, related_name='form26q_returns',
    )
    financial_year = models.CharField(max_length=7, help_text='e.g. FY2025')
    quarter = models.CharField(
        max_length=2, choices=[('Q1', 'Q1 Apr-Jun'), ('Q2', 'Q2 Jul-Sep'),
                                ('Q3', 'Q3 Oct-Dec'), ('Q4', 'Q4 Jan-Mar')],
    )
    due_date = models.DateField(help_text='Statutory filing due date')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')

    # Aggregate statistics (auto-populated by compute())
    total_transactions = models.IntegerField(default=0)
    total_gross_payment_inr = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_tds_deducted_inr = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_tds_deposited_inr = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    # TRACES filing details
    traces_ack_no = models.CharField(max_length=50, blank=True)
    filed_date = models.DateField(null=True, blank=True)
    filed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )

    # Generated return file
    return_document = models.ForeignKey(
        'documents.Document', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='form26q_returns',
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-financial_year', 'quarter']
        unique_together = ('organization', 'financial_year', 'quarter')

    def __str__(self):
        return f'26Q: {self.organization} — {self.financial_year} {self.quarter}'

    def compute(self):
        """Aggregate all TDSWithholding records for this FY/Quarter."""
        from django.db.models import Sum, Count
        qs = TDSWithholding.objects.filter(
            organization=self.organization,
            financial_year=self.financial_year,
            quarter=self.quarter,
        )
        agg = qs.aggregate(
            count=Count('id'),
            gross=Sum('gross_amount_inr'),
            tds=Sum('total_tds_inr'),
            deposited=Sum('total_tds_inr', filter=models.Q(deposited_to_govt=True)),
        )
        self.total_transactions = agg['count'] or 0
        self.total_gross_payment_inr = agg['gross'] or 0
        self.total_tds_deducted_inr = agg['tds'] or 0
        self.total_tds_deposited_inr = agg['deposited'] or 0
        self.status = 'computed'
        self.save()
        return self
