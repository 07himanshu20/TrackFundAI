"""
MIS Consolidation — Budget vs Actual + Cross-company P&L rollup.
v5 MIS Reporting: BvA model, variance engine, 6-month MIS aggregator.

CA-grade implementation:
- BvA per company per period per line item
- Variance = Actual - Budget (positive = favorable for revenue, unfavorable for cost)
- Variance% = (Actual - Budget) / |Budget| × 100
- Cross-company rollup: aggregate all portfolio companies' P&L for a fund/scheme
- 6-month rolling window for trend charts
"""
import uuid
from decimal import Decimal
from django.db import models


LINE_ITEM_CHOICES = [
    # Revenue
    ('revenue', 'Revenue / Net Sales'),
    ('other_income', 'Other Income'),
    ('total_revenue', 'Total Revenue'),
    # Costs
    ('cogs', 'Cost of Goods Sold (COGS)'),
    ('gross_profit', 'Gross Profit'),
    ('employee_cost', 'Employee / Payroll Cost'),
    ('marketing_cost', 'Marketing & Sales Cost'),
    ('rd_cost', 'R&D Cost'),
    ('g_and_a', 'G&A / Overheads'),
    ('total_opex', 'Total Operating Expenses'),
    # Profitability
    ('ebitda', 'EBITDA'),
    ('depreciation', 'Depreciation & Amortisation'),
    ('ebit', 'EBIT'),
    ('finance_cost', 'Finance Cost / Interest'),
    ('pbt', 'Profit Before Tax (PBT)'),
    ('tax', 'Tax (Current + Deferred)'),
    ('pat', 'Profit After Tax (PAT)'),
    # Balance Sheet (summary)
    ('total_assets', 'Total Assets'),
    ('total_debt', 'Total Debt'),
    ('cash_and_equivalents', 'Cash & Equivalents'),
    ('net_worth', 'Net Worth / Equity'),
    # Investment / CapEx
    ('capex', 'Capex / Capital Expenditure'),
    ('working_capital', 'Working Capital'),
    ('net_working_capital', 'Net Working Capital'),
    # Other
    ('dividend', 'Dividend Paid'),
    ('other_cost', 'Other Cost / Expense'),
    # Fund-level performance metrics (stored in ConsolidatedMIS from BvA sheets)
    ('net_irr',      'Net IRR (Fund Level)'),
    ('tvpi',         'TVPI (Total Value to Paid-In)'),
    ('portfolio_fv', 'Portfolio Fair Value'),
]


