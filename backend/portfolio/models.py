"""
Portfolio Monitoring models — Phase 2 of TrackFundAI.

Stores the portfolio hierarchy (fund → sector → segment → company)
and all financial time-series data in PostgreSQL. Replaces portfolio.json
as the source of truth for the dashboard.

Key design decisions:
  - PortfolioNode stores the full hierarchy with a self-referencing parent FK
  - Financials are stored as JSONField to preserve the exact API response shape
    (monthly_pl, cash_flow, working_capital, budget_vs_actual, sales_by_segment, etc.)
  - PortfolioSnapshot records when the portfolio was last built/loaded
  - The builder writes to these models; service.py reads from them
"""

import uuid
from django.db import models
from django.conf import settings


class PortfolioSnapshot(models.Model):
    """
    Records a portfolio build event. Each time the builder runs
    (whether from Excel parse or manual rebuild), a new snapshot is created.
    The latest active snapshot per organization is the one shown to users.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='portfolio_snapshots',
        null=True, blank=True,
    )
    schema_version = models.CharField(max_length=10, default='2.0')
    base_currency = models.CharField(max_length=3, default='USD')
    fx_as_of = models.CharField(max_length=10, blank=True)
    fx_rates = models.JSONField(default=dict, blank=True)
    period_range = models.JSONField(default=dict, blank=True)
    generated_at = models.DateTimeField(auto_now_add=True)
    source = models.CharField(
        max_length=20,
        choices=[
            ('excel_parse', 'Excel Parse'),
            ('json_import', 'JSON Import'),
            ('manual', 'Manual'),
        ],
        default='excel_parse',
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-generated_at']

    def __str__(self):
        return f'Snapshot {self.generated_at:%Y-%m-%d %H:%M} ({self.source})'


class PortfolioNode(models.Model):
    """
    A single node in the portfolio hierarchy.
    Levels: fund, sector, segment, company.

    The `node_id` field stores the hierarchical slug
    (e.g., "fund_healthcare::sector_distribution::segment_lab_distribution::company_analisa").
    This is the same ID format used in the JSON and API responses.
    """
    LEVEL_CHOICES = [
        ('fund', 'Fund'),
        ('sector', 'Sector'),
        ('segment', 'Segment'),
        ('company', 'Company'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    snapshot = models.ForeignKey(
        PortfolioSnapshot,
        on_delete=models.CASCADE,
        related_name='nodes',
    )
    node_id = models.CharField(max_length=500, db_index=True)
    name = models.CharField(max_length=255)
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES)
    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='children_set',
    )
    parent_node_id = models.CharField(max_length=500, blank=True, null=True)
    currency = models.CharField(max_length=3, default='USD')
    native_currency = models.CharField(max_length=3, blank=True, null=True)
    is_real = models.BooleanField(default=False)
    description = models.TextField(blank=True, null=True)

    # All financial data stored as JSON — preserves exact API shape
    financials = models.JSONField(default=dict, blank=True)

    # Ordering for consistent tree traversal
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'name']
        indexes = [
            models.Index(fields=['snapshot', 'level']),
            models.Index(fields=['snapshot', 'node_id']),
            models.Index(fields=['snapshot', 'parent']),
        ]
        unique_together = [('snapshot', 'node_id')]

    def __str__(self):
        return f'{self.level}: {self.name} ({self.node_id})'
