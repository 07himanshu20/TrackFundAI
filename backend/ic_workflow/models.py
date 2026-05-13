"""
IC Workflow — Investment Committee deal sourcing → screening → presentation → vote → capital call.
v5 Fund Lifecycle: IC Workflow section.
"""
import uuid
from django.conf import settings
from django.db import models


class DealPipeline(models.Model):
    """
    Tracks a deal from sourcing through IC decision.
    One record per deal opportunity per organization.
    """
    STAGE_CHOICES = [
        ('sourced', 'Sourced'),
        ('initial_screen', 'Initial Screen'),
        ('deep_dive', 'Deep Dive'),
        ('term_sheet', 'Term Sheet'),
        ('ic_presentation', 'IC Presentation'),
        ('approved', 'IC Approved'),
        ('rejected', 'IC Rejected'),
        ('closed', 'Deal Closed'),
        ('passed', 'Passed / No Action'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE, related_name='deal_pipeline',
    )
    fund = models.ForeignKey(
        'funds.Fund', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='deal_pipeline',
        help_text='Target fund for this investment',
    )
    company_name = models.CharField(max_length=255)
    sector = models.CharField(max_length=100, blank=True)
    sub_sector = models.CharField(max_length=100, blank=True)
    geography = models.CharField(max_length=100, default='India')
    stage = models.CharField(max_length=20, choices=STAGE_CHOICES, default='sourced')

    proposed_investment_inr = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Proposed investment amount in INR Cr',
    )
    equity_stake_pct = models.DecimalField(
        max_digits=6, decimal_places=3, null=True, blank=True,
        help_text='Proposed equity stake percentage',
    )
    pre_money_valuation_inr = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Pre-money valuation in INR Cr',
    )

    sourced_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sourced_deals',
    )
    sourced_date = models.DateField(null=True, blank=True)
    source_channel = models.CharField(
        max_length=30,
        choices=[
            ('network', 'Network Referral'), ('accelerator', 'Accelerator'),
            ('inbound', 'Inbound'), ('scout', 'Scout'), ('co_investor', 'Co-investor'),
            ('other', 'Other'),
        ],
        default='inbound',
    )

    executive_summary = models.TextField(blank=True)
    rejection_reason = models.TextField(blank=True)
    pass_reason = models.TextField(blank=True)

    linked_portfolio_company = models.ForeignKey(
        'investments.PortfolioCompany', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='deal_pipeline',
        help_text='Set once deal is closed and company is added to portfolio',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'stage']),
        ]

    def __str__(self):
        return f'{self.company_name} — {self.get_stage_display()}'


class ICPresentation(models.Model):
    """
    Formal IC memo / presentation deck for a deal, presented to the IC.
    One deal can have multiple IC presentations (re-presentations after modifications).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    deal = models.ForeignKey(DealPipeline, on_delete=models.CASCADE, related_name='presentations')
    presentation_date = models.DateField()
    presenter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ic_presentations',
    )
    memo_document = models.ForeignKey(
        'documents.Document', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ic_presentations',
        help_text='IC memo PDF in document vault',
    )
    deck_document = models.ForeignKey(
        'documents.Document', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ic_decks',
        help_text='Investment thesis presentation deck',
    )
    investment_thesis = models.TextField(blank=True)
    key_risks = models.TextField(blank=True)
    mitigants = models.TextField(blank=True)
    recommended_valuation_inr = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
    )
    quorum_required = models.PositiveIntegerField(
        default=3,
        help_text='Minimum IC members required for quorum (default 3 of 5)',
    )
    outcome = models.CharField(
        max_length=15,
        choices=[
            ('pending', 'Pending Vote'), ('approved', 'Approved'),
            ('rejected', 'Rejected'), ('deferred', 'Deferred for More Info'),
        ],
        default='pending',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-presentation_date']

    def __str__(self):
        return f'IC: {self.deal.company_name} on {self.presentation_date}'


class ICVote(models.Model):
    """Individual IC member vote on a presentation."""
    VOTE_CHOICES = [
        ('approve', 'Approve'),
        ('reject', 'Reject'),
        ('abstain', 'Abstain'),
        ('defer', 'Defer'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    presentation = models.ForeignKey(ICPresentation, on_delete=models.CASCADE, related_name='votes')
    voter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ic_votes',
    )
    vote = models.CharField(max_length=10, choices=VOTE_CHOICES)
    comment = models.TextField(blank=True)
    conditions = models.TextField(
        blank=True,
        help_text='Conditions attached to approval (e.g., "subject to due diligence on IP")',
    )
    voted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('presentation', 'voter')
        ordering = ['-voted_at']

    def __str__(self):
        return f'{self.voter.username}: {self.vote} — {self.presentation}'


class ICDecision(models.Model):
    """
    Final IC decision record after quorum is reached.
    Triggers: capital call or rejection notification.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    presentation = models.OneToOneField(
        ICPresentation, on_delete=models.CASCADE, related_name='decision',
    )
    decision = models.CharField(
        max_length=15,
        choices=[
            ('approved', 'Approved'), ('rejected', 'Rejected'), ('deferred', 'Deferred'),
        ],
    )
    decision_date = models.DateField()
    approved_investment_inr = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Final approved investment amount (may differ from proposed)',
    )
    approved_equity_stake_pct = models.DecimalField(
        max_digits=6, decimal_places=3, null=True, blank=True,
    )
    conditions = models.TextField(
        blank=True,
        help_text='Conditions precedent to close (CP list)',
    )
    capital_call_triggered = models.BooleanField(
        default=False,
        help_text='True once capital call is issued to LPs for this investment',
    )
    capital_call_date = models.DateField(
        null=True, blank=True,
        help_text='Date capital call was issued',
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ic_decisions_made',
        help_text='GP Partner who recorded the final decision',
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-decision_date']

    def __str__(self):
        return f'{self.decision.upper()}: {self.presentation.deal.company_name} on {self.decision_date}'

    def save(self, *args, **kwargs):
        """Sync deal stage to match IC decision."""
        super().save(*args, **kwargs)
        deal = self.presentation.deal
        if self.decision == 'approved':
            deal.stage = 'approved'
        elif self.decision == 'rejected':
            deal.stage = 'rejected'
        deal.save(update_fields=['stage'])
