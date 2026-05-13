"""
Fund Close — Final accounts, carry clawback calc, SEBI deregistration.
v5 Fund Lifecycle: Fund Close section.

Carry waterfall (European style):
  1. Return of capital to LPs
  2. Preferred return @ 8% hurdle
  3. GP carry @ 20% of profits above hurdle
  4. Clawback provision if GP was overpaid mid-life
"""
import uuid
from decimal import Decimal
from django.conf import settings
from django.db import models


class FundCloseEvent(models.Model):
    """
    Marks the beginning of wind-down / close process for a fund.
    One per fund (or per scheme if scheme-level close).
    """
    STATUS_CHOICES = [
        ('initiated', 'Close Initiated'),
        ('final_accounts', 'Final Accounts Being Prepared'),
        ('carry_calc', 'Carry & Clawback Calculation'),
        ('lp_distribution', 'LP Final Distribution'),
        ('sebi_filing', 'SEBI Deregistration Filing'),
        ('deregistered', 'SEBI Deregistered'),
        ('closed', 'Fund Closed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fund = models.ForeignKey(
        'funds.Fund', on_delete=models.CASCADE, related_name='close_events',
    )
    scheme = models.ForeignKey(
        'funds.Scheme', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='close_events',
        help_text='If scheme-level close; null = fund-level',
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='initiated')
    initiation_date = models.DateField()
    target_close_date = models.DateField(null=True, blank=True)
    actual_close_date = models.DateField(null=True, blank=True)

    # Final NAV snapshot
    final_nav_inr = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Final NAV at close date (INR Cr)',
    )
    total_invested_inr = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Total capital invested (INR Cr)',
    )
    total_realized_inr = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Total realized proceeds from exits (INR Cr)',
    )

    # MOIC and IRR at close
    final_moic = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    final_irr_pct = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)

    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-initiation_date']
        unique_together = ('fund', 'scheme')

    def __str__(self):
        return f'Close: {self.fund.name} — {self.get_status_display()}'


class ClawbackCalculation(models.Model):
    """
    GP carry clawback calculation.
    If mid-life carry payments to GP exceeded what the final waterfall allows,
    the GP must return the excess to LPs.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    close_event = models.OneToOneField(
        FundCloseEvent, on_delete=models.CASCADE, related_name='clawback',
    )
    calc_date = models.DateField()

    # Capital inputs
    total_committed_capital_inr = models.DecimalField(max_digits=18, decimal_places=2)
    total_drawn_capital_inr = models.DecimalField(max_digits=18, decimal_places=2)
    total_distributions_inr = models.DecimalField(max_digits=18, decimal_places=2)

    # Hurdle and carry parameters
    hurdle_rate_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('8.00'),
        help_text='Preferred return hurdle (default 8% p.a.)',
    )
    carry_rate_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('20.00'),
        help_text='GP carry rate (default 20%)',
    )

    # Waterfall outputs
    return_of_capital_inr = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    preferred_return_inr = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Hurdle return owed to LPs = drawn_capital × hurdle_rate × years',
    )
    profit_above_hurdle_inr = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    gp_carry_owed_inr = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Carry GP should receive = profit_above_hurdle × carry_rate',
    )
    gp_carry_paid_inr = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Carry already paid to GP during fund life',
    )
    clawback_amount_inr = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='GP must return this to LPs if positive (overpaid); LP tops up if negative',
    )
    clawback_direction = models.CharField(
        max_length=10,
        choices=[('gp_owes', 'GP Owes LPs'), ('none', 'No Clawback'), ('lp_owes', 'LP Topup')],
        default='none',
    )

    # Settlement
    settled = models.BooleanField(default=False)
    settled_date = models.DateField(null=True, blank=True)
    settlement_notes = models.TextField(blank=True)

    calculated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-calc_date']

    def compute_waterfall(self, fund_life_years: float):
        """
        Compute European-style waterfall and set all output fields.
        Call this before save().
        """
        drawn = self.total_drawn_capital_inr
        distributed = self.total_distributions_inr
        profit = max(distributed - drawn, Decimal('0'))

        self.return_of_capital_inr = min(distributed, drawn)
        preferred = drawn * (self.hurdle_rate_pct / 100) * Decimal(str(fund_life_years))
        self.preferred_return_inr = min(preferred, max(distributed - drawn, Decimal('0')))

        above_hurdle = max(profit - self.preferred_return_inr, Decimal('0'))
        self.profit_above_hurdle_inr = above_hurdle
        self.gp_carry_owed_inr = above_hurdle * (self.carry_rate_pct / 100)

        clawback = self.gp_carry_paid_inr - self.gp_carry_owed_inr
        self.clawback_amount_inr = abs(clawback)
        if clawback > 0:
            self.clawback_direction = 'gp_owes'
        elif clawback < 0:
            self.clawback_direction = 'lp_owes'
        else:
            self.clawback_direction = 'none'

    def __str__(self):
        return f'Clawback: {self.close_event.fund.name} — {self.clawback_direction}'


class SEBIDeregistration(models.Model):
    """
    SEBI AIF deregistration filing workflow.
    Per SEBI AIF Regulations — surrender of Certificate of Registration.
    """
    STATUS_CHOICES = [
        ('not_started', 'Not Started'),
        ('noc_obtained', 'NOC from LPs Obtained'),
        ('sebi_application', 'Application Filed with SEBI'),
        ('sebi_review', 'SEBI Under Review'),
        ('approved', 'SEBI Approved Deregistration'),
        ('completed', 'Deregistration Complete'),
        ('rejected', 'SEBI Rejected — Resubmit'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    close_event = models.OneToOneField(
        FundCloseEvent, on_delete=models.CASCADE, related_name='sebi_deregistration',
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='not_started')

    # Key dates
    noc_date = models.DateField(null=True, blank=True, help_text='Date NOC from all LPs obtained')
    application_date = models.DateField(null=True, blank=True, help_text='Date SEBI application filed')
    sebi_acknowledgement_no = models.CharField(max_length=50, blank=True)
    sebi_approval_date = models.DateField(null=True, blank=True)
    sebi_certificate_surrender_date = models.DateField(null=True, blank=True)

    # Document trail
    final_accounts_document = models.ForeignKey(
        'documents.Document', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sebi_deregistration_final_accounts',
        help_text='Final audited accounts PDF',
    )
    application_document = models.ForeignKey(
        'documents.Document', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sebi_deregistration_applications',
        help_text='SEBI deregistration application PDF',
    )

    compliance_officer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
        help_text='Compliance officer responsible for filing',
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'SEBI Dereg: {self.close_event.fund.name} — {self.get_status_display()}'
