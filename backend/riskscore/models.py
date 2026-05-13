"""
Risk Score models — ML-based portfolio company risk assessment.

Risk score: 0-100 (0 = lowest risk, 100 = highest risk)
Tier: LOW (0-33), MEDIUM (34-66), HIGH (67-100)

Based on 10 signals per the v5 flowchart:
  1. Revenue growth vs plan
  2. EBITDA margin trend
  3. Cash burn & runway
  4. Working capital ratio
  5. Debt service coverage
  6. Customer concentration
  7. Management team changes
  8. Market conditions
  9. Peer comparisons
  10. Compliance status
"""

import uuid
from django.db import models


class CompanyRiskScore(models.Model):
    """
    Risk score for a portfolio company as of a given date.
    Computed by the scoring engine and stored here for dashboard display.
    """
    TIER_CHOICES = [
        ('low',    'LOW (0-33)'),
        ('medium', 'MEDIUM (34-66)'),
        ('high',   'HIGH (67-100)'),
    ]
    METHOD_CHOICES = [
        ('rule_based', 'Rule-Based (Phase 1)'),
        ('xgboost',    'XGBoost Ensemble (Phase 2)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    portfolio_company = models.ForeignKey(
        'investments.PortfolioCompany',
        on_delete=models.CASCADE,
        related_name='risk_scores',
    )
    score_date = models.DateField(help_text='Date of risk score computation')

    # Overall score
    risk_score = models.DecimalField(
        max_digits=5, decimal_places=2,
        help_text='Composite risk score 0-100 (higher = more risk)',
    )
    risk_tier = models.CharField(max_length=8, choices=TIER_CHOICES)
    method = models.CharField(max_length=12, choices=METHOD_CHOICES, default='rule_based')

    # 10 signal scores (0-10 each, higher = more risk)
    signal_revenue_vs_plan = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    signal_ebitda_margin_trend = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    signal_cash_burn_runway = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    signal_working_capital = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    signal_debt_service = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    signal_customer_concentration = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    signal_mgmt_changes = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    signal_market_conditions = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    signal_peer_comparisons = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    signal_compliance_status = models.DecimalField(max_digits=4, decimal_places=2, default=0)

    # Explanation / flags
    flags = models.JSONField(
        default=list, blank=True,
        help_text='List of risk flags e.g. ["Cash runway < 6 months", "Revenue 30% below plan"]',
    )
    ai_commentary = models.TextField(
        blank=True,
        help_text='Gemini-generated natural language risk summary',
    )

    # Trend vs previous score
    previous_score = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='Previous risk score (for trend display)',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['portfolio_company', '-score_date']
        unique_together = ('portfolio_company', 'score_date')
        indexes = [
            models.Index(fields=['risk_tier', 'score_date']),
        ]

    def __str__(self):
        return f'{self.portfolio_company.name} — Risk {self.risk_score} ({self.risk_tier.upper()}) @ {self.score_date}'