class BudgetVsActual(models.Model):
    """
    Budget vs Actual record — one row per company per fund per period per line item.
    Stores budget, actual, and auto-computed variance.
    Fund-scoped: a company shared between two funds gets separate BvA records per fund.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    portfolio_company = models.ForeignKey(
        'investments.PortfolioCompany', on_delete=models.CASCADE,
        related_name='bva_records',
    )
    fund = models.ForeignKey(
        'funds.Fund', on_delete=models.CASCADE,
        related_name='bva_records', null=True, blank=True,
        help_text='Fund this BvA record belongs to — prevents cross-fund contamination',
    )
    period_year = models.PositiveIntegerField(help_text='Financial year e.g. 2025')
    period_month = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Month 1-12 for monthly; null for quarterly/annual',
    )
    period_quarter = models.CharField(
        max_length=2, blank=True,
        choices=[('Q1', 'Q1'), ('Q2', 'Q2'), ('Q3', 'Q3'), ('Q4', 'Q4')],
        help_text='Quarter if monthly is null',
    )
    period_type = models.CharField(
        max_length=10,
        choices=[('monthly', 'Monthly'), ('quarterly', 'Quarterly'), ('annual', 'Annual')],
        default='monthly',
    )

    line_item = models.CharField(max_length=30, choices=LINE_ITEM_CHOICES)

    budget_inr = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Budgeted amount in INR (Lakhs)',
    )
    actual_inr = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Actual amount in INR (Lakhs)',
    )

    # Auto-computed
    variance_inr = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Actual - Budget (positive = over-achievement for revenue)',
    )
    variance_pct = models.DecimalField(
        max_digits=8, decimal_places=3, null=True, blank=True,
        help_text='(Actual - Budget) / |Budget| × 100',
    )
    is_favorable = models.BooleanField(
        null=True, blank=True,
        help_text='True if variance is favorable (revenue lines: actual>budget; cost lines: actual<budget)',
    )

    COST_LINE_ITEMS = {
        'cogs', 'employee_cost', 'marketing_cost', 'rd_cost', 'g_and_a',
        'total_opex', 'depreciation', 'finance_cost', 'tax',
    }

    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-period_year', '-period_month', 'portfolio_company']
        indexes = [
            models.Index(fields=['portfolio_company', 'period_year', 'period_month'], name='bva_company_period_idx'),
        ]
        unique_together = ('portfolio_company', 'fund', 'period_year', 'period_month', 'period_quarter', 'line_item')

    def save(self, *args, **kwargs):
        """Auto-compute variance fields."""
        if self.budget_inr is not None and self.actual_inr is not None:
            self.variance_inr = self.actual_inr - self.budget_inr
            if self.budget_inr != 0:
                self.variance_pct = (self.variance_inr / abs(self.budget_inr)) * 100
            else:
                self.variance_pct = None

            # Favorable logic: revenue over-achievement or cost under-run
            if self.line_item in self.COST_LINE_ITEMS:
                self.is_favorable = self.variance_inr < 0  # cost less than budget = good
            else:
                self.is_favorable = self.variance_inr > 0  # revenue more than budget = good
        super().save(*args, **kwargs)

    def __str__(self):
        return f'BvA: {self.portfolio_company} — {self.line_item} {self.period_year}-{self.period_month}'


class ConsolidatedMIS(models.Model):
    """
    Cross-company P&L rollup — aggregated across all portfolio companies for a fund/scheme.
    Populated by the MIS Aggregator service (Celery task).
    One record per fund per period per line item.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE, related_name='consolidated_mis',
    )
    fund = models.ForeignKey(
        'funds.Fund', on_delete=models.CASCADE, related_name='consolidated_mis',
    )
    scheme = models.ForeignKey(
        'funds.Scheme', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='consolidated_mis',
    )

    period_year = models.PositiveIntegerField()
    period_month = models.PositiveIntegerField(null=True, blank=True)
    period_quarter = models.CharField(max_length=2, blank=True)
    period_type = models.CharField(
        max_length=10,
        choices=[('monthly', 'Monthly'), ('quarterly', 'Quarterly'), ('annual', 'Annual')],
        default='monthly',
    )

    line_item = models.CharField(max_length=30, choices=LINE_ITEM_CHOICES)

    # Aggregate values
    total_actual_inr = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    total_budget_inr = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    company_count = models.IntegerField(default=0, help_text='Number of companies contributing')

    # Computed from BvA aggregation
    total_variance_inr = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    total_variance_pct = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)

    computed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-period_year', '-period_month', 'line_item']
        unique_together = ('fund', 'scheme', 'period_year', 'period_month', 'period_quarter', 'line_item')

    def __str__(self):
        return f'ConsolidatedMIS: {self.fund} — {self.line_item} {self.period_year}-{self.period_month}'


class MISAnomalyAlert(models.Model):
    """
    Anomaly detected in MIS data (budget variance alert, statistical outlier).
    v5: MIS Anomaly Detection — triggers LP briefing notification.
    """
    SEVERITY_CHOICES = [
        ('critical', 'Critical (>50% variance)'),
        ('high', 'High (25-50% variance)'),
        ('medium', 'Medium (10-25% variance)'),
        ('low', 'Low (<10% variance)'),
    ]
    ANOMALY_TYPE_CHOICES = [
        ('budget_variance', 'Budget Variance Exceeded Threshold'),
        ('revenue_decline', 'Revenue Decline >20% MoM'),
        ('cash_burn', 'Cash Burn Acceleration'),
        ('ebitda_compression', 'EBITDA Margin Compression'),
        ('statistical_outlier', 'Statistical Outlier (Z-score >2)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bva_record = models.ForeignKey(
        BudgetVsActual, on_delete=models.CASCADE, related_name='anomaly_alerts',
        null=True, blank=True,
    )
    portfolio_company = models.ForeignKey(
        'investments.PortfolioCompany', on_delete=models.CASCADE,
        related_name='mis_anomalies',
    )
    anomaly_type = models.CharField(max_length=25, choices=ANOMALY_TYPE_CHOICES)
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES)
    description = models.TextField()
    detected_at = models.DateTimeField(auto_now_add=True)
    resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    notification_sent = models.BooleanField(default=False)

    class Meta:
        ordering = ['-detected_at']

    def __str__(self):
        return f'Anomaly: {self.portfolio_company} — {self.anomaly_type} ({self.severity})'
